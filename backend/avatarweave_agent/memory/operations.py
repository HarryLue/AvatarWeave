"""M20 versioned migration plans and copy/verify phases; switching is explicit."""

import hashlib

from sqlalchemy import func, select

from .contracts import ArchiveManifest, MigrationOutput, ModuleResult
from .execution import MemoryFault
from .storage import SchemaVersion
from .tables import ObjectManifest, Tombstone


class MigrationMixin:
    async def migration(self, request, store=None, embedding_gateway=None, deletion_journal=None):
        async def operation():
            spec = request.spec
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
            if spec.kind == "restore":
                if deletion_journal is None:
                    raise MemoryFault(
                        "unsupported_capability", "Restore requires an independent deletion journal"
                    )
                await deletion_journal.verify_reconciled(
                    self.database,
                    request.scope,
                    spec.expected_deleted_watermark,
                    min(spec.batch_size, 10000),
                )
            if spec.kind == "embedding":
                return await self._migrate_embedding(request, embedding_gateway)
            if spec.kind == "archive_format" and (
                spec.source_version != "2" or spec.target_version != "2"
            ):
                raise MemoryFault(
                    "unsupported_capability",
                    "No reviewed conversion exists for this archive version",
                )
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                deleted = (
                    await db.scalar(
                        select(func.max(Tombstone.privacy_epoch)).where(
                            Tombstone.account_id == account.id
                        )
                    )
                    or 0
                )
                if deleted != spec.expected_deleted_watermark:
                    raise MemoryFault(
                        "privacy_changed", "Restore/migration deletion watermark changed"
                    )
                if spec.kind == "schema":
                    version = await db.get(SchemaVersion, 1)
                    if (
                        spec.source_version not in {"1", "2", "3"}
                        or spec.target_version != "3"
                        or version.version != 3
                    ):
                        raise MemoryFault(
                            "unsupported_capability",
                            "No reviewed conversion exists for this schema version",
                        )
                    return ModuleResult(
                        status="ok",
                        data=MigrationOutput(
                            migration_id=spec.migration_id,
                            state="planned" if spec.dry_run else "ready_to_switch",
                            copied_count=0,
                            verified_count=0,
                            deleted_records_reintroduced=0,
                            checksum_failures=0,
                            next_cursor=None,
                            rollback_manifest_ref="schema:3",
                        ),
                        visibility=self._visibility(account, None, [], []),
                    )
                rows = list(
                    await db.scalars(
                        select(ObjectManifest)
                        .where(
                            ObjectManifest.account_id == account.id,
                            ObjectManifest.companion_id == request.scope.companion_id,
                            ObjectManifest.id.in_(request.manifest_ids),
                            ObjectManifest.kind == "archive",
                            ObjectManifest.state == "active",
                        )
                        .limit(spec.batch_size + 1)
                    )
                )
                if len(rows) != len(set(request.manifest_ids)) or len(rows) > spec.batch_size:
                    raise MemoryFault(
                        "not_found", "Migration batch is unavailable or exceeds its explicit bound"
                    )
                manifests = [ArchiveManifest.model_validate_json(row.manifest_json) for row in rows]
                if spec.kind == "encryption_key" and any(
                    m.encryption_key_version != spec.source_version for m in manifests
                ):
                    raise MemoryFault("version_conflict", "Migration source key version differs")
            if spec.kind not in {"encryption_key", "archive_format", "restore"}:
                raise MemoryFault(
                    "unsupported_capability",
                    "Embedding migration uses the explicit new-profile index adapter",
                )
            copied, verified = 0, 0
            rollback = None
            if not spec.dry_run:
                if store is None:
                    raise MemoryFault(
                        "storage_unavailable", "Migration object/key provider unavailable"
                    )
                for manifest in manifests:
                    if manifest.privacy_epoch != request.fence.privacy_epoch:
                        raise MemoryFault(
                            "privacy_changed", "Archive requires current deletion reconciliation"
                        )
                    payload = await store.read(
                        request.meta.operation_id,
                        manifest.object_ref,
                        manifest.encryption_key_version,
                        8 * 1024 * 1024,
                    )
                    if hashlib.sha256(payload).hexdigest() != manifest.content_sha256:
                        raise MemoryFault("storage_unavailable", "Migration checksum failed")
                    from .archive import ArchiveBundle, EncryptedObjectStore

                    bundle = ArchiveBundle.model_validate_json(payload)
                    async with self.database.sessions() as db:
                        current = await self._account(db, request.scope)
                        self._privacy(current, request)
                        for record in bundle.records:
                            if record.scope != request.scope or await db.get(
                                Tombstone, (current.id, record.ref.object_id, "memory")
                            ):
                                raise MemoryFault(
                                    "privacy_changed", "Restore would reintroduce deleted data"
                                )
                    key_version = (
                        spec.target_version if spec.kind == "encryption_key" else store.key_version
                    )
                    target = EncryptedObjectStore(store.root, store.keys, key_version=key_version)
                    target.tasks = store.tasks
                    archive_id = hashlib.sha256(
                        (spec.migration_id + manifest.archive_id + spec.target_version).encode()
                    ).hexdigest()[:32]
                    object_ref = hashlib.sha256((archive_id + ":object").encode()).hexdigest()[:32]
                    migrated = manifest.model_copy(
                        update={
                            "archive_id": archive_id,
                            "object_ref": object_ref,
                            "encryption_key_version": key_version,
                            "state": "staged",
                        }
                    )
                    async with self.database.sessions.begin() as db:
                        current = await self._account(db, request.scope, lock=True)
                        self._privacy(current, request)
                        prior = await db.get(ObjectManifest, archive_id)
                        if prior and prior.state == "invalidated":
                            raise MemoryFault("privacy_changed", "Migration copy was revoked")
                        if prior is None:
                            db.add(
                                ObjectManifest(
                                    id=archive_id,
                                    account_id=current.id,
                                    companion_id=request.scope.companion_id,
                                    kind="archive",
                                    state="staged",
                                    manifest_json=migrated.model_dump_json(),
                                )
                            )
                    await target.write(request.meta.operation_id, object_ref, payload)
                    if (
                        await target.read(
                            request.meta.operation_id, object_ref, key_version, 8 * 1024 * 1024
                        )
                        != payload
                    ):
                        raise MemoryFault(
                            "storage_unavailable", "Migration copy verification failed"
                        )
                    migrated = migrated.model_copy(update={"state": "verified"})
                    async with self.database.sessions.begin() as db:
                        current = await self._account(db, request.scope, lock=True)
                        self._privacy(current, request)
                        catalog = await db.get(ObjectManifest, migrated.archive_id)
                        if catalog is None or catalog.state == "invalidated":
                            raise MemoryFault("privacy_changed", "Migration was revoked during I/O")
                        catalog.state, catalog.manifest_json = (
                            "verified",
                            migrated.model_dump_json(),
                        )
                    copied += 1
                    verified += 1
                    rollback = manifest.archive_id
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                return ModuleResult(
                    status="ok",
                    data=MigrationOutput(
                        migration_id=spec.migration_id,
                        state="planned" if spec.dry_run else "ready_to_switch",
                        copied_count=copied,
                        verified_count=verified,
                        deleted_records_reintroduced=0,
                        checksum_failures=0,
                        next_cursor=None,
                        rollback_manifest_ref=rollback,
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        if store and store.remaining not in self.executor.work_providers:
            self.executor.work_providers.append(store.remaining)
        return await self.executor.run(request, operation)

    async def _migrate_embedding(self, request, gateway):
        from .contracts import EmbeddingInput
        from .embedding import render_record
        from .facts import fact_scope, load_record
        from .http import meta
        from .tables import FactVersion, VectorEntry

        spec = request.spec
        if (
            spec.target_embedding_profile is None
            or gateway is None
            or gateway.profile != spec.target_embedding_profile
            or spec.target_version not in {gateway.profile.profile_id, gateway.profile.revision}
        ):
            raise MemoryFault(
                "unsupported_capability", "Target embedding profile is not configured exactly"
            )
        async with self.database.sessions() as db:
            account = await self._account(db, request.scope)
            self._privacy(account, request)
            deleted = (
                await db.scalar(
                    select(func.max(Tombstone.privacy_epoch)).where(
                        Tombstone.account_id == account.id
                    )
                )
                or 0
            )
            if deleted != spec.expected_deleted_watermark:
                raise MemoryFault(
                    "privacy_changed", "Embedding migration deletion watermark changed"
                )
            revision = account.memory_revision
            query = select(FactVersion).where(
                *fact_scope(request.scope),
                FactVersion.status == "active",
                FactVersion.system_to.is_(None),
            )
            if request.cursor:
                try:
                    previous_revision, last = request.cursor.split(":", 1)
                    if int(previous_revision) != revision:
                        raise MemoryFault("version_conflict", "Migration snapshot changed")
                except ValueError:
                    raise MemoryFault("invalid_transition", "Invalid migration cursor") from None
                query = query.where(FactVersion.memory_id > last)
            rows = list(
                await db.scalars(
                    query.order_by(FactVersion.memory_id).limit(min(spec.batch_size, 1000) + 1)
                )
            )
            records = [load_record(row) for row in rows[: min(spec.batch_size, 1000)]]
            next_cursor = (
                f"{revision}:{records[-1].ref.object_id}" if len(rows) > len(records) else None
            )
        if not spec.dry_run:
            for offset in range(0, len(records), 10):
                result = await self.embedding(
                    EmbeddingInput(
                        meta=meta(self, lane="maintenance", budget_ms=5000),
                        scope=request.scope,
                        fence=request.fence,
                        profile=gateway.profile,
                        purpose="migration",
                        items=[(r.ref, render_record(r)) for r in records[offset : offset + 10]],
                    ),
                    gateway,
                )
                if result.error:
                    raise MemoryFault(
                        result.error.code, result.error.message, retryable=result.error.retryable
                    )
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                for record in records:
                    vector = await db.get(
                        VectorEntry,
                        (
                            account.id,
                            record.ref.object_id,
                            record.ref.version,
                            gateway.profile.profile_id,
                            "index:" + gateway.profile.revision,
                        ),
                    )
                    if vector is None or vector.profile_json != gateway.profile.model_dump_json():
                        raise MemoryFault(
                            "version_conflict", "Migration projection verification failed"
                        )
        return ModuleResult(
            status="ok",
            data=MigrationOutput(
                migration_id=spec.migration_id,
                state="planned" if spec.dry_run else "copying" if next_cursor else "verifying",
                copied_count=0 if spec.dry_run else len(records),
                verified_count=0 if spec.dry_run else len(records),
                deleted_records_reintroduced=0,
                checksum_failures=0,
                next_cursor=next_cursor,
                rollback_manifest_ref="embedding:" + spec.source_version,
            ),
            visibility=self._visibility(account, None, [], []),
        )
