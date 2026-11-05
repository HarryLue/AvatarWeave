"""M16 generation ownership and honest output execution ledger."""

from uuid import uuid4

from sqlalchemy import delete, select, update

from .contracts import ModuleResult, OutputOutput, VersionFence
from .execution import MemoryFault
from .tables import Generation, OutputLedger
from .utils import utc

TERMINAL = {"completed", "interrupted", "cancelled", "failed", "unknown"}
TRANSITIONS = {
    "published": {"accepted", "started", "failed", "cancelled", "unknown"},
    "started": {"partial", "completed", "failed", "cancelled", "unknown"},
    "partial": {"partial", "completed", "failed", "cancelled", "unknown"},
}


class OutputMixin:
    async def begin_generation(self, scope, session_id, owner_epoch):
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            session = await self._session(db, scope, session_id)
            if (
                session.owner_epoch != owner_epoch
                or session.status != "active"
                or not session.lease_until
                or utc(session.lease_until) <= self.clock()
            ):
                raise MemoryFault("stale_generation", "Worker ownership expired")
            current = await db.get(Generation, session_id)
            identifier = uuid4().hex
            if current is None:
                current = Generation(
                    session_id=session_id,
                    generation_id=identifier,
                    privacy_epoch=account.privacy_epoch,
                    memory_revision=account.memory_revision,
                    owner_epoch=owner_epoch,
                    cancelled=False,
                    serial=1,
                )
                db.add(current)
            else:
                await db.execute(
                    update(OutputLedger)
                    .where(
                        OutputLedger.session_id == session_id, OutputLedger.state.not_in(TERMINAL)
                    )
                    .values(state="cancelled")
                )
                current.generation_id, current.privacy_epoch = identifier, account.privacy_epoch
                current.memory_revision, current.owner_epoch, current.cancelled = (
                    account.memory_revision,
                    owner_epoch,
                    False,
                )
                current.serial += 1
            from .tables import GenerationUse

            await db.execute(
                delete(GenerationUse).where(
                    GenerationUse.session_id == session_id,
                    GenerationUse.generation_id != identifier,
                )
            )
            return identifier, VersionFence(
                privacy_epoch=account.privacy_epoch,
                memory_revision=account.memory_revision,
                owner_epoch=owner_epoch,
            )

    async def generation_valid(self, scope, session_id, generation_id, fence):
        async with self.database.sessions() as db:
            account = await self._account(db, scope)
            session = await self._session(db, scope, session_id)
            generation = await db.get(Generation, session_id)
            valid = bool(
                generation
                and not generation.cancelled
                and generation.generation_id == generation_id
                and generation.owner_epoch == fence.owner_epoch == session.owner_epoch
                and generation.privacy_epoch == fence.privacy_epoch == account.privacy_epoch
                and generation.memory_revision == fence.memory_revision
                and session.status == "active"
                and session.lease_until
                and utc(session.lease_until) > self.clock()
            )
            return valid and await self._generation_read_set_valid(
                db, scope, session_id, generation_id
            )

    async def cancel_generation(self, scope, session_id, generation_id):
        async with self.database.sessions.begin() as db:
            await self._account(db, scope, lock=True)
            await self._session(db, scope, session_id)
            await db.execute(
                update(Generation)
                .where(
                    Generation.session_id == session_id, Generation.generation_id == generation_id
                )
                .values(cancelled=True)
            )
            await db.execute(
                update(OutputLedger)
                .where(
                    OutputLedger.session_id == session_id,
                    OutputLedger.generation_id == generation_id,
                    OutputLedger.state.not_in(TERMINAL),
                )
                .values(state="cancelled")
            )

    async def output(self, request):
        async def operation():
            plan = request.plan
            if not await self.generation_valid(
                request.scope, plan.session_id, plan.generation_id, request.fence
            ):
                raise MemoryFault("stale_generation", "Output generation is no longer valid")
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                session = await self._session(db, request.scope, plan.session_id)
                generation = await db.get(Generation, plan.session_id)
                if (
                    generation is None
                    or generation.cancelled
                    or generation.generation_id != plan.generation_id
                    or generation.owner_epoch != session.owner_epoch
                ):
                    raise MemoryFault("stale_generation", "Output fence changed before commit")
                if not await self._generation_read_set_valid(
                    db, request.scope, session.id, plan.generation_id
                ):
                    raise MemoryFault(
                        "stale_generation", "Output evidence changed before publication"
                    )
                row = await db.get(OutputLedger, plan.output_id)
                if row and (
                    row.account_id != account.id
                    or row.session_id != session.id
                    or row.generation_id != plan.generation_id
                ):
                    raise MemoryFault("not_found", "Output not found")
                if plan.expires_at <= self.clock():
                    raise MemoryFault("deadline_exceeded", "Output plan expired")
                if request.operation == "plan":
                    if row is None:
                        # In no-memory mode this is a technical receipt with no text.
                        retained = (
                            plan
                            if session.memory_enabled
                            else plan.model_copy(update={"spoken_text": None})
                        )
                        row = OutputLedger(
                            id=plan.output_id,
                            session_id=session.id,
                            account_id=account.id,
                            generation_id=plan.generation_id,
                            state="planned",
                            plan_json=retained.model_dump_json(),
                            last_client_sequence=-1,
                        )
                        db.add(row)
                elif row is None:
                    raise MemoryFault("not_found", "Output plan not found")
                elif row.state in TERMINAL:
                    if request.operation == "resume":
                        raise MemoryFault(
                            "invalid_transition", "Terminal outputs cannot be replayed"
                        )
                elif request.operation == "publish":
                    if row.state == "planned":
                        row.state = "published"
                    elif row.state not in {
                        "published",
                        "accepted",
                        "started",
                        "partial",
                        "completed",
                    }:
                        raise MemoryFault("invalid_transition", "Output has already been published")
                elif request.operation == "interrupt":
                    row.state = "interrupted"
                    generation.cancelled = True
                elif request.operation == "observe":
                    for observation in request.observations:
                        if (
                            observation.output_id != row.id
                            or observation.generation_id != row.generation_id
                        ):
                            raise MemoryFault(
                                "stale_generation", "Observation does not match the output"
                            )
                        if observation.client_sequence <= row.last_client_sequence:
                            continue
                        if (
                            (
                                observation.source in {"client", "renderer"}
                                and observation.state == "completed"
                            )
                            and request.capabilities.playback_granularity == "none"
                            and not (
                                plan.action_ids and request.capabilities.action_completed_receipt
                            )
                        ):
                            raise MemoryFault(
                                "unsupported_capability",
                                "Client cannot prove playback completion",
                            )
                        if observation.state == "accepted":
                            row.last_client_sequence = observation.client_sequence
                            continue
                        if observation.state not in TRANSITIONS.get(row.state, set()):
                            raise MemoryFault(
                                "invalid_transition", "Invalid observation transition"
                            )
                        row.state = observation.state
                        row.last_client_sequence = observation.client_sequence
                else:
                    raise MemoryFault("invalid_transition", "Unsupported output transition")
                output = OutputOutput(
                    output_id=row.id,
                    generation_id=row.generation_id,
                    state=row.state,
                    safe_spoken_prefix=None,
                    evidence_granularity=request.capabilities.playback_granularity
                    if row.state in {"started", "partial", "completed"}
                    else "none",
                    cancelled_action_ids=plan.action_ids
                    if row.state in {"cancelled", "interrupted"}
                    else [],
                    durable_event_id=row.id,
                )
                return ModuleResult(
                    status="ok",
                    data=output,
                    visibility=self._visibility(account, session.id, [], []),
                )

        return await self.executor.run(request, operation)

    async def track_generation(self, scope, session_id, generation_id, refs, *, kind="memory"):
        from .tables import GenerationUse

        async with self.database.sessions.begin() as db:
            await self._account(db, scope)
            await self._session(db, scope, session_id)
            for ref in refs:
                key = (session_id, generation_id, kind, ref.object_id, ref.version)
                if await db.get(GenerationUse, key) is None:
                    db.add(
                        GenerationUse(
                            session_id=session_id,
                            generation_id=generation_id,
                            kind=kind,
                            object_id=ref.object_id,
                            version=ref.version,
                        )
                    )

    async def _generation_read_set_valid(self, db, scope, session_id, generation_id):
        from .storage import EventHead, SourceRevision
        from .tables import DerivedView, EvidenceSuppression, FactVersion, GenerationUse

        uses = list(
            await db.scalars(
                select(GenerationUse)
                .where(
                    GenerationUse.session_id == session_id,
                    GenerationUse.generation_id == generation_id,
                )
                .limit(129)
            )
        )
        if len(uses) > 128:
            return False
        from sqlalchemy import exists, tuple_

        if any(use.kind not in {"memory", "source", "open_loop"} for use in uses):
            return False
        loops = {(use.object_id, use.version) for use in uses if use.kind == "open_loop"}
        if loops:
            actual = set(
                (
                    await db.execute(
                        select(DerivedView.id, DerivedView.version).where(
                            DerivedView.account_id == scope.user_id,
                            DerivedView.companion_id == scope.companion_id,
                            DerivedView.kind == "open_loop",
                            DerivedView.state == "valid",
                            tuple_(DerivedView.id, DerivedView.version).in_(loops),
                        )
                    )
                ).all()
            )
            if actual != loops:
                return False
        memories = {(use.object_id, use.version) for use in uses if use.kind == "memory"}
        if memories:
            actual = set(
                (
                    await db.execute(
                        select(FactVersion.memory_id, FactVersion.version).where(
                            FactVersion.account_id == scope.user_id,
                            FactVersion.companion_id == scope.companion_id,
                            tuple_(FactVersion.memory_id, FactVersion.version).in_(memories),
                            FactVersion.system_to.is_(None),
                            FactVersion.status == "active",
                        )
                    )
                ).all()
            )
            if actual != memories:
                return False
        sources = {(use.object_id, use.version) for use in uses if use.kind == "source"}
        if sources:
            suppressed = exists(
                select(EvidenceSuppression.event_id).where(
                    EvidenceSuppression.account_id == scope.user_id,
                    EvidenceSuppression.event_id == SourceRevision.event_id,
                    EvidenceSuppression.source_revision == SourceRevision.revision,
                )
            )
            actual = set(
                (
                    await db.execute(
                        select(SourceRevision.event_id, SourceRevision.revision)
                        .join(
                            EventHead,
                            (EventHead.event_id == SourceRevision.event_id)
                            & (EventHead.revision == SourceRevision.revision),
                        )
                        .where(
                            SourceRevision.account_id == scope.user_id,
                            SourceRevision.companion_id == scope.companion_id,
                            tuple_(SourceRevision.event_id, SourceRevision.revision).in_(sources),
                            SourceRevision.revoked.is_(False),
                            SourceRevision.source_json.is_not(None),
                            SourceRevision.expires_at > self.clock(),
                            ~suppressed,
                        )
                    )
                ).all()
            )
            if actual != sources:
                return False
        return True
