"""M02 exact, scoped aliases; ambiguity is returned instead of forced merging."""

import hashlib
import re
from typing import Annotated
from uuid import uuid4

from pydantic import Field
from sqlalchemy import delete, func, or_, select

from .contracts import (
    Contract,
    EntityBinding,
    EntityCandidate,
    EntityOutput,
    Identifier,
    JobSpec,
    ModuleResult,
    PositiveInt,
    PrivacyInput,
    PrivacyPreference,
    VersionRef,
)
from .execution import MemoryFault
from .lifecycle import normalized
from .tables import ControlOperation, Dependency, DerivedView, Entity, EntityAlias, Job


class EntityBody(Contract):
    canonical_name: Annotated[str, Field(min_length=1, max_length=128)]
    aliases: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]], Field(max_length=16)
    ]
    expected_version: PositiveInt | None = None
    idempotency_key: Identifier


def entity_audit_id(entity_id):
    return "entity:" + hashlib.sha256(entity_id.encode()).hexdigest()


class PublicEntity(Contract):
    entity_id: Identifier
    version: PositiveInt
    canonical_name: str
    aliases: list[str]


class PublicEntityPage(Contract):
    items: list[PublicEntity]
    next_cursor: Identifier | None


class EntityPrivacyInput(PrivacyInput):
    entity_id: Identifier
    expected_entity_version: PositiveInt


