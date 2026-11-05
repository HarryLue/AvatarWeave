"""Authenticated UI controls, their minimal durable sources and idempotency."""

import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from .contracts import ClientCapabilities, Contract, SourceEvent
from .execution import MemoryFault
from .storage import EventHead, MemorySession, SourceRevision
from .tables import ControlOperation


class ControlMixin:
    async def control_event(
        self, scope, *, key: str, operation: str, body: Contract, text: str, target: str = ""
    ):
        if not text or len(text) > 16000:
            raise MemoryFault("invalid_transition", "Control source is invalid")
        fingerprint = hashlib.sha256((target + "\0" + body.model_dump_json()).encode()).hexdigest()
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            existing = await db.get(ControlOperation, (account.id, key))
            if existing:
                if (
                    existing.companion_id != scope.companion_id
                    or existing.operation != operation
                    or existing.request_fingerprint != fingerprint
                ):
                    raise MemoryFault("version_conflict", "Idempotency key already used")
                source = await db.get(SourceRevision, (existing.event_id, 1))
                if source is None or source.revoked or source.source_json is None:
                    raise MemoryFault("not_found", "Control source is unavailable")
                return SourceEvent.model_validate_json(source.source_json), existing.result_json
            # A dedicated, non-RTC, ended session cannot consume an active-session slot.
            session = await db.scalar(
                select(MemorySession).where(
                    MemorySession.account_id == account.id,
                    MemorySession.idempotency_key == "__control__:" + scope.companion_id,
                )
            )
            if session is None:
                session = MemorySession(
                    id=uuid4().hex,
                    account_id=account.id,
                    companion_id=scope.companion_id,
                    room="vm-control-" + uuid4().hex,
                    participant="vm-control-" + uuid4().hex,
                    memory_enabled=False,
                    status="ended",
                    owner_epoch=0,
                    created_at=self.clock(),
                    capabilities_json=ClientCapabilities(
                        command_versions=[1],
                        action_cancel=False,
                        action_completed_receipt=False,
                        playback_granularity="none",
                        media_clock_supported=False,
                    ).model_dump_json(),
                    idempotency_key="__control__:" + scope.companion_id,
                )
                db.add(session)
                await db.flush()
            from .contracts import PrivacyPreference

            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            now = self.clock()
            event = SourceEvent(
                event_id=uuid4().hex,
                revision=1,
                session_id=session.id,
                turn_id=uuid4().hex,
                scope=scope,
                origin="user_ui",
                content=text,
                occurred_at=now,
                received_at=now,
                is_final=True,
            )
            db.add(
                EventHead(
                    event_id=event.event_id,
                    session_id=session.id,
                    turn_id=event.turn_id,
                    revision=1,
                )
            )
            await db.flush()
            account.source_watermark += 1
            db.add(
                SourceRevision(
                    event_id=event.event_id,
                    revision=1,
                    account_id=account.id,
                    companion_id=scope.companion_id,
                    session_id=session.id,
                    sequence=account.source_watermark,
                    source_json=event.model_dump_json(),
                    origin="user_ui",
                    occurred_at=now,
                    expires_at=now + timedelta(days=prefs.raw_transcript_days),
                    state="rejected",
                    structured_settled=True,
                    index_settled=True,
                    revoked=False,
                )
            )
            db.add(
                ControlOperation(
                    account_id=account.id,
                    idempotency_key=key,
                    companion_id=scope.companion_id,
                    event_id=event.event_id,
                    operation=operation,
                    request_fingerprint=fingerprint,
                )
            )
            await db.flush()
            await self.advance_watermarks(db, account)
            return event, None
