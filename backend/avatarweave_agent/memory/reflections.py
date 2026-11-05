"""Revocable hypotheses and explicit user-authored confirmation, never silent promotion."""

import hashlib
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy import select

from .contracts import (
    CandidateMemory,
    CommitInput,
    ConflictInput,
    Contract,
    EvidenceRef,
    Hypothesis,
    Identifier,
    ScopedRequest,
    Statement,
    TimeWindow,
    VersionFence,
    VersionRef,
)
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .lifecycle import fact_key, rule_for
from .tables import ControlOperation, Dependency, DerivedView, FactVersion


class ReflectionSnapshot(Contract):
    ref: VersionRef
    hypothesis: Hypothesis
    state: Literal["needs_confirmation", "confirmed", "rejected"]
    confirmed_memory_ref: VersionRef | None = None


class PublicReflection(Contract):
    reflection_id: Identifier
    version: int
    text: str
    state: Literal["needs_confirmation", "confirmed", "rejected"]
    confirmed_memory_id: Identifier | None = None


class PublicReflectionPage(Contract):
    items: list[PublicReflection]
    next_cursor: Identifier | None


class ReflectionReviewBody(Contract):
    expected_version: int = Field(ge=1)
    action: Literal["confirm", "reject"]
    confirmation_text: Annotated[str, Field(max_length=2048)] = ""
    predicate: Literal["preference", "interaction_boundary"] = "preference"
    polarity: Literal["positive", "negative"] = "positive"
    idempotency_key: Identifier


def public_view(value):
    return PublicReflection(
        reflection_id=value.ref.object_id,
        version=value.ref.version,
        text=value.hypothesis.text,
        state=value.state,
        confirmed_memory_id=value.confirmed_memory_ref.object_id
        if value.confirmed_memory_ref
        else None,
    )


