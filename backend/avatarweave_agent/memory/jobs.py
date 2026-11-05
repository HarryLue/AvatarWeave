"""M17 durable reference jobs, finite leases/retries and fair user admission."""

import asyncio
import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update

from .contracts import (
    AdmissionInput,
    CommitInput,
    ConflictInput,
    Contract,
    Identifier,
    JobInput,
    JobOutput,
    JobSpec,
    ModuleResult,
    Scope,
    TemporalInput,
    VersionFence,
    VersionRef,
)
from .execution import MemoryFault
from .lifecycle import fact_key, rule_for, temporal
from .storage import Account, Outbox, SourceRevision
from .tables import DerivedView, Job, UserWork
from .utils import utc


class MaintenanceReceipt(Contract):
    job_id: Identifier
    kind: Identifier
    result_schema: Identifier
    artifact_ids: list[Identifier]
    privacy_epoch: int
    memory_revision: int
    proposal_accepted: bool | None


class JobMixin:
    async def dispatch_outbox(self, limit=32):
        if not 1 <= limit <= 128:
            raise ValueError("Invalid dispatch batch")
        async with self.database.sessions.begin() as db:
            rows = list(
                await db.scalars(
                    select(Outbox)
                    .join(Account, Account.id == Outbox.account_id)
                    .where(
                        Account.workspace_id == self.workspace_id,
                        Outbox.state == "queued",
                        Outbox.available_at <= self.clock(),
                    )
                    .order_by(Outbox.available_at, Outbox.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                if await db.get(Job, row.id) is None:
                    spec = JobSpec(
                        job_id=row.id,
                        kind=row.kind,
                        payload_ref=row.event_id,
                        payload_schema="source-event.v1",
                        source_refs=[
                            VersionRef(object_id=row.event_id, version=row.source_revision)
                        ],
                        input_watermark=0,
                        privacy_epoch=row.privacy_epoch,
                        idempotency_key=row.id,
                        available_at=utc(row.available_at),
                    )
                    db.add(
                        Job(
                            id=row.id,
                            account_id=row.account_id,
                            companion_id=row.companion_id,
                            kind=row.kind,
                            spec_json=spec.model_dump_json(),
                            state="queued",
                            attempt=0,
                            available_at=row.available_at,
                            created_at=row.created_at,
                        )
                    )
                row.state = "running"
            return len(rows)

    async def job(self, request: JobInput):
        async def operation():
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                row = await db.scalar(
                    select(Job)
                    .where(
                        Job.id == request.job.job_id,
                        Job.account_id == account.id,
                        Job.companion_id == request.scope.companion_id,
                    )
                    .with_for_update()
                )
                if request.operation == "enqueue":
                    self._privacy(account, request)
                    payload = await db.get(DerivedView, (request.job.payload_ref, 1))
                    if (
                        payload is None
                        or payload.account_id != account.id
                        or payload.companion_id != request.scope.companion_id
                        or payload.kind != "job_payload"
                        or payload.state != "queued"
                    ):
                        raise MemoryFault("not_found", "Authorized typed job payload is required")
                    if row is None:
                        await self._job_capacity(db, account.id)
                        row = Job(
                            id=request.job.job_id,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            kind=request.job.kind,
                            spec_json=request.job.model_dump_json(),
                            state="queued",
                            attempt=0,
                            available_at=request.job.available_at,
                        )
                        db.add(row)
                        await db.flush()
                    elif row.spec_json != request.job.model_dump_json():
                        raise MemoryFault("version_conflict", "Job identifier already used")
                    return ModuleResult[JobOutput](
                        status="ok",
                        data=JobOutput(
                            job_id=row.id,
                            state=row.state,
                            attempt=row.attempt,
                            lease_token=row.lease_token,
                            lease_until=row.lease_until,
                            next_attempt_at=None,
                        ),
                        visibility=self._visibility(account, None, [], []),
                    )
                if row is None:
                    raise MemoryFault("not_found", "Job not found")
                spec = JobSpec.model_validate_json(row.spec_json)
                if spec != request.job:
                    raise MemoryFault("version_conflict", "Job specification changed")
                obsolete = spec.privacy_epoch != account.privacy_epoch
                for ref in spec.source_refs:
                    source = await db.get(SourceRevision, (ref.object_id, ref.version))
                    if (
                        source is None
                        or source.revoked
                        or (spec.kind == "extract" and source.source_json is None)
                    ):
                        obsolete = True
                if obsolete:
                    row.state, row.lease_token, row.lease_until = "obsolete", None, None
                elif request.operation == "claim":
                    expired = (
                        row.state == "running"
                        and row.lease_until
                        and utc(row.lease_until) <= self.clock()
                    )
                    if (row.state not in {"queued", "retry_wait"} and not expired) or utc(
                        row.available_at
                    ) > self.clock():
                        raise MemoryFault("version_conflict", "Job cannot be claimed")
                    if row.attempt >= spec.max_attempts:
                        row.state = "dead"
                    else:
                        running = await db.scalar(
                            select(func.count())
                            .select_from(Job)
                            .where(
                                Job.account_id == account.id,
                                Job.state == "running",
                                Job.lease_until > self.clock(),
                            )
                        )
                        if running >= 1:
                            raise MemoryFault(
                                "overloaded", "User maintenance slot is busy", retryable=True
                            )
                        row.attempt += 1
                        row.worker_id, row.lease_token = request.worker_id, uuid4().hex
                        row.lease_until = self.clock() + timedelta(seconds=30)
                        row.state = "running"
                        work = await db.get(UserWork, account.id)
                        if work is None:
                            db.add(UserWork(account_id=account.id, last_claimed_at=self.clock()))
                        else:
                            work.last_claimed_at = self.clock()
                else:
                    if (
                        row.state != "running"
                        or row.lease_token != request.lease_token
                        or row.worker_id != request.worker_id
                        or row.attempt != request.expected_attempt
                        or row.lease_until is None
                        or utc(row.lease_until) <= self.clock()
                    ):
                        raise MemoryFault("version_conflict", "Job lease is stale")
                    if request.operation == "renew":
                        row.lease_until = self.clock() + timedelta(seconds=30)
                    elif request.operation == "complete":
                        row.state = "done"
                    elif request.operation == "fail":
                        row.last_error_json = (
                            request.error.model_dump_json() if request.error else None
                        )
                        permanent = request.error and not request.error.retryable
                        row.state = (
                            "dead"
                            if permanent or row.attempt >= spec.max_attempts
                            else "retry_wait"
                        )
                        base = min(30 * 2 ** (row.attempt - 1), 3600)
                        jitter = (
                            int(
                                hashlib.sha256(f"{row.id}:{row.attempt}".encode()).hexdigest()[:8],
                                16,
                            )
                            % 2001
                            / 10000
                        )
                        row.available_at = self.clock() + timedelta(seconds=base * (1 + jitter))
                    else:
                        raise MemoryFault("invalid_transition", "Unsupported job transition")
                if row.state != "running":
                    row.lease_token, row.lease_until = None, None
                await db.execute(update(Outbox).where(Outbox.id == row.id).values(state=row.state))
                return ModuleResult[JobOutput](
                    status="ok",
                    data=JobOutput(
                        job_id=row.id,
                        state=row.state,
                        attempt=row.attempt,
                        lease_token=row.lease_token,
                        lease_until=row.lease_until,
                        next_attempt_at=utc(row.available_at)
                        if row.state == "retry_wait"
                        else None,
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def next_job(self, worker_id, gateway, embedding_gateway=None, store=None):
        """Process one bounded job, releasing every DB transaction before provider I/O."""
        from .http import meta

        await self.dispatch_outbox()
        async with self.database.sessions() as db:
            # At most one live task per user. Skip busy users, rather than repeatedly
            # claiming their oldest job and starving all subsequent users.
            busy = select(Job.account_id).where(
                Job.state == "running", Job.lease_until > self.clock()
            )
            row = await db.scalar(
                select(Job)
                .join(Account, Account.id == Job.account_id)
                .outerjoin(UserWork, UserWork.account_id == Job.account_id)
                .where(
                    Account.workspace_id == self.workspace_id,
                    Job.account_id.not_in(busy),
                    or_(
                        Job.state.in_(["queued", "retry_wait"]),
                        (Job.state == "running") & (Job.lease_until <= self.clock()),
                    ),
                    Job.available_at <= self.clock(),
                )
                .order_by(UserWork.last_claimed_at.asc().nullsfirst(), Job.available_at, Job.id)
                .limit(1)
            )
            if row is None:
                return False
            account = await db.get(Account, row.account_id)
            scope = Scope(
                workspace_id=account.workspace_id, user_id=account.id, companion_id=row.companion_id
            )
            fence = VersionFence(
                privacy_epoch=account.privacy_epoch,
                memory_revision=account.memory_revision,
                owner_epoch=0,
            )
            spec = JobSpec.model_validate_json(row.spec_json)
        claim_request = JobInput(
            meta=meta(self, lane="maintenance", budget_ms=1000),
            scope=scope,
            fence=fence,
            operation="claim",
            job=spec,
            worker_id=worker_id,
            lease_token=None,
            expected_attempt=0,
        )
        claimed = await self.job(claim_request)
        if claimed.status != "ok" or claimed.data.state != "running":
            return True
        from .job_context import JOB_LEASE

        token = JOB_LEASE.set((spec.job_id, claimed.data.lease_token, claimed.data.attempt))
        processing = asyncio.create_task(
            self._process_claimed(claim_request, claimed, gateway, embedding_gateway, store)
        )
        renewal_failed = False

        async def renew():
            nonlocal renewal_failed
            from .http import meta

            while True:
                await asyncio.sleep(10)
                result = await self.job(
                    claim_request.model_copy(
                        update={
                            "meta": meta(self, lane="control", budget_ms=1000),
                            "operation": "renew",
                            "lease_token": claimed.data.lease_token,
                            "expected_attempt": claimed.data.attempt,
                        }
                    )
                )
                if result.status != "ok" or result.data.state != "running":
                    renewal_failed = True
                    processing.cancel()
                    return

        renewing = asyncio.create_task(renew())
        try:
            try:
                return await processing
            except asyncio.CancelledError:
                if renewal_failed:
                    return True
                raise
        finally:
            renewing.cancel()
            await asyncio.gather(renewing, return_exceptions=True)
            JOB_LEASE.reset(token)

    async def _process_claimed(self, claim_request, claimed, gateway, embedding_gateway, store):
        from .http import meta

        scope, fence, spec = claim_request.scope, claim_request.fence, claim_request.job
        outcome, failure = "complete", None
        try:
            if spec.kind != "extract":
                await self._process_maintenance(claim_request, gateway, embedding_gateway, store)
                return await self._finish_claim(claim_request, claimed, "complete", None)
            async with self.database.sessions() as db:
                source = await self._source(
                    db, scope, spec.source_refs[0].object_id, spec.source_refs[0].version
                )
                from .contracts import SourceEvent

                event = SourceEvent.model_validate_json(source.source_json)
                from .tables import CorrectionTarget

                correction = await db.get(CorrectionTarget, event.event_id)

            from .contracts import AdmissionOutput, CommitOutput
            from .job_context import verify_job_lease
            from .tables import OperationReceipt

            cache_id = spec.job_id + ":extracted"
            async with self.database.sessions() as db:
                cached = await db.get(DerivedView, (cache_id, 1))
            if cached and cached.state != "valid":
                raise MemoryFault("privacy_changed", "Extraction snapshot was invalidated")
            if cached:
                admission = ModuleResult[AdmissionOutput](
                    status="ok", data=AdmissionOutput.model_validate_json(cached.document_json)
                )
            else:
                if gateway is None:
                    raise MemoryFault(
                        "provider_unavailable",
                        "Extraction provider is not configured",
                        retryable=True,
                    )
                admission = await self.admission(
                    AdmissionInput(
                        meta=meta(self, lane="maintenance", budget_ms=15000),
                        scope=scope,
                        fence=fence,
                        events=[event],
                        memory_enabled=True,
                        extraction_prompt_version="memory-extract.v1",
                    ),
                    gateway,
                )
            if admission.error:
                raise MemoryFault(
                    admission.error.code,
                    admission.error.message,
                    retryable=admission.error.retryable,
                )
            if cached is None:
                async with self.database.sessions.begin() as db:
                    account = await self._account(db, scope, lock=True)
                    self._privacy(account, claim_request)
                    await verify_job_lease(db, account.id, self.clock())
                    from .storage import QueueGuard

                    await db.execute(
                        update(QueueGuard)
                        .where(QueueGuard.id == 1)
                        .values(revision=QueueGuard.revision + 1)
                    )
                    _, used, _, _ = await self.queue_usage(db, account.id)
                    if (
                        used + len(admission.data.model_dump_json().encode())
                        > self.max_payload_bytes
                    ):
                        raise MemoryFault(
                            "overloaded", "Extraction cache byte budget is full", retryable=True
                        )
                    db.add(
                        DerivedView(
                            id=cache_id,
                            version=1,
                            account_id=account.id,
                            companion_id=scope.companion_id,
                            kind="extraction",
                            state="valid",
                            document_json=admission.data.model_dump_json(),
                        )
                    )
            accepted = {d.candidate_id for d in admission.data.decisions if d.decision == "accept"}
            needs_confirmation, committed = False, False
            committed_refs = []
            for candidate in admission.data.candidates:
                if candidate.candidate_id not in accepted:
                    needs_confirmation = True
                    continue
                resolved = temporal(
                    TemporalInput(
                        meta=meta(self),
                        scope=scope,
                        fence=fence,
                        candidates=[candidate],
                        user_timezone="Asia/Shanghai",
                        anchor_event=event,
                    )
                )
                candidate = resolved.candidates[0]
                if any(value.clarification for value in resolved.resolutions):
                    needs_confirmation = True
                    continue
                linked = await self.link_event_update(scope, event, candidate)
                if linked.requires_confirmation:
                    needs_confirmation = True
                    continue
                candidate = linked.candidate
                commit_id = hashlib.sha256(
                    (spec.job_id + candidate.candidate_id).encode()
                ).hexdigest()
                async with self.database.sessions() as db:
                    receipt = await db.get(OperationReceipt, (scope.user_id, commit_id))
                if receipt:
                    saved = CommitOutput.model_validate_json(receipt.result_json)
                    committed, committed_refs = True, committed_refs + saved.committed
                    if correction:
                        await self._complete_correction(scope, event.event_id, saved.committed[-1])
                    continue
                async with self.database.sessions() as db:
                    current_rows = await self._comparable(db, scope, fact_key(scope, candidate))
                    from .facts import load_record

                    current = [load_record(r) for r in current_rows]
                conflict = await self.conflict(
                    ConflictInput(
                        meta=meta(self, lane="maintenance", budget_ms=1000).model_copy(
                            update={"operation_id": commit_id}
                        ),
                        scope=scope,
                        fence=fence,
                        candidate=candidate,
                        predicate_rule=rule_for(candidate),
                        comparable_records=current,
                    )
                )
                if conflict.error:
                    raise MemoryFault(
                        conflict.error.code,
                        conflict.error.message,
                        retryable=conflict.error.retryable,
                    )
                proposals = conflict.data.proposals
                if correction:
                    from .contracts import MutationProposal

                    target = next(
                        (
                            r
                            for r in current
                            if r.ref.object_id == correction.memory_id
                            and r.ref.version == correction.expected_version
                        ),
                        None,
                    )
                    if (
                        target is None
                        or candidate.statement.predicate != target.statement.predicate
                        or len(accepted) != 1
                    ):
                        needs_confirmation = True
                        continue
                    proposals = [
                        MutationProposal(
                            action="retract",
                            candidate_id=candidate.candidate_id,
                            target_refs=[target.ref],
                            new_statement=candidate.statement,
                            reason="Authenticated correction control",
                            requires_user_confirmation=True,
                        )
                    ]
                if not correction and any(p.requires_user_confirmation for p in proposals):
                    needs_confirmation = True
                    continue
                result = await self.commit(
                    CommitInput(
                        meta=meta(self, lane="maintenance", budget_ms=1000).model_copy(
                            update={"operation_id": commit_id}
                        ),
                        scope=scope,
                        fence=fence,
                        candidate=candidate,
                        proposals=proposals,
                        user_confirmation_event=event.event_id if correction else None,
                        expected_refs=[r.ref for r in current],
                    )
                )
                if result.error:
                    raise MemoryFault(
                        result.error.code, result.error.message, retryable=result.error.retryable
                    )
                committed = True
                committed_refs.extend(result.data.committed)
                if correction:
                    await self._complete_correction(
                        scope, event.event_id, result.data.committed[-1]
                    )

            # Structured success commits before optional embedding I/O. A slow or
            # failed index must not hide facts that are already durable.
            async with self.database.sessions.begin() as db:
                account = await self._account(db, scope, lock=True)
                self._privacy(account, claim_request)
                await verify_job_lease(db, account.id, self.clock())
                source = await self._source(db, scope, event.event_id, event.revision)
                source.state = (
                    "needs_confirmation"
                    if needs_confirmation
                    else "structured"
                    if committed
                    else "rejected"
                )
                source.structured_settled = not needs_confirmation
                source.index_settled = not committed and not needs_confirmation
                await db.flush()
                await self.advance_watermarks(db, account)
            indexed = False
            if committed_refs and embedding_gateway:
                from .contracts import EmbeddingInput
                from .embedding import render_record
                from .facts import fact_scope, load_record
                from .tables import FactVersion

                items = []
                async with self.database.sessions() as db:
                    for ref in committed_refs:
                        row = await db.scalar(
                            select(FactVersion).where(
                                *fact_scope(scope),
                                FactVersion.memory_id == ref.object_id,
                                FactVersion.version == ref.version,
                                FactVersion.system_to.is_(None),
                                FactVersion.status == "active",
                            )
                        )
                        if row:
                            items.append((ref, render_record(load_record(row))))
                for offset in range(0, len(items), 10):
                    embedded = await self.embedding(
                        EmbeddingInput(
                            meta=meta(self, lane="maintenance", budget_ms=5000),
                            scope=scope,
                            fence=fence,
                            profile=embedding_gateway.profile,
                            purpose="index",
                            items=items[offset : offset + 10],
                        ),
                        embedding_gateway,
                    )
                    if embedded.error:
                        raise MemoryFault(
                            embedded.error.code,
                            embedded.error.message,
                            retryable=embedded.error.retryable,
                        )
                indexed = bool(items)
            async with self.database.sessions.begin() as db:
                account = await self._account(db, scope, lock=True)
                self._privacy(account, claim_request)
                await verify_job_lease(db, account.id, self.clock())
                source = await self._source(db, scope, event.event_id, event.revision)
                source.state = (
                    "needs_confirmation"
                    if needs_confirmation
                    else "indexed"
                    if indexed
                    else "structured"
                    if committed
                    else "rejected"
                )
                source.structured_settled = not needs_confirmation
                # Exact/lexical facts are available; semantic indexing remains pending.
                source.index_settled = indexed or (not committed and not needs_confirmation)
                await db.flush()
                await self.advance_watermarks(db, account)
        except MemoryFault as exc:
            outcome, failure = "fail", exc.info
        except (ValueError, TypeError):
            from .contracts import ErrorInfo

            outcome, failure = (
                "fail",
                ErrorInfo(
                    code="invalid_model_output",
                    message="Malformed maintenance payload or output",
                    retryable=False,
                ),
            )
        except BaseException:
            # The lease is left running on cancellation/process death and can be
            # reclaimed after expiry; never report successful completion.
            raise
        return await self._finish_claim(claim_request, claimed, outcome, failure)

    async def _finish_claim(self, claim_request, claimed, outcome, failure):
        from .http import meta

        spec = claim_request.job
        finished = await self.job(
            claim_request.model_copy(
                update={
                    "meta": meta(self, lane="maintenance", budget_ms=1000),
                    "operation": outcome,
                    "lease_token": claimed.data.lease_token,
                    "expected_attempt": claimed.data.attempt,
                    "error": failure,
                }
            )
        )
        if finished.data and finished.data.state in {"done", "dead", "obsolete"}:
            async with self.database.sessions.begin() as db:
                await db.execute(
                    update(DerivedView)
                    .where(
                        DerivedView.account_id == claim_request.scope.user_id,
                        DerivedView.id == spec.job_id + ":extracted",
                    )
                    .values(document_json=None, state="done")
                )
                if spec.kind != "extract" and finished.data.state in {"dead", "obsolete"}:
                    payload = await db.get(DerivedView, (spec.payload_ref, 1))
                    model = self._job_models().get(spec.kind)
                    if payload and payload.state == "queued" and payload.document_json and model:
                        payload.document_json = hashlib.sha256(
                            model.model_validate_json(payload.document_json)
                            .model_dump_json(exclude={"meta"})
                            .encode()
                        ).hexdigest()
                        payload.state = finished.data.state
        if finished.data and finished.data.state == "dead" and spec.source_refs:
            async with self.database.sessions.begin() as db:
                source = await db.get(
                    SourceRevision, (spec.source_refs[0].object_id, spec.source_refs[0].version)
                )
                if source and not source.revoked:
                    source.state = "failed"
        return True

    async def _complete_correction(self, scope, event_id, ref):
        from .job_context import verify_job_lease
        from .tables import ControlOperation, CorrectionTarget

        async with self.database.sessions.begin() as db:
            await self._account(db, scope, lock=True)
            await verify_job_lease(db, scope.user_id, self.clock())
            target = await db.get(CorrectionTarget, event_id)
            if target and target.account_id == scope.user_id:
                target.state = "completed"
            control = await db.scalar(
                select(ControlOperation).where(
                    ControlOperation.account_id == scope.user_id,
                    ControlOperation.event_id == event_id,
                )
            )
            if control:
                control.result_json = ref.model_dump_json()

    async def _job_capacity(self, db, account_id):
        from .storage import QueueGuard

        await db.execute(
            update(QueueGuard).where(QueueGuard.id == 1).values(revision=QueueGuard.revision + 1)
        )
        total, size, oldest, user = await self.queue_usage(db, account_id)
        if total >= self.max_outbox or user >= self.max_user_outbox:
            raise MemoryFault("overloaded", "Maintenance queue is full", retryable=True)
        if oldest and (self.clock() - utc(oldest)).total_seconds() > 300:
            raise MemoryFault(
                "overloaded", "Maintenance backlog age exceeds admission budget", retryable=True
            )
        return size

    async def queue_usage(self, db, account_id):
        """Outbox and jobs share admission; dispatched source jobs count only once."""
        from sqlalchemy import LargeBinary, cast

        active = ["queued", "running", "retry_wait"]
        queue = (
            select(Job.id, Job.account_id, Job.created_at)
            .where(Job.state.in_(active))
            .union(
                select(Outbox.id, Outbox.account_id, Outbox.created_at).where(
                    Outbox.state.in_(active)
                )
            )
            .subquery()
        )
        total, oldest = (
            await db.execute(select(func.count(), func.min(queue.c.created_at)).select_from(queue))
        ).one()
        user = await db.scalar(
            select(func.count()).select_from(queue).where(queue.c.account_id == account_id)
        )
        source_bytes = await db.scalar(
            select(func.coalesce(func.sum(Outbox.payload_bytes), 0)).where(Outbox.state.in_(active))
        )
        length = (
            func.octet_length(DerivedView.document_json)
            if self.database.engine.dialect.name == "postgresql"
            else func.length(cast(DerivedView.document_json, LargeBinary))
        )
        payload_bytes = await db.scalar(
            select(func.coalesce(func.sum(length), 0)).where(
                or_(
                    (DerivedView.kind == "job_payload") & (DerivedView.state == "queued"),
                    (DerivedView.kind == "extraction") & (DerivedView.state == "valid"),
                )
            )
        )
        return total, source_bytes + payload_bytes, oldest, user

    @staticmethod
    def _job_models():
        from .contracts import (
            ArchiveInput,
            CompressionInput,
            EmbeddingInput,
            ExportInput,
            MigrationInput,
            PrivacyInput,
            ReflectionInput,
            TieringInput,
        )

        return {
            "embed": EmbeddingInput,
            "reindex": EmbeddingInput,
            "summarize": CompressionInput,
            "reflect": ReflectionInput,
            "tier": TieringInput,
            "archive": ArchiveInput,
            "delete": PrivacyInput,
            "restore": MigrationInput,
            "export": ExportInput,
        }

    async def enqueue_maintenance(self, kind, payload):
        """Trusted scheduler API: validate and persist a typed payload before its ref-only job."""
        from .contracts import ScopedRequest

        model = self._job_models().get(kind)
        if (
            model is None
            or not isinstance(payload, model)
            or not isinstance(payload, ScopedRequest)
        ):
            raise MemoryFault("invalid_model_output", "Wrong typed payload for maintenance kind")
        encoded = payload.model_dump_json()
        canonical = payload.model_dump_json(exclude={"meta"})
        if len(encoded.encode()) > 256 * 1024:
            raise MemoryFault("budget_exceeded", "Maintenance payload exceeds its bounded shard")
        job_id = hashlib.sha256(
            (payload.scope.user_id + kind + payload.meta.operation_id).encode()
        ).hexdigest()
        payload_id = job_id + ":payload"
        async with self.database.sessions.begin() as db:
            account = await self._account(db, payload.scope, lock=True)
            self._privacy(account, payload)
            existing = await db.get(Job, job_id)
            if existing:
                previous = await db.get(DerivedView, (payload_id, 1))
                expected = (
                    hashlib.sha256(canonical.encode()).hexdigest()
                    if previous and previous.state in {"done", "dead", "obsolete"}
                    else canonical
                )
                actual = (
                    previous.document_json
                    if previous and previous.state in {"done", "dead", "obsolete"}
                    else (
                        model.model_validate_json(previous.document_json).model_dump_json(
                            exclude={"meta"}
                        )
                        if previous and previous.document_json
                        else None
                    )
                )
                if previous is None or actual != expected:
                    raise MemoryFault("version_conflict", "Maintenance idempotency key changed")
                return JobSpec.model_validate_json(existing.spec_json)
            used = await self._job_capacity(db, account.id)
            if used + len(encoded.encode()) > self.max_payload_bytes:
                raise MemoryFault("overloaded", "Maintenance byte budget is full", retryable=True)
            spec = JobSpec(
                job_id=job_id,
                kind=kind,
                payload_ref=payload_id,
                payload_schema=kind + ".v1",
                source_refs=[],
                input_watermark=account.source_watermark,
                privacy_epoch=account.privacy_epoch,
                idempotency_key=payload.meta.operation_id,
                available_at=self.clock(),
            )
            db.add(
                DerivedView(
                    id=payload_id,
                    version=1,
                    account_id=account.id,
                    companion_id=payload.scope.companion_id,
                    kind="job_payload",
                    state="queued",
                    document_json=encoded,
                )
            )
            db.add(
                Job(
                    id=job_id,
                    account_id=account.id,
                    companion_id=payload.scope.companion_id,
                    kind=kind,
                    spec_json=spec.model_dump_json(),
                    state="queued",
                    attempt=0,
                    available_at=self.clock(),
                )
            )
        return spec

    async def _process_maintenance(self, claim, gateway, embedding_gateway, store):
        from .http import meta
        from .job_context import verify_job_lease

        spec = claim.job
        model = self._job_models().get(spec.kind)
        async with self.database.sessions() as db:
            payload = await db.get(DerivedView, (spec.payload_ref, 1))
            if payload and payload.account_id == claim.scope.user_id and payload.state == "done":
                return
            if (
                model is None
                or payload is None
                or payload.account_id != claim.scope.user_id
                or payload.state != "queued"
            ):
                raise MemoryFault("not_found", "Maintenance payload is unavailable")
            request = model.model_validate_json(payload.document_json)
        if request.scope != claim.scope or request.fence.privacy_epoch != spec.privacy_epoch:
            raise MemoryFault("scope_mismatch", "Maintenance payload authorization changed")
        request = request.model_copy(
            update={
                "meta": meta(self, lane="maintenance", budget_ms=30000).model_copy(
                    update={"operation_id": spec.idempotency_key}
                )
            }
        )
        if spec.kind in {"embed", "reindex"}:
            if embedding_gateway is None:
                raise MemoryFault(
                    "provider_unavailable", "Embedding provider unavailable", retryable=True
                )
            result = await self.embedding(request, embedding_gateway)
        elif spec.kind == "reflect":
            if gateway is None:
                raise MemoryFault(
                    "provider_unavailable", "Reflection provider unavailable", retryable=True
                )
            result = await self.reflection(request, gateway)
        elif spec.kind == "archive":
            from .contracts import ArchiveManifest
            from .tables import ObjectManifest

            active = None
            if request.operation == "write":
                archive_id = hashlib.sha256(
                    (request.scope.user_id + request.meta.operation_id).encode()
                ).hexdigest()[:32]
                async with self.database.sessions() as db:
                    row = await db.scalar(
                        select(ObjectManifest).where(
                            ObjectManifest.id == archive_id,
                            ObjectManifest.account_id == request.scope.user_id,
                            ObjectManifest.companion_id == request.scope.companion_id,
                            ObjectManifest.kind == "archive",
                            ObjectManifest.state == "active",
                        )
                    )
                    active = ArchiveManifest.model_validate_json(row.manifest_json) if row else None
            result = await self.archive(
                request.model_copy(update={"operation": "verify", "archive": active})
                if active
                else request,
                store,
            )
            if (
                request.operation == "write"
                and result.status == "ok"
                and result.data.manifest.state != "active"
            ):
                result = await self.archive(
                    request.model_copy(
                        update={"operation": "verify", "archive": result.data.manifest}
                    ),
                    store,
                )
        elif spec.kind == "restore":
            result = await self.migration(
                request,
                store,
                embedding_gateway=embedding_gateway,
                deletion_journal=getattr(self, "deletion_journal", None),
            )
        elif spec.kind == "export":
            # Export orchestration calls the maintenance commit lane on import.
            request = request.model_copy(
                update={
                    "meta": request.meta.model_copy(
                        update={
                            "execution": request.meta.execution.model_copy(
                                update={"lane": "control"}
                            )
                        }
                    )
                }
            )
            result = await self.export(request, store)
        else:
            method = {"summarize": self.compression, "tier": self.tiering, "delete": self.privacy}[
                spec.kind
            ]
            result = await method(request)
        if result.error:
            raise MemoryFault(
                result.error.code, result.error.message, retryable=result.error.retryable
            )
        if spec.kind == "tier":
            await self.apply_tier_moves(request, result.data.moves, store)
        if (
            spec.kind == "archive"
            and request.operation == "write"
            and result.data.verified
            and result.data.manifest.state == "active"
        ):
            await self._set_tiers(request, [(ref, "cold") for ref in request.record_refs])
        async with self.database.sessions.begin() as db:
            account = await self._account(db, claim.scope, lock=True)
            await verify_job_lease(db, claim.scope.user_id, self.clock())
            payload = await db.get(DerivedView, (spec.payload_ref, 1))
            payload.state, payload.document_json = (
                "done",
                hashlib.sha256(
                    model.model_validate_json(payload.document_json)
                    .model_dump_json(exclude={"meta"})
                    .encode()
                ).hexdigest(),
            )
            db.add(
                DerivedView(
                    id=spec.job_id + ":result",
                    version=1,
                    account_id=claim.scope.user_id,
                    companion_id=claim.scope.companion_id,
                    kind="job_result",
                    state="valid",
                    document_json=MaintenanceReceipt(
                        job_id=spec.job_id,
                        kind=spec.kind,
                        result_schema=spec.kind + ".v1",
                        artifact_ids=[
                            str(value)
                            for value in [
                                getattr(
                                    getattr(result.data, "summary", None), "ref", None
                                ).object_id
                                if getattr(result.data, "summary", None)
                                else None,
                                getattr(getattr(result.data, "manifest", None), "archive_id", None),
                                getattr(result.data, "export_id", None),
                                getattr(result.data, "deletion_job_id", None),
                                getattr(result.data, "migration_id", None),
                            ]
                            if value is not None
                        ],
                        privacy_epoch=account.privacy_epoch,
                        memory_revision=account.memory_revision,
                        proposal_accepted=getattr(result.data, "accepted", None),
                    ).model_dump_json(),
                )
            )
