"""P1: server-scoped sessions, revisioned final input and honest visibility.

No inference runs inside a transaction. Outbox rows contain references only.
P2+ consumers may settle rows but cannot turn source persistence into a fact.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update

from .archive import ArchiveMixin
from .context import ContextMixin
from .contracts import (
    InputEventInput,
    InputEventOutput,
    MemoryVisibility,
    ModuleResult,
    PrivacyPreference,
    RepositoryInput,
    RepositoryOutput,
    Scope,
    SessionInput,
    SessionOutput,
    SourceDescriptor,
    SourceEvent,
    TargetEventState,
    VisibilityReadInput,
    VisibilityReadOutput,
)
from .controls import ControlMixin
from .embedding import EmbeddingMixin
from .entities import EntityMixin
from .event_updates import EventUpdateMixin
from .execution import Executor, MemoryFault
from .facts import FactMixin
from .jobs import JobMixin
from .maintenance import MaintenanceMixin
from .management import ManagementMixin
from .operations import MigrationMixin
from .organizer import OrganizerMixin
from .output import OutputMixin
from .portability import PortabilityMixin
from .privacy import PrivacyMixin
from .recall import RecallMixin
from .reflections import ReflectionMixin
from .scheduler import SchedulerMixin
from .storage import (
    Account,
    EventHead,
    MemoryDatabase,
    MemorySession,
    Outbox,
    QueueGuard,
    SessionReceipt,
    SourceRevision,
)


def utc(value):
    # SQLite drops offset information; stored timestamps are always UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def default_preferences():
    return PrivacyPreference(
        long_term_memory_enabled=False, permitted_shared_predicates=[], preference_version=1
    )


class MemoryService(
    EventUpdateMixin,
    FactMixin,
    PrivacyMixin,
    ControlMixin,
    JobMixin,
    ManagementMixin,
    EmbeddingMixin,
    RecallMixin,
    ContextMixin,
    OutputMixin,
    OrganizerMixin,
    MaintenanceMixin,
    ArchiveMixin,
    EntityMixin,
    PortabilityMixin,
    MigrationMixin,
    SchedulerMixin,
    ReflectionMixin,
):
    def __init__(
        self,
        database: MemoryDatabase,
        *,
        companions: set[str],
        workspace_id="default",
        clock=None,
        max_outbox=10000,
        max_user_outbox=256,
        max_payload_bytes=16 * 1024 * 1024,
    ):
        self.database = database
        self.companions = companions
        self.workspace_id = workspace_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.executor = Executor(clock=self.clock)
        self.max_outbox = max_outbox
        self.max_user_outbox = max_user_outbox
        self.max_payload_bytes = max_payload_bytes

    async def resolve_account(self, issuer: str, subject: str) -> str:
        """Only call after the OIDC library validates the ID token; never from a body.

        Mapping uses issuer/sub, never email, name, voice or room. Upsert makes
        simultaneous first logins converge on the same stable internal identity.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert = sqlite_insert if self.database.engine.dialect.name == "sqlite" else pg_insert
        async with self.database.sessions.begin() as db:
            await db.execute(
                insert(Account)
                .values(
                    id=uuid4().hex,
                    workspace_id=self.workspace_id,
                    issuer=issuer,
                    subject=subject,
                    preferences_json=default_preferences().model_dump_json(),
                )
                .on_conflict_do_nothing(index_elements=["workspace_id", "issuer", "subject"])
            )
            return await db.scalar(
                select(Account.id).where(
                    Account.workspace_id == self.workspace_id,
                    Account.issuer == issuer,
                    Account.subject == subject,
                )
            )

    async def _account(self, db, scope: Scope, *, lock=False):
        from .tables import AccountBarrier

        if await db.get(AccountBarrier, scope.user_id):
            raise MemoryFault("privacy_changed", "Account data is being deleted")
        if scope.workspace_id != self.workspace_id or scope.companion_id not in self.companions:
            raise MemoryFault("not_found", "Account or companion not found")
        query = select(Account).where(
            Account.id == scope.user_id, Account.workspace_id == scope.workspace_id
        )
        if lock:
            # Acquires a write lock in both PostgreSQL and SQLite. This is a
            # short source/control transaction, never an inference lifetime lock.
            await db.execute(
                update(Account)
                .where(Account.id == scope.user_id, Account.workspace_id == scope.workspace_id)
                .values(source_watermark=Account.source_watermark)
            )
        row = await db.scalar(query.execution_options(populate_existing=True))
        if lock and await db.get(AccountBarrier, scope.user_id, populate_existing=True):
            raise MemoryFault("privacy_changed", "Account data is being deleted")
        if row is None:
            raise MemoryFault("not_found", "Account not found")
        if lock:
            from .job_context import verify_job_lease

            await verify_job_lease(db, row.id, self.clock())
        return row

    @staticmethod
    def _privacy(account, request):
        if account.privacy_epoch != request.fence.privacy_epoch:
            raise MemoryFault("privacy_changed", "Memory authorization changed")

    async def _session(self, db, scope, session_id):
        row = await db.scalar(
            select(MemorySession).where(
                MemorySession.id == session_id,
                MemorySession.account_id == scope.user_id,
                MemorySession.companion_id == scope.companion_id,
            )
        )
        if row is None:
            raise MemoryFault("not_found", "Session not found")
        return row

    async def _session_slot_count(self, db, account_id, *, exclude=None):
        now = self.clock()
        query = (
            select(func.count())
            .select_from(MemorySession)
            .where(
                MemorySession.account_id == account_id,
                MemorySession.status != "ended",
                or_(
                    MemorySession.lease_until > now,
                    # Existing schema-v3 rows may predate explicit connection reservations.
                    (MemorySession.status == "ready")
                    & MemorySession.lease_until.is_(None)
                    & (MemorySession.created_at > now - timedelta(minutes=2)),
                ),
            )
        )
        if exclude:
            query = query.where(MemorySession.id != exclude)
        return await db.scalar(query)

    async def _reserve_session_slot(self, db, row):
        now = self.clock()
        if row.lease_until and utc(row.lease_until) > now:
            return  # A live owner/reservation already occupies this slot.
        if await self._session_slot_count(db, row.account_id, exclude=row.id) >= 2:
            raise MemoryFault("quota_exceeded", "Two active sessions allowed", retryable=True)
        # Retain the session and its sources for recovery. An expired worker
        # cannot renew after losing owner_id, or publish after resume cancels it.
        row.owner_id = None
        row.lease_until = now + timedelta(minutes=2)
        # An old room may still exist without a running dispatch. Rotate only
        # the transport; the stable session ID and all source ownership remain.
        row.room = "vm-" + uuid4().hex
        row.participant = "vm-user-" + uuid4().hex
        row.status = "reconnecting" if row.owner_epoch else "ready"

    async def session(self, request: SessionInput) -> ModuleResult[SessionOutput]:
        async def operation():
            if request.idempotency_key.startswith("__"):
                raise MemoryFault("invalid_transition", "Reserved session operation key")
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                if request.operation != "create":
                    row = await self._session(db, request.scope, request.session_id)
                    receipt = await db.get(SessionReceipt, (account.id, request.idempotency_key))
                    request_json = request.model_dump_json(
                        include={"operation", "session_id", "capabilities", "memory_enabled"}
                    )
                    if receipt:
                        if receipt.request_json != request_json:
                            raise MemoryFault("version_conflict", "Idempotency key already used")
                    else:
                        if request.operation == "end":
                            row.status = "ended"
                            row.owner_epoch += 1
                            row.lease_until = None
                            row.owner_id = None
                        elif request.operation == "resume":
                            if row.status == "ended":
                                raise MemoryFault(
                                    "invalid_transition", "Ended session cannot resume"
                                )
                            await self._reserve_session_slot(db, row)
                            # An active Worker is not displaced by a browser reconnect.
                            row.capabilities_json = request.capabilities.model_dump_json()
                            from .tables import Generation, OutputLedger

                            await db.execute(
                                update(Generation)
                                .where(Generation.session_id == row.id)
                                .values(cancelled=True)
                            )
                            await db.execute(
                                update(OutputLedger)
                                .where(
                                    OutputLedger.session_id == row.id,
                                    OutputLedger.state.not_in(
                                        ["completed", "cancelled", "failed", "unknown"]
                                    ),
                                )
                                .values(state="cancelled")
                            )
                        else:
                            raise MemoryFault(
                                "unsupported_capability", "Use trusted owner lease adapter"
                            )
                        db.add(
                            SessionReceipt(
                                account_id=account.id,
                                idempotency_key=request.idempotency_key,
                                operation=request.operation,
                                session_id=row.id,
                                request_json=request_json,
                            )
                        )
                    return ModuleResult(
                        status="ok",
                        data=SessionOutput(
                            session_id=row.id,
                            room_name=row.room,
                            participant_identity=row.participant,
                            owner_epoch=row.owner_epoch,
                            status=row.status,
                            rtc_credential_ref=None,
                            resume_watermark=account.source_watermark,
                        ),
                    )
                if request.session_id is not None:
                    raise MemoryFault("invalid_transition", "Create must not supply a session ID")
                row = await db.scalar(
                    select(MemorySession).where(
                        MemorySession.account_id == account.id,
                        MemorySession.idempotency_key == request.idempotency_key,
                    )
                )
                capabilities = request.capabilities.model_dump_json()
                if row:
                    if (
                        row.companion_id != request.scope.companion_id
                        or row.memory_enabled != request.memory_enabled
                        or row.capabilities_json != capabilities
                    ):
                        raise MemoryFault("version_conflict", "Idempotency key already used")
                    if row.status != "ended":
                        await self._reserve_session_slot(db, row)
                else:
                    prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                    if request.memory_enabled and not prefs.long_term_memory_enabled:
                        raise MemoryFault("invalid_transition", "Enable memory preferences first")
                    count = await self._session_slot_count(db, account.id)
                    if count >= 2:
                        raise MemoryFault(
                            "quota_exceeded", "Two active sessions allowed", retryable=True
                        )
                    row = MemorySession(
                        id=uuid4().hex,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        room="vm-" + uuid4().hex,
                        participant="vm-user-" + uuid4().hex,
                        memory_enabled=request.memory_enabled,
                        status="ready",
                        capabilities_json=capabilities,
                        idempotency_key=request.idempotency_key,
                        owner_epoch=0,
                        lease_until=self.clock() + timedelta(minutes=2),
                        created_at=self.clock(),
                    )
                    db.add(row)
                    await db.flush()
                return ModuleResult(
                    status="ok",
                    data=SessionOutput(
                        session_id=row.id,
                        room_name=row.room,
                        participant_identity=row.participant,
                        owner_epoch=row.owner_epoch,
                        status=row.status,
                        rtc_credential_ref=None,
                        resume_watermark=account.source_watermark,
                    ),
                )

        return await self.executor.run(request, operation)

    async def claim_owner(self, scope: Scope, session_id: str, room: str, worker_id: str):
        """Trusted dispatch adapter only. A session reference is not authentication."""
        async with self.database.sessions.begin() as db:
            await self._account(db, scope, lock=True)
            row = await self._session(db, scope, session_id)
            now = self.clock()
            if row.room != room or row.status == "ended":
                raise MemoryFault("not_found", "Session not available")
            if row.owner_id is not None and row.lease_until and utc(row.lease_until) > now:
                if row.owner_id == worker_id:
                    return row.owner_epoch
                raise MemoryFault("version_conflict", "Session already has an owner")
            if await self._session_slot_count(db, scope.user_id, exclude=row.id) >= 2:
                raise MemoryFault("quota_exceeded", "Two active sessions allowed", retryable=True)
            row.owner_epoch += 1
            row.owner_id = worker_id
            row.lease_until = now + timedelta(seconds=30)
            row.status = "active"
            return row.owner_epoch

    async def renew_owner(self, scope: Scope, session_id: str, worker_id: str, owner_epoch: int):
        async with self.database.sessions.begin() as db:
            await self._account(db, scope, lock=True)
            row = await self._session(db, scope, session_id)
            if (
                row.status != "active"
                or row.owner_id != worker_id
                or row.owner_epoch != owner_epoch
                or row.lease_until is None
                or utc(row.lease_until) <= self.clock()
            ):
                raise MemoryFault("stale_generation", "Owner lease is no longer valid")
            row.lease_until = self.clock() + timedelta(seconds=30)

    async def release_owner(self, scope, session_id, worker_id, owner_epoch):
        async with self.database.sessions.begin() as db:
            await self._account(db, scope, lock=True)
            row = await self._session(db, scope, session_id)
            if row.owner_id == worker_id and row.owner_epoch == owner_epoch:
                row.owner_id, row.lease_until = None, None
                if row.status != "ended":
                    row.status = "reconnecting"

    async def expire_sessions(self, batch_size=128):
        from sqlalchemy import or_

        cutoff = self.clock() - timedelta(days=1)
        async with self.database.sessions.begin() as db:
            rows = list(
                await db.scalars(
                    select(MemorySession)
                    .join(Account, Account.id == MemorySession.account_id)
                    .where(
                        Account.workspace_id == self.workspace_id,
                        MemorySession.status != "ended",
                        or_(
                            (MemorySession.lease_until.is_(None))
                            & (MemorySession.created_at <= cutoff),
                            MemorySession.lease_until <= cutoff,
                        ),
                    )
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.status, row.owner_id, row.lease_until = "ended", None, None
                row.owner_epoch += 1
            return len(rows)

    async def input_event(self, request: InputEventInput) -> ModuleResult[InputEventOutput]:
        async def operation():
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                session = await self._session(db, request.scope, request.session_id)
                now = self.clock()
                if (
                    session.status != "active"
                    or session.owner_epoch != request.fence.owner_epoch
                    or session.lease_until is None
                    or utc(session.lease_until) <= now
                ):
                    raise MemoryFault("stale_generation", "Session owner is no longer valid")
                event = request.event
                if event.scope != request.scope or event.session_id != session.id:
                    raise MemoryFault("scope_mismatch", "Input does not match authorized session")
                if not event.is_final or event.origin not in {
                    "user_text",
                    "user_transcript",
                    "user_ui",
                }:
                    raise MemoryFault("invalid_transition", "Only final user input is accepted")
                if request.memory_enabled != session.memory_enabled:
                    raise MemoryFault("privacy_changed", "Session memory setting changed")
                prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                if not session.memory_enabled or not prefs.long_term_memory_enabled:
                    # No receipt, source, hash, payload or task is persisted.
                    visibility = self._visibility(account, session.id, [], [])
                    return ModuleResult(
                        status="ok",
                        data=InputEventOutput(
                            turn_id=event.turn_id,
                            accepted_revision=event.revision,
                            persisted=False,
                            input_watermark=account.source_watermark,
                            outbox_ids=[],
                            cancelled_generation_ids=[],
                            visibility=visibility,
                        ),
                        visibility=visibility,
                    )
                head = await db.get(EventHead, event.event_id)
                if head and head.session_id != session.id:
                    raise MemoryFault("not_found", "Event not found")
                existing = await db.get(SourceRevision, (event.event_id, event.revision))
                if existing:
                    if (
                        head.revision != event.revision
                        or existing.revoked
                        or existing.source_json is None
                        or SourceEvent.model_validate_json(existing.source_json) != event
                    ):
                        raise MemoryFault("version_conflict", "Input revision already used")
                    if request.expected_turn_revision != event.revision - 1:
                        raise MemoryFault("version_conflict", "Unexpected input revision")
                    visibility = self._visibility(account, session.id, [existing], [event.event_id])
                    return ModuleResult(
                        status="ok",
                        data=InputEventOutput(
                            turn_id=event.turn_id,
                            accepted_revision=event.revision,
                            persisted=True,
                            input_watermark=existing.sequence,
                            outbox_ids=[],
                            cancelled_generation_ids=[],
                            visibility=visibility,
                        ),
                        visibility=visibility,
                    )
                current = head.revision if head else 0
                if head is None and await db.scalar(
                    select(EventHead).where(
                        EventHead.session_id == session.id, EventHead.turn_id == event.turn_id
                    )
                ):
                    raise MemoryFault("version_conflict", "Turn already has a source event")
                if current != request.expected_turn_revision or event.revision != current + 1:
                    raise MemoryFault("version_conflict", "Source revision changed")
                if head:
                    if (
                        head.turn_id != event.turn_id
                        or event.supersedes is None
                        or event.supersedes.object_id != event.event_id
                        or event.supersedes.version != current
                    ):
                        raise MemoryFault(
                            "version_conflict", "Revision must supersede current source"
                        )
                elif event.supersedes is not None:
                    raise MemoryFault(
                        "version_conflict", "First source cannot supersede another event"
                    )
                # One bounded reference-only outbox; do not hide overflow in a
                # second unbounded queue. Global guard makes admission atomic.
                await db.execute(
                    update(QueueGuard)
                    .where(QueueGuard.id == 1)
                    .values(revision=QueueGuard.revision + 1)
                )
                count, size, oldest, user_count = await self.queue_usage(db, account.id)
                payload_bytes = len(event.model_dump_json().encode())
                if (
                    count >= self.max_outbox
                    or user_count >= self.max_user_outbox
                    or size + payload_bytes > self.max_payload_bytes
                    or (oldest and (now - utc(oldest)).total_seconds() > 300)
                ):
                    raise MemoryFault(
                        "overloaded", "Memory processing queue is full", retryable=True
                    )
                if head:
                    old = await db.get(SourceRevision, (event.event_id, current))
                    await self.invalidate_source(db, account, event.event_id, current)
                    old.revoked = True
                    old.structured_settled = old.index_settled = True
                    old.state = "rejected"
                    await db.execute(
                        update(Outbox)
                        .where(Outbox.event_id == event.event_id, Outbox.source_revision == current)
                        .values(state="obsolete")
                    )
                    # Revision invalidates every in-flight snapshot. P2 must
                    # additionally invalidate derivatives in this transaction.
                    account.memory_revision += 1
                    head.revision = event.revision
                else:
                    head = EventHead(
                        event_id=event.event_id,
                        session_id=session.id,
                        turn_id=event.turn_id,
                        revision=event.revision,
                    )
                    db.add(head)
                    await db.flush()
                account.source_watermark += 1
                source = SourceRevision(
                    event_id=event.event_id,
                    revision=event.revision,
                    account_id=account.id,
                    companion_id=session.companion_id,
                    session_id=session.id,
                    sequence=account.source_watermark,
                    source_json=event.model_dump_json(),
                    origin=event.origin,
                    occurred_at=event.occurred_at,
                    expires_at=now + timedelta(days=prefs.raw_transcript_days),
                    state="persisted",
                    structured_settled=False,
                    index_settled=False,
                    revoked=False,
                )
                job_id = uuid4().hex
                db.add(source)
                await self.control_barrier(db, account, request.scope, event)
                db.add(
                    Outbox(
                        id=job_id,
                        account_id=account.id,
                        companion_id=session.companion_id,
                        event_id=event.event_id,
                        source_revision=event.revision,
                        privacy_epoch=account.privacy_epoch,
                        payload_bytes=payload_bytes,
                        created_at=now,
                        available_at=now,
                    )
                )
                await db.flush()
                await self.advance_watermarks(db, account)
                visibility = self._visibility(account, session.id, [source], [event.event_id])
                return ModuleResult(
                    status="ok",
                    data=InputEventOutput(
                        turn_id=event.turn_id,
                        accepted_revision=event.revision,
                        persisted=True,
                        input_watermark=account.source_watermark,
                        outbox_ids=[job_id],
                        cancelled_generation_ids=[],
                        visibility=visibility,
                    ),
                    visibility=visibility,
                )

        return await self.executor.run(request, operation)

    async def advance_watermarks(self, db, account):
        """Use the first unsettled sequence; a later completed item never jumps a hole."""
        for flag, attr in [
            (SourceRevision.structured_settled, "structured_watermark"),
            (SourceRevision.index_settled, "index_watermark"),
        ]:
            gap = await db.scalar(
                select(func.min(SourceRevision.sequence)).where(
                    SourceRevision.account_id == account.id, flag.is_(False)
                )
            )
            setattr(account, attr, gap - 1 if gap is not None else account.source_watermark)

    def _visibility(self, account, session_id, rows, overlay):
        return MemoryVisibility(
            source_watermark=account.source_watermark,
            structured_settled_watermark=account.structured_watermark,
            index_settled_watermark=account.index_watermark,
            overlay_session_id=session_id,
            overlay_event_ids=overlay,
            target_states=[
                TargetEventState(
                    event_id=r.event_id, revision=r.revision, state=r.state, updated_at=self.clock()
                )
                for r in rows
            ],
            observed_at=self.clock(),
        )

    async def visibility(self, request: VisibilityReadInput) -> ModuleResult[VisibilityReadOutput]:
        async def operation():
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                session = await self._session(db, request.scope, request.session_id)
                # SQL-side scope filtering; never fetch all users then filter in Python.
                rows = list(
                    await db.scalars(
                        select(SourceRevision)
                        .join(
                            EventHead,
                            (EventHead.event_id == SourceRevision.event_id)
                            & (EventHead.revision == SourceRevision.revision),
                        )
                        .where(
                            SourceRevision.account_id == account.id,
                            SourceRevision.companion_id == request.scope.companion_id,
                            SourceRevision.event_id.in_(request.target_event_ids),
                            SourceRevision.revoked.is_(False),
                        )
                    )
                )
                if len(rows) != len(set(request.target_event_ids)):
                    raise MemoryFault("not_found", "One or more events are unavailable")
                readable = [
                    r.event_id
                    for r in rows
                    if r.source_json is not None and utc(r.expires_at) > self.clock()
                ]
                structured = [
                    r.event_id
                    for r in rows
                    if r.state in {"structured", "indexed"}
                    or (r.state == "failed" and r.structured_settled)
                ]
                indexed = [r.event_id for r in rows if r.state == "indexed"]
                overlay = [
                    r.event_id
                    for r in rows
                    if r.session_id == session.id and r.event_id in readable
                ][:32]
                visibility = self._visibility(account, session.id, rows, overlay)
                return ModuleResult(
                    status="ok",
                    data=VisibilityReadOutput(
                        visibility=visibility,
                        source_readable_event_ids=readable,
                        structured_ready_event_ids=structured,
                        indexed_event_ids=indexed,
                        unresolved_event_ids=[
                            r.event_id
                            for r in rows
                            if r.state
                            in {"persisted", "provisional", "needs_confirmation", "failed"}
                        ],
                        retry_after_ms=500 if len(structured) < len(rows) else None,
                    ),
                    visibility=visibility,
                )

        return await self.executor.run(request, operation)

    async def repository(self, request: RepositoryInput) -> ModuleResult[RepositoryOutput]:
        if request.operation != "read_sources":
            return await self.fact_repository(request)

        async def operation():
            if request.cursor is not None or len(request.refs) > request.page_size:
                raise MemoryFault("budget_exceeded", "Batch exact source reads to page_size")
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                sources, descriptors, capsules = [], [], []
                from .contracts import EvidenceCapsule
                from .tables import Capsule

                for ref in request.refs:
                    row = await db.scalar(
                        select(SourceRevision)
                        .join(
                            EventHead,
                            (EventHead.event_id == SourceRevision.event_id)
                            & (EventHead.revision == SourceRevision.revision),
                        )
                        .where(
                            SourceRevision.event_id == ref.object_id,
                            SourceRevision.revision == ref.version,
                            SourceRevision.account_id == account.id,
                            SourceRevision.companion_id == request.scope.companion_id,
                            SourceRevision.revoked.is_(False),
                        )
                    )
                    if row is None:
                        raise MemoryFault("not_found", "Source unavailable")
                    available = row.source_json is not None and utc(row.expires_at) > self.clock()
                    if available:
                        source = SourceEvent.model_validate_json(row.source_json)
                        sources.append(source)
                        origin = source.origin
                    else:
                        origin = row.origin
                    retained = list(
                        await db.scalars(
                            select(Capsule)
                            .where(
                                Capsule.account_id == account.id,
                                Capsule.companion_id == request.scope.companion_id,
                                Capsule.event_id == row.event_id,
                                Capsule.source_revision == row.revision,
                            )
                            .limit(1001)
                        )
                    )
                    if len(retained) + len(capsules) > 1000:
                        raise MemoryFault("budget_exceeded", "Capsule batch requires another page")
                    capsules.extend(
                        EvidenceCapsule.model_validate_json(c.capsule_json).model_copy(
                            update={"original_source_available": available}
                        )
                        for c in retained
                    )
                    descriptors.append(
                        SourceDescriptor(
                            event_id=row.event_id,
                            revision=row.revision,
                            scope=request.scope,
                            session_id=row.session_id,
                            origin=origin,
                            occurred_at=utc(row.occurred_at),
                            raw_available=available,
                            capsule_ids=[c.id for c in retained],
                            revoked=False,
                        )
                    )
                visibility = self._visibility(account, None, [], [])
                return ModuleResult(
                    status="ok",
                    data=RepositoryOutput(
                        records=[],
                        sources=sources,
                        capsules=capsules,
                        source_metadata=descriptors,
                        next_cursor=None,
                        read_watermark=account.memory_revision,
                    ),
                    visibility=visibility,
                )

        return await self.executor.run(request, operation)

    async def expire_raw_batch(self, *, batch_size=128):
        """P1 maintenance primitive; no implicit full scan or retention of expired text."""
        if not 1 <= batch_size <= 1000:
            raise ValueError("Invalid batch size")
        async with self.database.sessions.begin() as db:
            rows = list(
                await db.scalars(
                    select(SourceRevision)
                    .join(Account, Account.id == SourceRevision.account_id)
                    .where(
                        Account.workspace_id == self.workspace_id,
                        SourceRevision.expires_at <= self.clock(),
                        SourceRevision.source_json.is_not(None),
                    )
                    .order_by(SourceRevision.expires_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                row.source_json = None
                from .tables import DerivedView

                jobs = list(
                    await db.scalars(
                        select(Outbox.id).where(
                            Outbox.event_id == row.event_id, Outbox.source_revision == row.revision
                        )
                    )
                )
                if jobs:
                    await db.execute(
                        update(DerivedView)
                        .where(
                            DerivedView.account_id == row.account_id,
                            DerivedView.id.in_([key + ":extracted" for key in jobs]),
                        )
                        .values(state="invalidated", document_json=None)
                    )
            return len(rows)