class ReflectionMixin:
    async def persist_reflections(self, request, output):
        saved = []
        async with self.database.sessions.begin() as db:
            account = await self._account(db, request.scope, lock=True)
            self._privacy(account, request)
            for index, hypothesis in enumerate(output.hypotheses):
                key = hashlib.sha256(
                    ("reflection:" + account.id + request.meta.operation_id + str(index)).encode()
                ).hexdigest()
                prior = await db.get(DerivedView, (key, 1))
                if prior:
                    if prior.state == "invalidated" or prior.kind != "reflection":
                        raise MemoryFault("version_conflict", "Hypothesis was invalidated")
                    saved.append(
                        ReflectionSnapshot.model_validate_json(prior.document_json).hypothesis
                    )
                    continue
                for ref in hypothesis.supports + hypothesis.counters:
                    row = await db.scalar(
                        select(FactVersion).where(
                            *fact_scope(request.scope),
                            FactVersion.memory_id == ref.object_id,
                            FactVersion.version == ref.version,
                            FactVersion.status == "active",
                            FactVersion.system_to.is_(None),
                        )
                    )
                    if row is None:
                        raise MemoryFault(
                            "version_conflict", "Hypothesis evidence changed during inference"
                        )
                value = ReflectionSnapshot(
                    ref=VersionRef(object_id=key, version=1),
                    hypothesis=hypothesis,
                    state="needs_confirmation",
                )
                saved.append(hypothesis)
                db.add(
                    DerivedView(
                        id=key,
                        version=1,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        kind="reflection",
                        state="pending_review",
                        document_json=value.model_dump_json(),
                    )
                )
                for parent in {
                    r.model_dump_json(): r for r in hypothesis.supports + hypothesis.counters
                }.values():
                    db.add(
                        Dependency(
                            account_id=account.id,
                            parent_id=parent.object_id,
                            parent_version=parent.version,
                            child_id=key,
                            child_version=1,
                            relation="inferred_from",
                        )
                    )
        return saved

    async def list_reflections(self, scope, page):
        async with self.database.sessions() as db:
            await self._account(db, scope)
            query = select(DerivedView).where(
                DerivedView.account_id == scope.user_id,
                DerivedView.companion_id == scope.companion_id,
                DerivedView.kind == "reflection",
                DerivedView.state.in_(["pending_review", "confirmed", "rejected"]),
            )
            if page.cursor:
                query = query.where(DerivedView.id > page.cursor)
            rows = list(await db.scalars(query.order_by(DerivedView.id).limit(page.limit + 1)))
            return PublicReflectionPage(
                items=[
                    public_view(ReflectionSnapshot.model_validate_json(r.document_json))
                    for r in rows[: page.limit]
                ],
                next_cursor=rows[page.limit - 1].id if len(rows) > page.limit else None,
            )

    async def review_reflection(self, scope, reflection_id, body):
        from .http import meta

        if body.action == "confirm" and not body.confirmation_text.strip():
            raise MemoryFault(
                "invalid_transition", "Write the preference or boundary you want to confirm"
            )
        event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="reflection_review",
            target=reflection_id,
            body=body,
            text=body.confirmation_text if body.action == "confirm" else "reject_reflection",
        )
        if receipt:
            return PublicReflection.model_validate_json(receipt)
        async with self.database.sessions() as db:
            account = await self._account(db, scope)
            row = await db.get(DerivedView, (reflection_id, body.expected_version))
            if (
                row is None
                or row.account_id != account.id
                or row.companion_id != scope.companion_id
                or row.kind != "reflection"
                or row.state == "invalidated"
            ):
                raise MemoryFault(
                    "version_conflict", "Hypothesis is unavailable or evidence changed"
                )
            value = ReflectionSnapshot.model_validate_json(row.document_json)
            if value.state != "needs_confirmation":
                raise MemoryFault("version_conflict", "Hypothesis was already reviewed")
            fence = VersionFence(
                privacy_epoch=account.privacy_epoch,
                memory_revision=account.memory_revision,
                owner_epoch=0,
            )
        confirmed = None
        if body.action == "confirm":
            candidate = CandidateMemory(
                candidate_id=hashlib.sha256(
                    (reflection_id + body.idempotency_key).encode()
                ).hexdigest(),
                namespace="life",
                content_kind="preference"
                if body.predicate == "preference"
                else "interaction_preference",
                assertion_kind="user_confirmed",
                interpretation="negated" if body.polarity == "negative" else "asserted",
                sensitivity="private",
                proposed_importance=0.6,
                display_text=body.confirmation_text,
                statement=Statement(
                    subject_id=account.id,
                    predicate=body.predicate,
                    value_type="text",
                    canonical_value=body.confirmation_text,
                    polarity=body.polarity,
                    valid_time=TimeWindow(precision="unknown"),
                ),
                sources=[
                    EvidenceRef(
                        event_id=event.event_id,
                        source_revision=1,
                        start_char=0,
                        end_char=len(event.content),
                        quote=event.content,
                        origin="user_ui",
                    )
                ],
            )
            async with self.database.sessions() as db:
                current = [
                    load_record(r)
                    for r in await self._comparable(db, scope, fact_key(scope, candidate))
                ]
            planned = await self.conflict(
                ConflictInput(
                    meta=meta(self),
                    scope=scope,
                    fence=fence,
                    candidate=candidate,
                    predicate_rule=rule_for(candidate),
                    comparable_records=current,
                )
            )
            if planned.error:
                raise MemoryFault(
                    planned.error.code, planned.error.message, retryable=planned.error.retryable
                )
            result = await self.commit(
                CommitInput(
                    meta=meta(self).model_copy(update={"operation_id": body.idempotency_key}),
                    scope=scope,
                    fence=fence,
                    candidate=candidate,
                    proposals=planned.data.proposals,
                    expected_refs=[r.ref for r in current],
                    user_confirmation_event=event.event_id,
                )
            )
            if result.error:
                raise MemoryFault(
                    result.error.code, result.error.message, retryable=result.error.retryable
                )
            confirmed = result.data.committed[-1]
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            self._privacy(account, ScopedRequest(meta=meta(self), scope=scope, fence=fence))
            row = await db.get(DerivedView, (reflection_id, body.expected_version))
            if row.state == "invalidated" and confirmed is None:
                raise MemoryFault(
                    "version_conflict", "Hypothesis evidence changed before rejection"
                )
            value = value.model_copy(
                update={
                    "state": "confirmed" if confirmed else "rejected",
                    "confirmed_memory_ref": confirmed,
                }
            )
            row.state, row.document_json = value.state, value.model_dump_json()
            output = public_view(value)
            control = await db.get(ControlOperation, (account.id, body.idempotency_key))
            control.result_json = output.model_dump_json()
            return output
