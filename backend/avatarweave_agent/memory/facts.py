"""M01/M04/M05/M26: evidence validation, bitemporal revisions and atomic commits."""

import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select, update

from .contracts import (
    AdmissionDecision,
    AdmissionInput,
    AdmissionOutput,
    CommitInput,
    CommitOutput,
    ConflictInput,
    EvidenceCapsule,
    EvidencePointer,
    MemoryRecord,
    ModuleResult,
    PrivacyPreference,
    RepositoryOutput,
    VersionRef,
)
from .execution import MemoryFault
from .lifecycle import fact_key, resolve_conflict, rule_for
from .storage import EventHead, SourceRevision
from .tables import (
    Capsule,
    Dependency,
    DerivedView,
    Entity,
    FactKey,
    FactSource,
    FactVersion,
    ObjectManifest,
    OperationReceipt,
    VectorEntry,
)
from .utils import utc


def load_record(row):
    if row.record_json is None or row.status == "deleted":
        raise MemoryFault("not_found", "Memory is unavailable")
    value = MemoryRecord.model_validate_json(row.record_json)
    return value.model_copy(
        update={"status": row.status, "system_to": utc(row.system_to) if row.system_to else None}
    )


def fact_scope(scope):
    return (FactVersion.account_id == scope.user_id, FactVersion.companion_id == scope.companion_id)


async def invalidate(db, account_id, refs, *, erase=False):
    """Bounded DAG traversal. SQL authority marks descendants before they can be reused.

    Oversized cascades fail the transaction; the delete API can enqueue a scoped
    account-wide fence before a large physical sweep rather than returning partial revocation.
    """
    pending = [(r.object_id, r.version) for r in refs]
    seen = set()
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        if len(seen) > 4096:
            raise MemoryFault("budget_exceeded", "Dependency cascade needs a bounded deletion job")
        children = list(
            await db.scalars(
                select(Dependency)
                .where(
                    Dependency.account_id == account_id,
                    Dependency.parent_id == parent[0],
                    Dependency.parent_version == parent[1],
                )
                .limit(4097)
            )
        )
        for edge in children:
            pending.append((edge.child_id, edge.child_version))
            await db.execute(delete(Entity).where(Entity.account_id == account_id, Entity.id == edge.child_id))
            await db.execute(
                update(DerivedView)
                .where(
                    DerivedView.account_id == account_id,
                    DerivedView.id == edge.child_id,
                    DerivedView.version == edge.child_version,
                )
                .values(state="invalidated", **({"document_json": None} if erase else {}))
            )
            await db.execute(
                update(FactVersion)
                .where(
                    FactVersion.account_id == account_id,
                    FactVersion.memory_id == edge.child_id,
                    FactVersion.version == edge.child_version,
                )
                .values(
                    status="deleted" if erase else "uncertain",
                    **({"record_json": None} if erase else {}),
                )
            )
        await db.execute(
            delete(VectorEntry).where(
                VectorEntry.account_id == account_id,
                VectorEntry.memory_id == parent[0],
                VectorEntry.version == parent[1],
            )
        )
    # Export/archive snapshots are fenced immediately. Their physical cleanup is
    # separately tracked; never return a ready download based on an obsolete snapshot.
    await db.execute(
        update(ObjectManifest)
        .where(
            ObjectManifest.account_id == account_id,
            ObjectManifest.state.not_in(["invalidated", "revoked"]),
        )
        .values(state="invalidated")
    )


