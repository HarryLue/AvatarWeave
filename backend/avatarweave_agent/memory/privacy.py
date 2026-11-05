"""M19 revocation: online read barrier and opaque tombstones precede physical cleanup."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, select, update

from .contracts import PrivacyInput, PrivacyOutput, PrivacyPreference, VersionRef
from .execution import MemoryFault
from .facts import fact_scope, invalidate, load_record
from .storage import Login, MemorySession, Outbox, SourceRevision
from .tables import (
    Capsule,
    ControlOperation,
    Deletion,
    Entity,
    FactSource,
    FactVersion,
    Generation,
    Job,
    ObjectManifest,
    OperationReceipt,
    OutputLedger,
    Tombstone,
)


class PrivacyMixin:
    async def privacy(self, request: PrivacyInput):
        async def operation():
            from .contracts import ModuleResult

            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                from .entities import EntityPrivacyInput, entity_audit_id

                if isinstance(request, EntityPrivacyInput):
                    from sqlalchemy import func

                    from .tables import DerivedView

                    entity = await db.get(Entity, request.entity_id)
                    version = (
                        await db.scalar(
                            select(func.max(DerivedView.version)).where(
                                DerivedView.account_id == account.id,
                                DerivedView.id == entity_audit_id(request.entity_id),
                            )
                        )
                        or 1
                    )
                    if (
                        entity is None
                        or entity.account_id != account.id
                        or entity.companion_id != request.scope.companion_id
                        or version != request.expected_entity_version
                    ):
                        raise MemoryFault("version_conflict", "Person changed before deletion")
                if request.operation == "query_deletion":
                    row = await db.scalar(
                        select(Deletion).where(
                            Deletion.id == request.deletion_job_id,
                            Deletion.account_id == account.id,
                            Deletion.companion_id == request.scope.companion_id,
                        )
                    )
                    if row is None:
                        raise MemoryFault("not_found", "Deletion job not found")
                    output = PrivacyOutput.model_validate_json(row.result_json)
                    return ModuleResult(
                        status="ok", data=output, visibility=self._visibility(account, None, [], [])
                    )
                control = await db.scalar(
                    select(ControlOperation).where(
                        ControlOperation.account_id == account.id,
                        ControlOperation.event_id == request.confirmed_control_event_id,
                        ControlOperation.companion_id == request.scope.companion_id,
                    )
                )
                if control and control.result_json:
                    output = PrivacyOutput.model_validate_json(control.result_json)
                    return ModuleResult(
                        status="ok", data=output, visibility=self._visibility(account, None, [], [])
                    )
                old = PrivacyPreference.model_validate_json(account.preferences_json)
                if old.preference_version != request.expected_preference_version:
                    raise MemoryFault("version_conflict", "Preferences changed")
                if control is None:
                    raise MemoryFault(
                        "unauthorized", "An authenticated control operation is required"
                    )
                controls = list(
                    await db.scalars(
                        select(SourceRevision).where(
                            SourceRevision.account_id == account.id,
                            SourceRevision.companion_id == request.scope.companion_id,
                            SourceRevision.event_id == request.confirmed_control_event_id,
                            SourceRevision.origin == "user_ui",
                            SourceRevision.revoked.is_(False),
                        )
                    )
                )
                if len(controls) != 1:
                    raise MemoryFault("unauthorized", "Explicit owned control event is required")
                if request.operation == "update_preferences":
                    preferences = request.preferences
                    if (
                        preferences is None
                        or preferences.preference_version != old.preference_version + 1
                    ):
                        raise MemoryFault(
                            "version_conflict", "Preferences require the next version"
                        )
                    if (
                        preferences.cross_companion_share != "none"
                        or preferences.permitted_shared_predicates
                    ):
                        raise MemoryFault(
                            "unsupported_capability",
                            "Cross-companion sharing requires separate authorization",
                        )
                    account.preferences_json = preferences.model_dump_json()
                    account.privacy_epoch += 1
                    if not preferences.retain_evidence_capsules:
                        await db.execute(delete(Capsule).where(Capsule.account_id == account.id))
                    boundary = self.clock() + timedelta(days=preferences.raw_transcript_days)
                    await db.execute(
                        update(SourceRevision)
                        .execution_options(synchronize_session=False)
                        .where(
                            SourceRevision.account_id == account.id,
                            SourceRevision.expires_at > boundary,
                        )
                        .values(expires_at=boundary)
                    )
                    if not preferences.long_term_memory_enabled:
                        await db.execute(
                            update(Outbox)
                            .where(
                                Outbox.account_id == account.id,
                                Outbox.state.in_(["queued", "running", "retry_wait"]),
                            )
                            .values(state="obsolete")
                        )
                        await db.execute(
                            update(Job)
                            .where(
                                Job.account_id == account.id,
                                Job.state.in_(["queued", "running", "retry_wait"]),
                            )
                            .values(state="obsolete")
                        )
                        await db.execute(
                            update(SourceRevision)
                            .execution_options(synchronize_session=False)
                            .where(
                                SourceRevision.account_id == account.id,
                                SourceRevision.state.in_(
                                    ["persisted", "provisional", "failed", "needs_confirmation"]
                                ),
                            )
                            .values(state="rejected", structured_settled=True, index_settled=True)
                        )
                    await self._cancel_private_outputs(db, account.id)
                    await db.flush()
                    await self.advance_watermarks(db, account)
                    output = PrivacyOutput(
                        privacy_epoch=account.privacy_epoch,
                        logically_revoked_refs=[],
                        deletion_job_id=None,
                        state="applied",
                        cleaned_classes=[],
                        backup_expiry_at=None,
                    )
                    control.result_json = output.model_dump_json()
                    return ModuleResult(
                        status="ok", data=output, visibility=self._visibility(account, None, [], [])
                    )
                if request.operation not in {
                    "delete_memory",
                    "delete_sources",
                    "delete_range",
                    "delete_account",
                }:
                    raise MemoryFault("invalid_transition", "Unsupported privacy operation")
                if request.operation == "delete_memory" and (
                    not request.target_refs or request.target_source_ids
                ):
                    raise MemoryFault(
                        "invalid_transition", "Memory deletion requires explicit memory targets"
                    )
                if request.operation == "delete_sources" and not request.target_source_ids:
                    raise MemoryFault(
                        "invalid_transition", "Source deletion requires explicit source targets"
                    )
                if request.operation == "delete_range" and (
                    request.target_time is None
                    or (request.target_time.start is None and request.target_time.end is None)
                ):
                    raise MemoryFault(
                        "invalid_transition", "Range deletion needs an explicit time bound"
                    )
                query = select(FactVersion).where(
                    *fact_scope(request.scope), FactVersion.record_json.is_not(None)
                )
                sources = set(request.target_source_ids)
                if request.operation == "delete_account":
                    query = select(FactVersion).where(
                        FactVersion.account_id == account.id, FactVersion.record_json.is_not(None)
                    )
                    sources.update(
                        await db.scalars(
                            select(SourceRevision.event_id)
                            .where(SourceRevision.account_id == account.id)
                            .limit(1001)
                        )
                    )

                if request.operation == "delete_memory":
                    ids = {ref.object_id for ref in request.target_refs}
                    current = list(
                        await db.scalars(
                            query.where(
                                FactVersion.memory_id.in_(ids), FactVersion.system_to.is_(None)
                            )
                        )
                    )
                    if {(r.memory_id, r.version) for r in current} != {
                        (r.object_id, r.version) for r in request.target_refs
                    }:
                        raise MemoryFault(
                            "version_conflict", "Deletion target is missing or changed"
                        )
                    query = query.where(FactVersion.memory_id.in_(ids))
                elif request.operation == "delete_sources":
                    owned = list(
                        await db.scalars(
                            select(SourceRevision.event_id).where(
                                SourceRevision.account_id == account.id,
                                SourceRevision.companion_id == request.scope.companion_id,
                                SourceRevision.event_id.in_(sources),
                            )
                        )
                    )
                    if set(owned) != sources:
                        raise MemoryFault("not_found", "Source target unavailable")
                    query = query.join(
                        FactSource,
                        and_(
                            FactSource.memory_id == FactVersion.memory_id,
                            FactSource.version == FactVersion.version,
                        ),
                    ).where(FactSource.event_id.in_(sources))
                elif request.operation == "delete_range":
                    source_query = select(SourceRevision.event_id).where(
                        SourceRevision.account_id == account.id,
                        SourceRevision.companion_id == request.scope.companion_id,
                    )
                    if request.target_time.start:
                        source_query = source_query.where(
                            SourceRevision.occurred_at >= request.target_time.start
                        )
                    if request.target_time.end:
                        source_query = source_query.where(
                            SourceRevision.occurred_at < request.target_time.end
                        )
                    sources.update(await db.scalars(source_query.limit(1001)))
                    query = query.join(
                        FactSource,
                        and_(
                            FactSource.memory_id == FactVersion.memory_id,
                            FactSource.version == FactVersion.version,
                        ),
                    ).where(FactSource.event_id.in_(sources))
                rows = list((await db.scalars(query.limit(1001))).unique())
                if request.operation == "delete_account":
                    from .tables import AccountBarrier

                    job_id = uuid4().hex
                    account.privacy_epoch += 1
                    db.add(
                        Tombstone(
                            account_id=account.id,
                            object_id=account.id,
                            kind="account",
                            privacy_epoch=account.privacy_epoch,
                            created_at=self.clock(),
                        )
                    )
                    account.memory_revision += 1
                    account.preferences_json = old.model_copy(
                        update={
                            "long_term_memory_enabled": False,
                            "preference_version": old.preference_version + 1,
                        }
                    ).model_dump_json()
                    await db.execute(
                        update(Outbox)
                        .where(Outbox.account_id == account.id)
                        .values(state="obsolete")
                    )
                    await db.execute(
                        update(Job).where(Job.account_id == account.id).values(state="obsolete")
                    )
                    await db.execute(
                        update(ObjectManifest)
                        .where(ObjectManifest.account_id == account.id)
                        .values(state="invalidated")
                    )
                    await self._cancel_private_outputs(db, account.id)
                    db.add(
                        AccountBarrier(
                            account_id=account.id, deletion_job_id=job_id, state="deleting"
                        )
                    )
                    await db.execute(delete(Login).where(Login.account_id == account.id))
                    await db.execute(
                        update(MemorySession)
                        .where(MemorySession.account_id == account.id)
                        .values(
                            status="ended",
                            owner_epoch=MemorySession.owner_epoch + 1,
                            owner_id=None,
                            lease_until=None,
                        )
                    )
                    output = PrivacyOutput(
                        privacy_epoch=account.privacy_epoch,
                        logically_revoked_refs=[],
                        deletion_job_id=job_id,
                        state="revoked_cleaning",
                        cleaned_classes=[],
                        backup_expiry_at=None,
                    )
                    control.result_json = output.model_dump_json()
                    db.add(
                        Deletion(
                            id=job_id,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            privacy_epoch=account.privacy_epoch,
                            created_at=self.clock(),
                            state="revoked_cleaning",
                            result_json=output.model_dump_json(),
                        )
                    )
                    return ModuleResult(
                        status="ok", data=output, visibility=self._visibility(account, None, [], [])
                    )
                if len(rows) > 1000 or len(sources) > 1000:
                    raise MemoryFault(
                        "budget_exceeded", "Deletion requires a bounded account cleanup job"
                    )
                account.privacy_epoch += 1
                account.memory_revision += 1
                refs, capsule_ids = [], set()
                for row in rows:
                    record = load_record(row)
                    refs.append(record.ref)
                    sources.update(p.event_id for p in record.sources)
                    capsule_ids.update(p.capsule_id for p in record.sources if p.capsule_id)
                    row.status, row.record_json = "deleted", None
                    if await db.get(Tombstone, (account.id, row.memory_id, "memory")) is None:
                        db.add(
                            Tombstone(
                                account_id=account.id,
                                object_id=row.memory_id,
                                kind="memory",
                                privacy_epoch=account.privacy_epoch,
                                created_at=self.clock(),
                            )
                        )
                source_refs = [
                    VersionRef(object_id=row.event_id, version=row.revision)
                    for row in await db.scalars(
                        select(SourceRevision).where(
                            SourceRevision.account_id == account.id,
                            SourceRevision.event_id.in_(sources),
                        )
                    )
                ]
                await invalidate(db, account.id, refs + source_refs, erase=True)
                if sources:
                    # Removing raw source bodies prevents re-extraction into a new opaque
                    # memory ID. Unrelated facts retain their independent legal capsules.
                    await db.execute(
                        update(SourceRevision)
                        .execution_options(synchronize_session=False)
                        .where(
                            SourceRevision.account_id == account.id,
                            SourceRevision.event_id.in_(sources),
                        )
                        .values(
                            source_json=None,
                            revoked=request.operation != "delete_memory",
                            structured_settled=True,
                            index_settled=True,
                            state="rejected",
                        )
                    )
                    await db.execute(
                        update(Outbox)
                        .where(Outbox.account_id == account.id, Outbox.event_id.in_(sources))
                        .values(state="obsolete")
                    )
                    if request.operation != "delete_memory":
                        await db.execute(
                            delete(Capsule).where(
                                Capsule.account_id == account.id, Capsule.event_id.in_(sources)
                            )
                        )
                    for event_id in sources:
                        if await db.get(Tombstone, (account.id, event_id, "source_raw")) is None:
                            db.add(
                                Tombstone(
                                    account_id=account.id,
                                    object_id=event_id,
                                    kind="source_raw",
                                    privacy_epoch=account.privacy_epoch,
                                    created_at=self.clock(),
                                )
                            )
                if sources:
                    await db.execute(
                        delete(ControlOperation).where(
                            ControlOperation.account_id == account.id,
                            ControlOperation.event_id.in_(sources),
                            ControlOperation.operation != "delete_memory",
                        )
                    )
                if capsule_ids:
                    await db.execute(
                        delete(Capsule).where(
                            Capsule.account_id == account.id, Capsule.id.in_(capsule_ids)
                        )
                    )
                await db.execute(
                    delete(OperationReceipt).where(OperationReceipt.account_id == account.id)
                )
                await db.execute(
                    update(Job)
                    .where(
                        Job.account_id == account.id,
                        Job.state.in_(["queued", "running", "retry_wait"]),
                    )
                    .values(state="obsolete")
                )
                await self._cancel_private_outputs(db, account.id)
                if request.operation == "delete_account":
                    await db.execute(delete(Login).where(Login.account_id == account.id))
                    await db.execute(delete(Entity).where(Entity.account_id == account.id))
                    account.preferences_json = old.model_copy(
                        update={
                            "long_term_memory_enabled": False,
                            "preference_version": old.preference_version + 1,
                        }
                    ).model_dump_json()
                    await db.execute(
                        update(MemorySession)
                        .where(MemorySession.account_id == account.id)
                        .values(
                            status="ended",
                            owner_epoch=MemorySession.owner_epoch + 1,
                            lease_until=None,
                        )
                    )
                await db.flush()
                await self.advance_watermarks(db, account)
                remaining_objects = await db.scalar(
                    select(ObjectManifest.id)
                    .where(ObjectManifest.account_id == account.id)
                    .limit(1)
                )
                job_id = uuid4().hex
                output = PrivacyOutput(
                    privacy_epoch=account.privacy_epoch,
                    logically_revoked_refs=refs,
                    deletion_job_id=job_id,
                    state="revoked_cleaning",
                    cleaned_classes=[
                        "source",
                        "memory",
                        "embedding",
                        "summary",
                        "profile",
                        "cache",
                    ],
                    # No configured backup inventory: cannot claim physical completion.
                    backup_expiry_at=None,
                )
                if control:
                    control.result_json = output.model_dump_json()
                db.add(
                    Deletion(
                        id=job_id,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        privacy_epoch=account.privacy_epoch,
                        created_at=self.clock(),
                        state="revoked_cleaning",
                        result_json=output.model_dump_json(),
                    )
                )
                return ModuleResult(
                    status="ok",
                    data=output,
                    visibility=self._visibility(account, None, [], []),
                    warnings=["Physical object/backup cleanup requires a verified inventory"]
                    if remaining_objects
                    else [],
                )

        return await self.executor.run(request, operation)

    async def _cancel_private_outputs(self, db, account_id):
        from .tables import DerivedView

        await db.execute(
            update(DerivedView)
            .where(
                DerivedView.account_id == account_id,
                DerivedView.kind.in_(["extraction", "job_payload", "job_result"]),
            )
            .values(state="invalidated", document_json=None)
        )
        sessions = select(MemorySession.id).where(MemorySession.account_id == account_id)
        await db.execute(
            update(Generation).where(Generation.session_id.in_(sessions)).values(cancelled=True)
        )
        await db.execute(
            update(OutputLedger)
            .where(
                OutputLedger.account_id == account_id,
                OutputLedger.state.not_in(["completed", "cancelled", "failed"]),
            )
            .values(state="cancelled", plan_json=None)
        )

    async def process_deletions(
        self, *, batch_size=128, store=None, backup_inventory_verified=False
    ):
        """One finite physical-cleanup batch. External backup completion is explicit."""
        from sqlalchemy import func

        from .storage import Account, PreferenceReceipt
        from .tables import AccountBarrier, ControlOperation, DerivedView, FactKey, VectorEntry

        if not 1 <= batch_size <= 128:
            raise ValueError("Invalid cleanup batch")
        async with self.database.sessions.begin() as db:
            job = await db.scalar(
                select(Deletion)
                .join(Account, Account.id == Deletion.account_id)
                .where(
                    Deletion.state == "revoked_cleaning", Account.workspace_id == self.workspace_id
                )
                .order_by(Deletion.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return 0
            account = await db.get(Account, job.account_id)
            barrier = await db.get(AccountBarrier, account.id)
            changed = 0
            if barrier:
                records = list(
                    await db.scalars(
                        select(FactVersion)
                        .where(FactVersion.account_id == account.id)
                        .order_by(FactVersion.memory_id, FactVersion.version)
                        .limit(batch_size)
                    )
                )
                for row in records:
                    if await db.get(Tombstone, (account.id, row.memory_id, "memory")) is None:
                        db.add(
                            Tombstone(
                                account_id=account.id,
                                object_id=row.memory_id,
                                kind="memory",
                                privacy_epoch=account.privacy_epoch,
                                created_at=self.clock(),
                            )
                        )
                    await db.execute(
                        delete(FactSource).where(
                            FactSource.memory_id == row.memory_id, FactSource.version == row.version
                        )
                    )
                    await db.delete(row)
                changed += len(records)
                if records:
                    return changed
                sources = list(
                    await db.scalars(
                        select(SourceRevision)
                        .where(SourceRevision.account_id == account.id)
                        .order_by(SourceRevision.sequence)
                        .limit(batch_size)
                    )
                )
                for source in sources:
                    if await db.get(Tombstone, (account.id, source.event_id, "source_raw")) is None:
                        db.add(
                            Tombstone(
                                account_id=account.id,
                                object_id=source.event_id,
                                kind="source_raw",
                                privacy_epoch=account.privacy_epoch,
                                created_at=self.clock(),
                            )
                        )
                    await db.delete(source)
                changed += len(sources)
                if sources:
                    return changed
                for model, column in [
                    (Capsule, Capsule.id),
                    (DerivedView, DerivedView.id),
                    (Entity, Entity.id),
                    (VectorEntry, VectorEntry.memory_id),
                    (FactKey, FactKey.id),
                    (OutputLedger, OutputLedger.id),
                ]:
                    ids = list(
                        await db.scalars(
                            select(column).where(model.account_id == account.id).limit(batch_size)
                        )
                    )
                    if ids:
                        await db.execute(
                            delete(model).where(model.account_id == account.id, column.in_(ids))
                        )
                        return len(ids)
                await db.execute(
                    delete(OperationReceipt).where(OperationReceipt.account_id == account.id)
                )
                await db.execute(
                    delete(ControlOperation).where(ControlOperation.account_id == account.id)
                )
                await db.execute(
                    delete(PreferenceReceipt).where(PreferenceReceipt.account_id == account.id)
                )
            objects = list(
                await db.scalars(
                    select(ObjectManifest)
                    .where(
                        ObjectManifest.account_id == account.id,
                        ObjectManifest.state == "invalidated",
                    )
                    .limit(4)
                )
            )
            # Object deletion runs outside the transaction, under the active logical barrier.
            object_refs = []
            for obj in objects:
                from .archive import ArchiveManifest
                from .portability import ExportManifest

                model = ArchiveManifest if obj.kind == "archive" else ExportManifest
                object_refs.append(
                    (obj.id, model.model_validate_json(obj.manifest_json).object_ref)
                )
            job_id, account_id = job.id, account.id
        if object_refs:
            if store is None:
                return changed
            for manifest_id, object_ref in object_refs:
                await store.revoke(job_id, object_ref)
                async with self.database.sessions.begin() as db:
                    await db.execute(
                        delete(ObjectManifest).where(
                            ObjectManifest.id == manifest_id,
                            ObjectManifest.account_id == account_id,
                            ObjectManifest.state == "invalidated",
                        )
                    )
                changed += 1
        async with self.database.sessions.begin() as db:
            job = await db.get(Deletion, job_id)
            remaining = await db.scalar(
                select(func.count())
                .select_from(ObjectManifest)
                .where(
                    ObjectManifest.account_id == account_id, ObjectManifest.state == "invalidated"
                )
            )
            if not remaining and backup_inventory_verified:
                output = PrivacyOutput.model_validate_json(job.result_json)
                output = output.model_copy(
                    update={
                        "state": "completed",
                        "cleaned_classes": [
                            "source",
                            "memory",
                            "summary",
                            "profile",
                            "embedding",
                            "archive",
                            "cache",
                            "export",
                            "backup_manifest",
                        ],
                    }
                )
                job.result_json, job.state = output.model_dump_json(), "completed"
                if barrier:
                    barrier = await db.get(AccountBarrier, account_id)
                    barrier.state = "deleted"
                    await db.execute(delete(Login).where(Login.account_id == account_id))
                    account = await db.get(Account, account_id)
                    account.issuer, account.subject = "", "deleted:" + account.id
                changed += 1
        return changed
