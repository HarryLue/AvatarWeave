"""Authenticated memory projections and explicit correction controls."""

import re
from uuid import uuid4

from sqlalchemy import select

from .contracts import (
    CandidateMemory,
    CommitInput,
    EvidenceRef,
    EvidenceView,
    MutationProposal,
    PublicMemoryView,
    TimeWindow,
    VersionRef,
)
from .execution import MemoryFault
from .facts import fact_scope, invalidate, load_record
from .lifecycle import fact_key
from .storage import Outbox, SourceRevision
from .tables import Capsule, ControlOperation, CorrectionTarget, FactVersion
from .utils import utc


class ManagementMixin:
    async def public_memory(self, scope, memory_id, *, include_quote=False, version=None):
        async with self.database.sessions() as db:
            await self._account(db, scope)
            query = select(FactVersion).where(
                *fact_scope(scope),
                FactVersion.memory_id == memory_id,
                FactVersion.record_json.is_not(None),
                FactVersion.status != "deleted",
            )
            query = (
                query.where(FactVersion.version == version)
                if version
                else query.where(FactVersion.system_to.is_(None))
            )
            row = await db.scalar(query.order_by(FactVersion.version.desc()).limit(1))
            if row is None:
                raise MemoryFault("not_found", "Memory not found")
            record = load_record(row)
            evidence = []
            for pointer in record.sources[:16]:
                source = await db.scalar(
                    select(SourceRevision).where(
                        SourceRevision.account_id == scope.user_id,
                        SourceRevision.companion_id == scope.companion_id,
                        SourceRevision.event_id == pointer.event_id,
                        SourceRevision.revision == pointer.source_revision,
                        SourceRevision.revoked.is_(False),
                    )
                )
                granularity, quote = "metadata_only", None
                if source is not None:
                    if source.source_json and utc(source.expires_at) > self.clock():
                        granularity = "raw"
                        if include_quote and pointer.start_char is not None:
                            from .contracts import SourceEvent

                            original = SourceEvent.model_validate_json(source.source_json)
                            quote = original.content[pointer.start_char : pointer.end_char]
                    elif pointer.capsule_id:
                        capsule = await db.scalar(
                            select(Capsule).where(
                                Capsule.id == pointer.capsule_id,
                                Capsule.account_id == scope.user_id,
                                Capsule.companion_id == scope.companion_id,
                            )
                        )
                        if capsule:
                            from .contracts import EvidenceCapsule

                            retained = EvidenceCapsule.model_validate_json(capsule.capsule_json)
                            granularity = "evidence_capsule"
                            quote = retained.evidence.quote if include_quote else None
                evidence.append(
                    EvidenceView(
                        source_label="你提供的信息",
                        occurred_at=TimeWindow(
                            start=utc(source.occurred_at) if source else None,
                            precision="instant" if source else "unknown",
                        ),
                        granularity=granularity,
                        quote=quote,
                    )
                )
            return PublicMemoryView(
                memory_id=record.ref.object_id,
                version=record.ref.version,
                kind=record.content_kind,
                text=record.display_text,
                valid_time=record.statement.valid_time,
                label={
                    "user_report": "user_report",
                    "user_confirmed": "confirmed",
                    "tool_observed": "user_report",
                    "model_hypothesis": "hypothesis",
                    "authored_fiction": "fiction",
                }[record.assertion_kind],
                pinned=record.user_pinned,
                evidence=evidence,
                state="uncertain"
                if record.status == "uncertain"
                else "historical"
                if record.statement.valid_time.end
                and record.statement.valid_time.end <= self.clock()
                else "current",
            )

    async def correct_memory(self, scope, memory_id, body):
        from .http import fence_for, meta

        # Check ownership before persisting even a control reference.
        view = await self.public_memory(scope, memory_id)
        event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="correct_memory",
            body=body,
            text=body.correction_text,
            target=memory_id,
        )
        if receipt:
            ref = VersionRef.model_validate_json(receipt)
            return await self.public_memory(scope, ref.object_id, version=ref.version), False
        from .contracts import CommitOutput
        from .tables import OperationReceipt

        async with self.database.sessions.begin() as db:
            saved = await db.get(OperationReceipt, (scope.user_id, body.idempotency_key))
            if saved and saved.operation == "commit":
                ref = CommitOutput.model_validate_json(saved.result_json).committed[-1]
                control = await db.get(ControlOperation, (scope.user_id, body.idempotency_key))
                control.result_json = ref.model_dump_json()
            else:
                ref = None
            pending = await db.get(CorrectionTarget, event.event_id)
        if ref:
            return await self.public_memory(scope, ref.object_id, version=ref.version), False
        if pending:
            return await self.public_memory(scope, memory_id), pending.state != "completed"
        if view.version != body.expected_version:
            raise MemoryFault("version_conflict", "Memory version changed")
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            row = await db.scalar(
                select(FactVersion).where(
                    *fact_scope(scope),
                    FactVersion.memory_id == memory_id,
                    FactVersion.version == body.expected_version,
                    FactVersion.system_to.is_(None),
                )
            )
            if row is None:
                raise MemoryFault("version_conflict", "Memory version changed")
            old = load_record(row)
            row.status = "uncertain"
            account.memory_revision += 1
            await invalidate(db, account.id, [old.ref])
            # Natural-language corrections are asynchronous and immediately suppress
            # the potentially incorrect old fact. Simple literal value edits need no model.
            literal = len(body.correction_text) <= 128 and not re.search(
                r"[。！？!?；;]|从未|不是|以前|搬到|纠正|我|从|不再|没", body.correction_text
            )
            if not literal:
                db.add(
                    CorrectionTarget(
                        event_id=event.event_id,
                        account_id=account.id,
                        companion_id=scope.companion_id,
                        memory_id=memory_id,
                        expected_version=old.ref.version,
                        state="pending",
                    )
                )
                await self._admit_control_outbox(db, account, scope, event)
            fence = fence_for(account)
        if not literal:
            return await self.public_memory(scope, memory_id), True
        candidate = CandidateMemory(
            candidate_id=event.event_id,
            namespace=old.namespace,
            content_kind=old.content_kind,
            assertion_kind="user_confirmed",
            interpretation="asserted",
            statement=old.statement.model_copy(update={"canonical_value": body.correction_text}),
            display_text=body.correction_text,
            sensitivity=old.sensitivity,
            proposed_importance=old.importance,
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
            rows = await self._comparable(db, scope, fact_key(scope, candidate))
            expected = [load_record(r).ref for r in rows]
        request_meta = meta(self)
        request_meta = request_meta.model_copy(update={"operation_id": body.idempotency_key})
        result = await self.commit(
            CommitInput(
                meta=request_meta,
                scope=scope,
                fence=fence,
                candidate=candidate,
                expected_refs=expected,
                user_confirmation_event=event.event_id,
                proposals=[
                    MutationProposal(
                        action="retract",
                        candidate_id=candidate.candidate_id,
                        target_refs=[old.ref],
                        new_statement=candidate.statement,
                        reason="Explicit user correction",
                        requires_user_confirmation=True,
                    )
                ],
            )
        )
        if result.error:
            raise MemoryFault(
                result.error.code, result.error.message, retryable=result.error.retryable
            )
        ref = result.data.committed[-1]
        async with self.database.sessions.begin() as db:
            control = await db.get(ControlOperation, (scope.user_id, body.idempotency_key))
            control.result_json = ref.model_dump_json()
        return await self.public_memory(scope, memory_id), False

    async def _admit_control_outbox(self, db, account, scope, event, *, confirmation=True):
        from sqlalchemy import func, update

        from .storage import QueueGuard

        await db.execute(
            update(QueueGuard).where(QueueGuard.id == 1).values(revision=QueueGuard.revision + 1)
        )
        # A separate bounded reserve prevents ordinary extraction backlog from
        # starving explicit corrections. Both classes count toward disk capacity.
        count = await db.scalar(
            select(func.count())
            .select_from(CorrectionTarget)
            .where(CorrectionTarget.account_id == account.id, CorrectionTarget.state == "pending")
        )
        if count >= 32:
            raise MemoryFault("overloaded", "Correction queue is full", retryable=True)
        total, size, _, _ = await self.queue_usage(db, account.id)
        if (
            total >= self.max_outbox + 128
            or size + len(event.model_dump_json().encode()) > self.max_payload_bytes
        ):
            raise MemoryFault("overloaded", "Correction capacity is exhausted", retryable=True)
        db.add(
            Outbox(
                id=uuid4().hex,
                account_id=account.id,
                companion_id=scope.companion_id,
                event_id=event.event_id,
                source_revision=event.revision,
                privacy_epoch=account.privacy_epoch,
                payload_bytes=len(event.model_dump_json().encode()),
                created_at=self.clock(),
                available_at=self.clock(),
            )
        )
        source = await db.get(SourceRevision, (event.event_id, event.revision))
        source.state, source.structured_settled, source.index_settled = (
            "needs_confirmation" if confirmation else "persisted",
            False,
            False,
        )
        await db.flush()
        await self.advance_watermarks(db, account)

    async def correct_transcript(self, scope, body):
        from datetime import timedelta

        from sqlalchemy import update

        from .contracts import PrivacyPreference, PublicEventProcessing, SourceEvent
        from .storage import EventHead

        _control_event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="correct_transcript",
            body=body,
            text=body.corrected_text,
            target=body.source_event_id,
        )
        if receipt:
            return PublicEventProcessing.model_validate_json(receipt)
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            if not prefs.long_term_memory_enabled:
                raise MemoryFault("privacy_changed", "Memory saving is disabled")
            old = await self._source(db, scope, body.source_event_id, body.expected_source_revision)
            if (
                old.origin not in {"user_text", "user_transcript", "user_ui"}
                or old.source_json is None
            ):
                raise MemoryFault("invalid_transition", "Transcript source is unavailable")
            previous = SourceEvent.model_validate_json(old.source_json)
            event = previous.model_copy(
                update={
                    "revision": old.revision + 1,
                    "origin": "user_ui",
                    "content": body.corrected_text,
                    "received_at": self.clock(),
                    "supersedes": VersionRef(object_id=old.event_id, version=old.revision),
                }
            )
            await self.invalidate_source(db, account, old.event_id, old.revision)
            old.revoked, old.source_json, old.state = True, None, "rejected"
            old.structured_settled = old.index_settled = True
            await db.execute(
                update(Outbox)
                .where(Outbox.event_id == old.event_id, Outbox.source_revision == old.revision)
                .values(state="obsolete")
            )
            head = await db.get(EventHead, old.event_id)
            head.revision = event.revision
            account.memory_revision += 1
            account.source_watermark += 1
            db.add(
                SourceRevision(
                    event_id=event.event_id,
                    revision=event.revision,
                    account_id=account.id,
                    companion_id=scope.companion_id,
                    session_id=event.session_id,
                    sequence=account.source_watermark,
                    source_json=event.model_dump_json(),
                    origin="user_ui",
                    occurred_at=event.occurred_at,
                    expires_at=self.clock() + timedelta(days=prefs.raw_transcript_days),
                    state="persisted",
                    structured_settled=False,
                    index_settled=False,
                    revoked=False,
                )
            )
            await db.flush()
            await self._admit_control_outbox(db, account, scope, event, confirmation=False)
            result = PublicEventProcessing(
                event_id=event.event_id,
                source_revision=event.revision,
                source_saved=True,
                usable_in_this_session=True,
                cross_session_source_visible=True,
                structured_state="pending",
                index_state="pending",
                provisional=True,
                observed_at=self.clock(),
                retry_after_ms=500,
            )
            control = await db.get(ControlOperation, (account.id, body.idempotency_key))
            control.result_json = result.model_dump_json()
            return result