class FactMixin:
    async def _source(self, db, scope, event_id, revision):
        row = await db.scalar(
            select(SourceRevision)
            .join(
                EventHead,
                and_(
                    EventHead.event_id == SourceRevision.event_id,
                    EventHead.revision == SourceRevision.revision,
                ),
            )
            .where(
                SourceRevision.account_id == scope.user_id,
                SourceRevision.companion_id == scope.companion_id,
                SourceRevision.event_id == event_id,
                SourceRevision.revision == revision,
                SourceRevision.revoked.is_(False),
            )
        )
        if row is None:
            raise MemoryFault("evidence_missing", "Source is missing or revoked")
        return row

    async def _evidence(self, db, scope, candidate):
        events = {}
        for evidence in candidate.sources:
            row = await self._source(db, scope, evidence.event_id, evidence.source_revision)
            if row.source_json is None or utc(row.expires_at) <= self.clock():
                raise MemoryFault("evidence_missing", "Raw source is no longer available")
            from .contracts import SourceEvent

            event = SourceEvent.model_validate_json(row.source_json)
            if (
                evidence.origin != event.origin
                or event.content[evidence.start_char : evidence.end_char] != evidence.quote
                or not event.is_final
                or event.fictional
            ):
                raise MemoryFault(
                    "evidence_missing", "Evidence span does not match its authorized source"
                )
            if event.origin not in {"user_text", "user_transcript", "user_ui"}:
                raise MemoryFault(
                    "manual_review_required", "This source cannot assert a personal fact"
                )
            if event.origin != "user_ui":
                from .lifecycle import enclosing_clause

                left, right = enclosing_clause(
                    event.content, evidence.start_char, evidence.end_char
                )
                if (left, right) != (evidence.start_char, evidence.end_char):
                    raise MemoryFault(
                        "manual_review_required",
                        "Evidence must retain surrounding attribution and context",
                    )
            events[(event.event_id, event.revision)] = event
        if candidate.statement.subject_id != scope.user_id:
            entity = await db.scalar(
                select(Entity).where(
                    Entity.id == candidate.statement.subject_id,
                    Entity.account_id == scope.user_id,
                    Entity.companion_id == scope.companion_id,
                )
            )
            if entity is None:
                raise MemoryFault("manual_review_required", "Subject entity requires confirmation")
        if candidate.namespace == "character_lore" or candidate.assertion_kind not in {
            "user_report",
            "user_confirmed",
        }:
            raise MemoryFault(
                "manual_review_required", "Automatic personal commits require a user report"
            )
        if candidate.interpretation not in {"asserted", "negated"}:
            raise MemoryFault(
                "manual_review_required",
                "Hypothetical or ambiguous statements require clarification",
            )
        from .lifecycle import normalized

        if not any(
            normalized(candidate.statement.canonical_value) in normalized(e.quote)
            for e in candidate.sources
        ):
            raise MemoryFault("evidence_missing", "Canonical value is absent from its evidence")
        if candidate.assertion_kind == "user_confirmed" and not any(
            e.origin == "user_ui" for e in events.values()
        ):
            raise MemoryFault(
                "manual_review_required", "Model output cannot manufacture user confirmation"
            )
        rule_for(candidate)
        if not any(event.origin == "user_ui" for event in events.values()) and any(
            not any(normalized(q.value) in normalized(e.quote) for e in candidate.sources)
            for q in candidate.statement.qualifiers
        ):
            raise MemoryFault(
                "evidence_missing", "Comparison qualifier is absent from its evidence"
            )
        from .tables import EvidenceSuppression

        key = fact_key(scope, candidate)
        for evidence in candidate.sources:
            if await db.get(
                EvidenceSuppression,
                (scope.user_id, evidence.event_id, evidence.source_revision, key),
            ):
                raise MemoryFault(
                    "version_conflict", "This source was superseded by an explicit fact correction"
                )
        for evidence in candidate.sources:
            if evidence.origin != "user_ui" and re.search(
                r"假如|假设|如果我|开玩笑|朋友说|同事说|他说|她说", evidence.quote
            ):
                raise MemoryFault(
                    "manual_review_required", "Hypothetical or quoted speech needs confirmation"
                )
            if (
                candidate.statement.predicate == "current_city"
                and candidate.statement.polarity == "positive"
                and re.search(r"(?:从没|从未|没|未)[^。！？!?]{0,8}住", evidence.quote)
            ):
                raise MemoryFault(
                    "manual_review_required", "Negated residence cannot become a positive fact"
                )
            if (
                evidence.origin != "user_ui"
                and candidate.statement.predicate == "preference"
                and candidate.statement.polarity == "positive"
                and re.search(
                    r"(?:不|没|未|别|不要)[^。！？!?]{0,4}(?:喜欢|爱喝|爱吃|偏好)", evidence.quote
                )
            ):
                raise MemoryFault(
                    "manual_review_required", "Negated preference needs explicit polarity"
                )
        return events

    async def control_barrier(self, db, account, scope, event):
        text = event.content
        if not re.search(r"别再说我|我(?:从来没|从没|从未|不是)|你(?:听|记)错|纠正", text):
            return
        predicates = []
        if re.search(r"住|城市|搬", text):
            predicates.append("current_city")
        if re.search(r"名字|叫|姓名", text):
            predicates.append("name")
        if re.search(r"职业|工作|任职", text):
            predicates.append("occupation")
        if not predicates:
            return
        rows = list(
            await db.scalars(
                select(FactVersion)
                .where(
                    *fact_scope(scope),
                    FactVersion.subject_id == scope.user_id,
                    FactVersion.predicate.in_(predicates),
                    FactVersion.system_to.is_(None),
                    FactVersion.status == "active",
                )
                .limit(129)
            )
        )
        if len(rows) > 128:
            raise MemoryFault(
                "budget_exceeded", "Correction barrier requires a bounded fact-key page"
            )
        for row in rows:
            row.status = "uncertain"
        if rows:
            account.memory_revision += 1
            await invalidate(
                db, account.id, [VersionRef(object_id=r.memory_id, version=r.version) for r in rows]
            )

    async def admission(self, request: AdmissionInput, gateway):
        async def operation():
            if not request.memory_enabled:
                return ModuleResult[AdmissionOutput](
                    status="ok",
                    data=AdmissionOutput(candidates=[], decisions=[], input_watermark=0),
                )
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())
                prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                if not prefs.long_term_memory_enabled:
                    raise MemoryFault("privacy_changed", "Long-term saving is disabled")
                for event in request.events:
                    if event.scope != request.scope:
                        raise MemoryFault("scope_mismatch", "Admission input crosses scope")
                    row = await self._source(db, request.scope, event.event_id, event.revision)
                    if row.source_json != event.model_dump_json():
                        raise MemoryFault(
                            "version_conflict", "Admission input differs from persisted source"
                        )
                watermark = account.source_watermark
            # No database transaction/connection is held during inference.
            proposed = await gateway.extract(request)
            if len(proposed.candidates) > request.max_candidates:
                raise MemoryFault("budget_exceeded", "Extractor exceeded the candidate budget")
            ids = [c.candidate_id for c in proposed.candidates]
            if len(ids) != len(set(ids)):
                raise MemoryFault("invalid_model_output", "Duplicate candidate identifiers")
            candidates, decisions = [], []
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())
                for candidate in proposed.candidates:
                    allowed_sources = {(e.event_id, e.revision) for e in request.events}
                    if any(
                        (e.event_id, e.source_revision) not in allowed_sources
                        for e in candidate.sources
                    ):
                        raise MemoryFault(
                            "evidence_missing", "Extractor referenced an unprovided source"
                        )
                    try:
                        candidate = await self.normalize_subject(db, request.scope, candidate)
                        await self._evidence(db, request.scope, candidate)
                        decision, reason = "accept", "supported"
                    except MemoryFault as exc:
                        if exc.info.code == "evidence_missing":
                            raise
                        decision, reason = "defer", "out_of_scope"
                    candidates.append(candidate)
                    decisions.append(
                        AdmissionDecision(
                            candidate_id=candidate.candidate_id, decision=decision, reason=reason
                        )
                    )
                visibility = self._visibility(account, None, [], [])
            return ModuleResult[AdmissionOutput](
                status="ok",
                data=AdmissionOutput(
                    candidates=candidates, decisions=decisions, input_watermark=watermark
                ),
                visibility=visibility,
            )

        return await self.executor.run(request, operation)

    async def _comparable(self, db, scope, key):
        rows = list(
            await db.scalars(
                select(FactVersion)
                .where(
                    *fact_scope(scope),
                    FactVersion.fact_key == key,
                    FactVersion.system_to.is_(None),
                    FactVersion.status.in_(["active", "uncertain"]),
                )
                .order_by(FactVersion.memory_id, FactVersion.version)
                .limit(129)
            )
        )
        if len(rows) > 128:
            raise MemoryFault(
                "budget_exceeded", "Complete fact-key comparison needs a larger controlled batch"
            )
        return rows

    async def conflict(self, request: ConflictInput):
        async def operation():
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())
                await self._evidence(db, request.scope, request.candidate)
                rows = await self._comparable(
                    db, request.scope, fact_key(request.scope, request.candidate)
                )
                actual = [load_record(row) for row in rows]
                if request.comparable_records and request.comparable_records != actual:
                    raise MemoryFault("version_conflict", "Comparison snapshot changed")
                output = resolve_conflict(request.model_copy(update={"comparable_records": actual}))
                return ModuleResult(
                    status="ok", data=output, visibility=self._visibility(account, None, [], [])
                )

        return await self.executor.run(request, operation)

    async def _insert_version(self, db, record, key):
        db.add(
            FactVersion(
                memory_id=record.ref.object_id,
                version=record.ref.version,
                account_id=record.scope.user_id,
                companion_id=record.scope.companion_id,
                fact_key=key,
                subject_id=record.statement.subject_id,
                predicate=record.statement.predicate,
                namespace=record.namespace,
                system_from=record.system_from,
                system_to=record.system_to,
                valid_start=record.statement.valid_time.start,
                valid_end=record.statement.valid_time.end,
                status=record.status,
                record_json=record.model_dump_json(),
            )
        )
        await db.flush()
        for event_id, revision in sorted({(e.event_id, e.source_revision) for e in record.sources}):
            db.add(
                FactSource(
                    memory_id=record.ref.object_id,
                    version=record.ref.version,
                    event_id=event_id,
                    source_revision=revision,
                )
            )

    async def commit(self, request: CommitInput):
        async def operation():
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())

                prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                candidate = request.candidate
                events = await self._evidence(db, request.scope, candidate)
                if not prefs.long_term_memory_enabled and not (
                    request.user_confirmation_event
                    and any(
                        e.event_id == request.user_confirmation_event and e.origin == "user_ui"
                        for e in events.values()
                    )
                ):
                    raise MemoryFault("privacy_changed", "Memory saving is disabled")
                receipt = await db.get(OperationReceipt, (account.id, request.meta.operation_id))
                receipt_args = json.dumps(
                    {
                        "candidate": candidate.candidate_id,
                        "fingerprint": hashlib.sha256(
                            candidate.model_dump_json().encode()
                        ).hexdigest(),
                    },
                    sort_keys=True,
                )
                if receipt:
                    if (
                        receipt.operation != "commit"
                        or receipt.companion_id != request.scope.companion_id
                        or receipt.request_json != receipt_args
                    ):
                        raise MemoryFault("version_conflict", "Operation identifier already used")
                    output = CommitOutput.model_validate_json(receipt.result_json)
                    return ModuleResult[CommitOutput](
                        status="ok", data=output, visibility=output.visibility
                    )
                key = fact_key(request.scope, candidate)
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                insert = (
                    sqlite_insert if self.database.engine.dialect.name == "sqlite" else pg_insert
                )
                await db.execute(
                    insert(FactKey)
                    .values(
                        id=key,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        lock_revision=0,
                    )
                    .on_conflict_do_nothing()
                )
                await db.execute(
                    update(FactKey)
                    .where(FactKey.id == key)
                    .values(lock_revision=FactKey.lock_revision + 1)
                )
                rows = await self._comparable(db, request.scope, key)
                actual = [load_record(row) for row in rows]
                expected = {(r.object_id, r.version) for r in request.expected_refs}
                if expected != {(r.ref.object_id, r.ref.version) for r in actual}:
                    raise MemoryFault("version_conflict", "Fact-key versions changed")
                computed = resolve_conflict(
                    ConflictInput(
                        meta=request.meta,
                        scope=request.scope,
                        fence=request.fence,
                        candidate=candidate,
                        predicate_rule=rule_for(candidate),
                        comparable_records=actual,
                    )
                )
                confirmed = False
                if request.user_confirmation_event:
                    confirmed = any(
                        e.event_id == request.user_confirmation_event and e.origin == "user_ui"
                        for e in events.values()
                    )
                    if not confirmed:
                        raise MemoryFault("unauthorized", "Explicit confirmation source required")
                proposals = computed.proposals
                if (
                    confirmed
                    and len(request.proposals) == 1
                    and request.proposals[0].action == "retract"
                ):
                    # UI controls bind an owned target and are distinct from a new life stage.
                    targets = request.proposals[0].target_refs
                    if not targets or any(
                        (r.object_id, r.version) not in expected for r in targets
                    ):
                        raise MemoryFault("version_conflict", "Correction target changed")
                    proposals = request.proposals
                elif request.proposals != computed.proposals:
                    raise MemoryFault(
                        "version_conflict",
                        "Mutation proposal no longer matches deterministic rules",
                    )
                if any(p.requires_user_confirmation for p in proposals) and not confirmed:
                    raise MemoryFault(
                        "manual_review_required", computed.clarification or "Confirmation required"
                    )
                if any(p.action == "defer" for p in proposals):
                    raise MemoryFault("manual_review_required", "Ambiguous fact is not committed")
                # At-least-once extraction may produce a new candidate ID for the
                # same evidence. Do not manufacture a confirmation/version/capsule.
                if len(proposals) == 1 and proposals[0].action == "add_evidence":
                    prior = next(r for r in actual if r.ref == proposals[0].target_refs[0])
                    evidence_keys = {
                        (p.event_id, p.source_revision, p.start_char, p.end_char)
                        for p in prior.sources
                    }
                    if all(
                        (e.event_id, e.source_revision, e.start_char, e.end_char) in evidence_keys
                        for e in candidate.sources
                    ):
                        visibility = self._visibility(account, None, [], [])
                        output = CommitOutput(
                            committed=[prior.ref],
                            superseded=[],
                            memory_revision=account.memory_revision,
                            outbox_ids=[],
                            session_overlay_revision=account.memory_revision,
                            visibility=visibility,
                        )
                        db.add(
                            OperationReceipt(
                                account_id=account.id,
                                companion_id=request.scope.companion_id,
                                operation_id=request.meta.operation_id,
                                operation="commit",
                                request_json=receipt_args,
                                result_json=output.model_dump_json(),
                            )
                        )
                        return ModuleResult[CommitOutput](
                            status="ok", data=output, visibility=visibility
                        )
                pointers = []
                for evidence in candidate.sources:
                    event = events[(evidence.event_id, evidence.source_revision)]
                    capsule_id = None
                    if prefs.retain_evidence_capsules:
                        capsule_id = uuid4().hex
                        capsule = EvidenceCapsule(
                            capsule_id=capsule_id,
                            scope=request.scope,
                            source_session_id=event.session_id,
                            source_event_id=event.event_id,
                            source_revision=event.revision,
                            evidence=evidence,
                            captured_at=self.clock(),
                            retention_authorization_ref=f"preferences:{prefs.preference_version}",
                            original_source_available=True,
                            integrity_sha256=hashlib.sha256(evidence.quote.encode()).hexdigest(),
                            privacy_epoch=account.privacy_epoch,
                        )
                        db.add(
                            Capsule(
                                id=capsule_id,
                                account_id=account.id,
                                companion_id=request.scope.companion_id,
                                event_id=event.event_id,
                                source_revision=event.revision,
                                capsule_json=capsule.model_dump_json(),
                            )
                        )
                    pointers.append(
                        EvidencePointer(
                            event_id=event.event_id,
                            source_revision=event.revision,
                            level="evidence_capsule" if capsule_id else "raw",
                            capsule_id=capsule_id,
                            start_char=evidence.start_char,
                            end_char=evidence.end_char,
                        )
                    )
                committed, superseded = [], []
                now = self.clock()
                by_ref = {(row.memory_id, row.version): row for row in rows}
                handled = set()
                support = None
                correction_id, correction_version = None, 1
                for proposal in proposals:
                    for ref in proposal.target_refs:
                        if (ref.object_id, ref.version) in handled:
                            continue
                        handled.add((ref.object_id, ref.version))
                        row = by_ref[(ref.object_id, ref.version)]
                        previous = load_record(row)
                        row.system_to, row.status = now, "superseded"
                        superseded.append(ref)
                        await invalidate(db, account.id, [ref])
                        if proposal.action == "replace_interval":
                            boundary = candidate.statement.valid_time.start
                            if boundary is None:
                                raise MemoryFault(
                                    "invalid_transition", "A life change requires a boundary"
                                )
                            window = previous.statement.valid_time.model_copy(
                                update={"end": boundary}
                            )
                            historical = previous.model_copy(
                                update={
                                    "ref": VersionRef(
                                        object_id=ref.object_id, version=ref.version + 1
                                    ),
                                    "system_from": now,
                                    "system_to": None,
                                    "statement": previous.statement.model_copy(
                                        update={"valid_time": window}
                                    ),
                                }
                            )
                            await self._insert_version(db, historical, key)
                            committed.append(historical.ref)
                        elif proposal.action == "add_evidence":
                            merged = {p.model_dump_json(): p for p in previous.sources + pointers}
                            if len(merged) > 64:
                                raise MemoryFault(
                                    "budget_exceeded", "Evidence needs a source manifest batch"
                                )
                            support = previous.model_copy(
                                update={
                                    "ref": VersionRef(
                                        object_id=ref.object_id, version=ref.version + 1
                                    ),
                                    "sources": list(merged.values()),
                                    "system_from": now,
                                    "system_to": None,
                                    "mentioned_at": now,
                                    "last_user_confirmed_at": now,
                                }
                            )
                        elif proposal.action == "retract":
                            from .tables import EvidenceSuppression

                            for pointer in previous.sources:
                                suppression_key = (
                                    account.id,
                                    pointer.event_id,
                                    pointer.source_revision,
                                    key,
                                )
                                if await db.get(EvidenceSuppression, suppression_key) is None:
                                    db.add(
                                        EvidenceSuppression(
                                            account_id=account.id,
                                            event_id=pointer.event_id,
                                            source_revision=pointer.source_revision,
                                            fact_key=key,
                                        )
                                    )

                            correction_id, correction_version = ref.object_id, ref.version + 1
                if support is not None:
                    record = support
                else:
                    from .lifecycle import display_statement

                    record = MemoryRecord(
                        ref=VersionRef(
                            object_id=correction_id or uuid4().hex, version=correction_version
                        ),
                        scope=request.scope,
                        namespace=candidate.namespace,
                        content_kind=candidate.content_kind,
                        assertion_kind="user_confirmed" if confirmed else "user_report",
                        statement=candidate.statement,
                        display_text=display_statement(candidate, request.scope.user_id),
                        sources=pointers,
                        recorded_at=now,
                        system_from=now,
                        status="active",
                        tier="hot",
                        sensitivity=candidate.sensitivity,
                        importance=(
                            1.0
                            if any("记住" in e.quote for e in candidate.sources)
                            else 0.8
                            if candidate.content_kind in {"future_intent", "episode"}
                            and any(
                                re.search("结婚|毕业|去世|出生|搬家", e.quote)
                                for e in candidate.sources
                            )
                            else 0.6
                            if candidate.content_kind
                            in {"preference", "interaction_preference", "relationship"}
                            else 0.3
                        ),
                        mentioned_at=now,
                        last_user_confirmed_at=now if confirmed else None,
                    )
                await self._insert_version(db, record, key)
                await self._sync_open_loop(db, record)
                committed.append(record.ref)
                account.memory_revision += 1
                await db.flush()
                visibility = self._visibility(account, None, [], [])
                output = CommitOutput(
                    committed=committed,
                    superseded=superseded,
                    memory_revision=account.memory_revision,
                    outbox_ids=[],
                    session_overlay_revision=account.memory_revision,
                    visibility=visibility,
                )
                db.add(
                    OperationReceipt(
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        operation_id=request.meta.operation_id,
                        operation="commit",
                        request_json=receipt_args,
                        result_json=output.model_dump_json(),
                    )
                )
                return ModuleResult[CommitOutput](status="ok", data=output, visibility=visibility)

        return await self.executor.run(request, operation)

    async def fact_repository(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())

                query = select(FactVersion).where(
                    *fact_scope(request.scope),
                    FactVersion.record_json.is_not(None),
                    FactVersion.status != "deleted",
                )
                if request.operation == "read_versions":
                    if not request.refs:
                        return ModuleResult(
                            status="ok",
                            data=RepositoryOutput(
                                records=[],
                                sources=[],
                                capsules=[],
                                source_metadata=[],
                                next_cursor=None,
                                read_watermark=account.memory_revision,
                            ),
                            visibility=self._visibility(account, None, [], []),
                        )
                    query = query.where(
                        or_(
                            *(
                                and_(
                                    FactVersion.memory_id == r.object_id,
                                    FactVersion.version == r.version,
                                )
                                for r in request.refs
                            )
                        )
                    )
                else:
                    query = query.where(FactVersion.system_to.is_(None))
                    if request.operation == "scan_fact_key":
                        if not request.subject_id or not request.predicate:
                            raise MemoryFault(
                                "invalid_transition", "Fact scans require a subject and predicate"
                            )
                        query = query.where(
                            FactVersion.subject_id == request.subject_id,
                            FactVersion.predicate == request.predicate,
                        )
                    if request.period:
                        # SQL filtering is broad for unknown endpoints; point/interval
                        # semantics are checked on the typed record below.
                        if request.period.start:
                            query = query.where(
                                or_(
                                    FactVersion.valid_end.is_(None),
                                    FactVersion.valid_end >= request.period.start,
                                )
                            )
                        if request.period.end:
                            query = query.where(
                                or_(
                                    FactVersion.valid_start.is_(None),
                                    FactVersion.valid_start <= request.period.end,
                                )
                            )
                if request.cursor:
                    try:
                        revision_text, cursor_id = request.cursor.split(":", 1)
                        if int(revision_text) != account.memory_revision:
                            raise MemoryFault(
                                "version_conflict", "Memory changed during pagination"
                            )
                    except ValueError:
                        raise MemoryFault("invalid_transition", "Invalid memory cursor") from None
                    query = query.where(FactVersion.memory_id > cursor_id)
                rows = list(
                    await db.scalars(
                        query.order_by(FactVersion.memory_id, FactVersion.version).limit(
                            request.page_size + 1
                        )
                    )
                )
                records = [load_record(row) for row in rows[: request.page_size]]
                if request.operation == "read_versions" and len(records) != len(
                    {(r.object_id, r.version) for r in request.refs}
                ):
                    raise MemoryFault("not_found", "One or more memories are unavailable")
                from .lifecycle import overlaps

                if request.period:
                    records = [
                        r for r in records if overlaps(r.statement.valid_time, request.period)
                    ]
                cursor = (
                    f"{account.memory_revision}:{rows[request.page_size - 1].memory_id}"
                    if len(rows) > request.page_size
                    else None
                )
                return ModuleResult(
                    status="ok",
                    data=RepositoryOutput(
                        records=records,
                        sources=[],
                        capsules=[],
                        source_metadata=[],
                        next_cursor=cursor,
                        read_watermark=account.memory_revision,
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def invalidate_source(self, db, account, event_id, revision):
        refs = [
            VersionRef(object_id=row.memory_id, version=row.version)
            for row in await db.scalars(
                select(FactSource)
                .join(
                    FactVersion,
                    and_(
                        FactSource.memory_id == FactVersion.memory_id,
                        FactSource.version == FactVersion.version,
                    ),
                )
                .where(
                    FactVersion.account_id == account.id,
                    FactSource.event_id == event_id,
                    FactSource.source_revision == revision,
                )
            )
        ]
        for ref in refs:
            await db.execute(
                update(FactVersion)
                .where(FactVersion.memory_id == ref.object_id, FactVersion.version == ref.version)
                .values(status="uncertain")
            )
        await invalidate(db, account.id, refs + [VersionRef(object_id=event_id, version=revision)])
        await db.execute(
            delete(Capsule).where(
                Capsule.account_id == account.id,
                Capsule.event_id == event_id,
                Capsule.source_revision == revision,
            )
        )