class EntityMixin:
    async def index_entity_aliases(self, db, scope, entity):
        await db.execute(
            delete(EntityAlias).where(
                EntityAlias.account_id == scope.user_id,
                EntityAlias.companion_id == scope.companion_id,
                EntityAlias.entity_id == entity.entity_id,
            )
        )
        for name in sorted({normalized(s) for s in [entity.canonical_name] + entity.aliases}):
            if name:
                db.add(
                    EntityAlias(
                        account_id=scope.user_id,
                        companion_id=scope.companion_id,
                        entity_id=entity.entity_id,
                        alias=name,
                        alias_key=hashlib.sha256(name.encode()).hexdigest(),
                    )
                )

    async def normalize_subject(self, db, scope, candidate):
        if candidate.statement.subject_id == scope.user_id:
            if candidate.statement.predicate == "relationship":
                # Only explicit introductions join a family role and a name.
                # A coincident name or a pronoun never suffices to merge people.
                for evidence in candidate.sources:
                    match = re.search(
                        r"我(?:的)?(妈妈|爸爸|姐姐|妹妹|哥哥|弟弟|妻子|丈夫|女朋友|男朋友)"
                        r"(?:叫|名叫|名字是)([\u3400-\u9fff]{2,4})(?=[，。！？,!?；;\s]|$)",
                        evidence.quote,
                    )
                    if not match:
                        continue
                    role, name = match.groups()
                    ids = set(
                        await db.scalars(
                            select(EntityAlias.entity_id).where(
                                EntityAlias.account_id == scope.user_id,
                                EntityAlias.companion_id == scope.companion_id,
                                EntityAlias.alias.in_([role, name]),
                            )
                        )
                    )
                    if len(ids) > 1:
                        continue
                    if ids:
                        row = await db.get(Entity, next(iter(ids)))
                    else:
                        identified = await self.normalize_subject(
                            db,
                            scope,
                            candidate.model_copy(
                                update={
                                    "statement": candidate.statement.model_copy(
                                        update={"subject_id": role}
                                    )
                                }
                            ),
                        )
                        row = await db.get(Entity, identified.statement.subject_id)
                    value = EntityCandidate.model_validate_json(row.entity_json)
                    refs = {(r.object_id, r.version): r for r in value.context_refs}
                    for pointer in candidate.sources:
                        ref = VersionRef(
                            object_id=pointer.event_id, version=pointer.source_revision
                        )
                        if (ref.object_id, ref.version) not in refs:
                            db.add(
                                Dependency(
                                    account_id=scope.user_id,
                                    parent_id=ref.object_id,
                                    parent_version=ref.version,
                                    child_id=value.entity_id,
                                    child_version=1,
                                    relation="entity_from",
                                )
                            )
                        refs[(ref.object_id, ref.version)] = ref
                    value = value.model_copy(
                        update={
                            "aliases": sorted(set(value.aliases + [role, name]))[:16],
                            "context_refs": list(refs.values())[-32:],
                        }
                    )
                    row.entity_json = value.model_dump_json()
                    await self.index_entity_aliases(db, scope, value)
                    await db.flush()
            return candidate
        mention = normalized(candidate.statement.subject_id)
        matched_ids = select(EntityAlias.entity_id).where(
            EntityAlias.account_id == scope.user_id,
            EntityAlias.companion_id == scope.companion_id,
            EntityAlias.alias_key == hashlib.sha256(mention.encode()).hexdigest(),
            EntityAlias.alias == mention,
        )
        rows = list(
            await db.scalars(
                select(Entity)
                .where(
                    Entity.account_id == scope.user_id,
                    Entity.companion_id == scope.companion_id,
                    or_(Entity.id == candidate.statement.subject_id, Entity.id.in_(matched_ids)),
                )
                .limit(21)
            )
        )
        if len(rows) > 20:
            raise MemoryFault(
                "manual_review_required", "Too many matching people; confirm a specific alias"
            )
        matches = []
        for row in rows:
            entity = EntityCandidate.model_validate_json(row.entity_json)
            mention = candidate.statement.subject_id
            if mention == entity.entity_id or normalized(mention) in {
                normalized(name) for name in [entity.canonical_name] + entity.aliases
            }:
                for source in entity.context_refs:
                    await self._source(db, scope, source.object_id, source.version)
                matches.append(entity.entity_id)
        if (
            not matches
            and 1 < len(mention) <= 128
            and mention
            not in {
                "他",
                "她",
                "他们",
                "她们",
                "那个人",
                "这个人",
                "朋友",
                "同事",
                "有人",
                "自己",
            }
            and any(mention in source.quote for source in candidate.sources)
        ):
            # A directly attributed user report establishes a person automatically.
            # Deterministic scoped IDs and the admission account lock make retries safe.
            entity_id = hashlib.sha256(
                (scope.model_dump_json() + ":person:" + mention).encode()
            ).hexdigest()
            value = EntityCandidate(
                entity_id=entity_id,
                entity_type="person",
                canonical_name=mention,
                aliases=[],
                context_refs=list(
                    {
                        (p.event_id, p.source_revision): VersionRef(
                            object_id=p.event_id, version=p.source_revision
                        )
                        for p in candidate.sources
                    }.values()
                ),
            )
            db.add(
                Entity(
                    id=entity_id,
                    account_id=scope.user_id,
                    companion_id=scope.companion_id,
                    entity_json=value.model_dump_json(),
                )
            )
            await self.index_entity_aliases(db, scope, value)
            for ref in value.context_refs:
                db.add(
                    Dependency(
                        account_id=scope.user_id,
                        parent_id=ref.object_id,
                        parent_version=ref.version,
                        child_id=entity_id,
                        child_version=1,
                        relation="entity_from",
                    )
                )
            await db.flush()
            matches = [entity_id]
        if len(matches) != 1:
            raise MemoryFault(
                "manual_review_required",
                "Confirm a unique person and alias before recording this fact",
            )
        return candidate.model_copy(
            update={"statement": candidate.statement.model_copy(update={"subject_id": matches[0]})}
        )

    async def public_entities(self, scope, page):
        async with self.database.sessions() as db:
            await self._account(db, scope)
            query = select(Entity).where(
                Entity.account_id == scope.user_id, Entity.companion_id == scope.companion_id
            )
            if page.cursor:
                query = query.where(Entity.id > page.cursor)
            rows = list(await db.scalars(query.order_by(Entity.id).limit(page.limit + 1)))
            items = []
            for row in rows[: page.limit]:
                entity = EntityCandidate.model_validate_json(row.entity_json)
                version = (
                    await db.scalar(
                        select(func.max(DerivedView.version)).where(
                            DerivedView.id == entity_audit_id(entity.entity_id),
                            DerivedView.account_id == scope.user_id,
                        )
                    )
                    or 1
                )
                items.append(
                    PublicEntity(
                        entity_id=entity.entity_id,
                        version=version,
                        canonical_name=entity.canonical_name,
                        aliases=entity.aliases,
                    )
                )
            return PublicEntityPage(
                items=items, next_cursor=rows[page.limit - 1].id if len(rows) > page.limit else None
            )

    async def save_entity(self, scope, body, entity_id=None):
        event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="entity_update" if entity_id else "entity_create",
            body=body,
            target=entity_id or "new",
            text="确认人物：" + body.canonical_name + "；称呼：" + "、".join(body.aliases),
        )
        if receipt:
            return PublicEntity.model_validate_json(receipt)
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            if not prefs.long_term_memory_enabled:
                raise MemoryFault("privacy_changed", "Enable memory before confirming a person")
            if not normalized(body.canonical_name) or any(
                not normalized(alias) for alias in body.aliases
            ):
                raise MemoryFault("invalid_model_output", "Person names cannot be blank")
            if len({normalized(alias) for alias in body.aliases}) != len(body.aliases):
                raise MemoryFault("invalid_model_output", "Duplicate person aliases")
            row = (
                await db.scalar(
                    select(Entity).where(
                        Entity.id == entity_id,
                        Entity.account_id == account.id,
                        Entity.companion_id == scope.companion_id,
                    )
                )
                if entity_id
                else None
            )
            if entity_id and row is None:
                raise MemoryFault("not_found", "Person is not available in this account")
            old = EntityCandidate.model_validate_json(row.entity_json) if row else None
            entity_id = entity_id or uuid4().hex
            version = await db.scalar(
                select(func.max(DerivedView.version)).where(
                    DerivedView.id == entity_audit_id(entity_id),
                    DerivedView.account_id == account.id,
                )
            ) or (1 if old else 0)
            if old and body.expected_version != version:
                raise MemoryFault("version_conflict", "Person version changed")
            if not old and body.expected_version is not None:
                raise MemoryFault(
                    "invalid_transition", "New person cannot have an expected version"
                )
            value = EntityCandidate(
                entity_id=entity_id,
                entity_type=old.entity_type if old else "person",
                canonical_name=body.canonical_name.strip(),
                aliases=[a.strip() for a in body.aliases],
                context_refs=(old.context_refs[-31:] if old else [])
                + [VersionRef(object_id=event.event_id, version=1)],
            )
            if row is None:
                row = Entity(id=entity_id, account_id=account.id, companion_id=scope.companion_id)
                db.add(row)
            row.entity_json = value.model_dump_json()
            await self.index_entity_aliases(db, scope, value)
            db.add(
                DerivedView(
                    id=entity_audit_id(entity_id),
                    version=version + 1,
                    account_id=account.id,
                    companion_id=scope.companion_id,
                    kind="entity_revision",
                    state="valid",
                    document_json=value.model_dump_json(),
                )
            )
            for source in value.context_refs:
                if (
                    await db.get(
                        Dependency, (account.id, source.object_id, source.version, entity_id, 1)
                    )
                    is None
                ):
                    db.add(
                        Dependency(
                            account_id=account.id,
                            parent_id=source.object_id,
                            parent_version=source.version,
                            child_id=entity_id,
                            child_version=1,
                            relation="entity_from",
                        )
                    )
                db.add(
                    Dependency(
                        account_id=account.id,
                        parent_id=source.object_id,
                        parent_version=source.version,
                        child_id=entity_audit_id(entity_id),
                        child_version=version + 1,
                        relation="entity_revision",
                    )
                )
            account.memory_revision += 1
            import hashlib

            from sqlalchemy import or_

            from .storage import SourceRevision

            mentions = [value.canonical_name] + value.aliases
            pending = list(
                await db.scalars(
                    select(SourceRevision)
                    .where(
                        SourceRevision.account_id == account.id,
                        SourceRevision.companion_id == scope.companion_id,
                        SourceRevision.state == "needs_confirmation",
                        SourceRevision.source_json.is_not(None),
                        SourceRevision.revoked.is_(False),
                        SourceRevision.expires_at > self.clock(),
                        or_(
                            *[
                                SourceRevision.source_json.contains(name, autoescape=True)
                                for name in mentions
                            ]
                        ),
                    )
                    .order_by(SourceRevision.sequence)
                    .limit(12)
                )
            )
            for source in pending:
                job_id = hashlib.sha256(
                    (event.event_id + source.event_id + str(source.revision)).encode()
                ).hexdigest()
                if await db.get(Job, job_id):
                    continue
                await self._job_capacity(db, account.id)
                spec = JobSpec(
                    job_id=job_id,
                    kind="extract",
                    payload_ref=source.event_id,
                    payload_schema="source-event.v1",
                    source_refs=[VersionRef(object_id=source.event_id, version=source.revision)],
                    input_watermark=account.source_watermark,
                    privacy_epoch=account.privacy_epoch,
                    idempotency_key=job_id,
                    available_at=self.clock(),
                )
                db.add(
                    Job(
                        id=job_id,
                        account_id=account.id,
                        companion_id=scope.companion_id,
                        kind="extract",
                        spec_json=spec.model_dump_json(),
                        state="queued",
                        attempt=0,
                        available_at=self.clock(),
                    )
                )
                source.state = "persisted"
            output = PublicEntity(
                entity_id=entity_id,
                version=version + 1,
                canonical_name=value.canonical_name,
                aliases=value.aliases,
            )
            control = await db.get(ControlOperation, (account.id, body.idempotency_key))
            control.result_json = output.model_dump_json()
            return output

    async def delete_entity(self, scope, entity_id, body):
        from .contracts import PrivacyOutput, VersionFence
        from .http import meta

        if not body.confirmed or not body.delete_related_derivatives:
            raise MemoryFault("invalid_transition", "Confirm person and related-source deletion")
        event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="entity_delete",
            target=entity_id,
            body=body,
            text="delete_entity:" + entity_id,
        )
        if receipt:
            return PrivacyOutput.model_validate_json(receipt)
        async with self.database.sessions() as db:
            account = await self._account(db, scope)
            row = await db.scalar(
                select(Entity).where(
                    Entity.id == entity_id,
                    Entity.account_id == account.id,
                    Entity.companion_id == scope.companion_id,
                )
            )
            if row is None:
                raise MemoryFault("not_found", "Person is unavailable")
            entity = EntityCandidate.model_validate_json(row.entity_json)
            from .tables import FactSource, FactVersion

            source_ids = {r.object_id for r in entity.context_refs}
            source_ids.update(
                await db.scalars(
                    select(FactSource.event_id)
                    .join(
                        FactVersion,
                        (FactSource.memory_id == FactVersion.memory_id)
                        & (FactSource.version == FactVersion.version),
                    )
                    .where(
                        FactVersion.account_id == account.id,
                        FactVersion.companion_id == scope.companion_id,
                        FactVersion.subject_id == entity_id,
                    )
                    .limit(1001)
                )
            )
            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            fence = VersionFence(
                privacy_epoch=account.privacy_epoch,
                memory_revision=account.memory_revision,
                owner_epoch=0,
            )
        if not source_ids or len(source_ids) > 1000:
            raise MemoryFault("budget_exceeded", "Person deletion requires a bounded source set")
        result = await self.privacy(
            EntityPrivacyInput(
                meta=meta(self, lane="control", budget_ms=3000),
                scope=scope,
                fence=fence,
                operation="delete_sources",
                preferences=None,
                target_refs=[],
                target_source_ids=sorted(source_ids),
                target_time=None,
                expected_preference_version=prefs.preference_version,
                confirmed_control_event_id=event.event_id,
                deletion_job_id=None,
                entity_id=entity_id,
                expected_entity_version=body.expected_version,
            )
        )
        if result.error:
            raise MemoryFault(
                result.error.code, result.error.message, retryable=result.error.retryable
            )
        async with self.database.sessions.begin() as db:
            control = await db.get(ControlOperation, (scope.user_id, body.idempotency_key))
            control.result_json = result.data.model_dump_json()
        return result.data

    async def entities(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                known = list(
                    await db.scalars(
                        select(Entity)
                        .where(
                            Entity.account_id == account.id,
                            Entity.companion_id == request.scope.companion_id,
                        )
                        .limit(129)
                    )
                )
                if len(known) > 128:
                    raise MemoryFault(
                        "budget_exceeded", "Entity search needs an explicit scoped alias page"
                    )
                values = [EntityCandidate.model_validate_json(row.entity_json) for row in known]
                if any(
                    entity.entity_id not in {e.entity_id for e in values}
                    for entity in request.known_entities
                ):
                    raise MemoryFault("scope_mismatch", "Unknown entity supplied by caller")
                bindings, candidates, new = [], [], []
                for candidate in request.candidates:
                    subject = candidate.statement.subject_id
                    if subject == request.scope.user_id:
                        candidates.append(candidate)
                        continue
                    matches = [
                        entity
                        for entity in values
                        if subject == entity.entity_id
                        or normalized(subject)
                        in {normalized(name) for name in [entity.canonical_name] + entity.aliases}
                    ]
                    resolved = matches[0].entity_id if len(matches) == 1 else None
                    status = "existing" if resolved else "ambiguous" if matches else "new_candidate"
                    if resolved:
                        candidate = candidate.model_copy(
                            update={
                                "statement": candidate.statement.model_copy(
                                    update={"subject_id": resolved}
                                )
                            }
                        )
                    elif not matches:
                        proposed = EntityCandidate(
                            entity_id=uuid4().hex,
                            entity_type="person",
                            canonical_name=subject,
                            aliases=[],
                            context_refs=[
                                VersionRef(object_id=s.event_id, version=s.source_revision)
                                for s in candidate.sources
                            ],
                        )
                        new.append(proposed)
                    candidates.append(candidate)
                    bindings.append(
                        EntityBinding(
                            candidate_id=candidate.candidate_id,
                            mention=subject,
                            resolved_entity_id=resolved,
                            status=status,
                            evidence_ids=[s.event_id for s in candidate.sources],
                        )
                    )
                return ModuleResult(
                    status="ok",
                    data=EntityOutput(
                        candidates=candidates,
                        bindings=bindings,
                        new_entities=new,
                        clarification="请确认提到的人是谁。"
                        if any(b.status != "existing" for b in bindings)
                        else None,
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)
