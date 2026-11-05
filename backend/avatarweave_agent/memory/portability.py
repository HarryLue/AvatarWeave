"""M25 account-bound, revocable, encrypted exports and explicit import preflight."""

import hashlib
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime
from sqlalchemy import select

from .contracts import Contract, ExportOutput, ModuleResult
from .execution import MemoryFault
from .portable_graph import PortableBundle, PortableGraphMixin, read_package
from .tables import ControlOperation, ObjectManifest, Tombstone


class ExportManifest(Contract):
    export_id: str
    scope_user_id: str
    companion_id: str
    schema_version: Literal[2, 3] = 3
    object_ref: str
    encryption_key_version: str
    checksum: str
    privacy_epoch: int
    record_count: int
    expires_at: AwareDatetime
    state: Literal["staged", "ready", "verified", "needs_review", "revoked"]


class PortabilityMixin(PortableGraphMixin):
    async def export(self, request, store):
        async def operation():
            if store is None:
                raise MemoryFault("storage_unavailable", "Encrypted export store is not configured")
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                control = await db.scalar(
                    select(ControlOperation).where(
                        ControlOperation.account_id == account.id,
                        ControlOperation.companion_id == request.scope.companion_id,
                        ControlOperation.event_id == request.explicit_user_confirmation_event,
                    )
                )
                if control is None:
                    raise MemoryFault(
                        "unauthorized", "An authenticated export/import control is required"
                    )
                if request.operation == "create" and control.result_json:
                    output = ExportOutput.model_validate_json(control.result_json)
                    existing = await db.get(ObjectManifest, output.export_id)
                    if existing is None or existing.state in {"invalidated", "revoked", "staged"}:
                        raise MemoryFault("privacy_changed", "Previous export is unavailable")
                    old_manifest = ExportManifest.model_validate_json(existing.manifest_json)
                    if old_manifest.expires_at <= self.clock():
                        raise MemoryFault(
                            "privacy_changed", "Previous export expired; use a new request"
                        )
                    return ModuleResult[ExportOutput](
                        status="ok", data=output, visibility=self._visibility(account, None, [], [])
                    )
                if request.operation == "create":
                    portable = await self.build_portable_graph(db, request, account)
                    records = portable.bundle.records
                    plaintext = portable.model_dump_json().encode()
                    if len(plaintext) > 8 * 1024 * 1024:
                        raise MemoryFault(
                            "budget_exceeded", "Portable shard exceeds the 8 MiB transfer bound"
                        )
                    manifest = ExportManifest(
                        export_id=uuid4().hex,
                        scope_user_id=account.id,
                        companion_id=request.scope.companion_id,
                        object_ref=uuid4().hex,
                        encryption_key_version=store.key_version,
                        checksum=hashlib.sha256(plaintext).hexdigest(),
                        privacy_epoch=account.privacy_epoch,
                        record_count=len(records),
                        expires_at=self.clock() + timedelta(minutes=15),
                        state="staged",
                    )
                else:
                    row = await db.scalar(
                        select(ObjectManifest).where(
                            ObjectManifest.id == request.export_id,
                            ObjectManifest.account_id == account.id,
                            ObjectManifest.companion_id == request.scope.companion_id,
                            ObjectManifest.kind == "export",
                        )
                    )
                    if row is None or row.state in {"invalidated", "revoked", "staged"}:
                        raise MemoryFault("not_found", "Export is unavailable")
                    if request.operation == "import_apply" and row.state != "verified":
                        raise MemoryFault(
                            "invalid_transition",
                            "A verified dry run is required before import apply",
                        )
                    manifest = ExportManifest.model_validate_json(row.manifest_json)
                    if (
                        manifest.privacy_epoch != account.privacy_epoch
                        or manifest.expires_at <= self.clock()
                    ):
                        raise MemoryFault("privacy_changed", "Export authorization expired")
            if request.operation == "create":
                async with self.database.sessions.begin() as db:
                    account = await self._account(db, request.scope, lock=True)
                    self._privacy(account, request)
                    db.add(
                        ObjectManifest(
                            id=manifest.export_id,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            kind="export",
                            state="staged",
                            manifest_json=manifest.model_dump_json(),
                            expires_at=manifest.expires_at,
                        )
                    )
                await store.write(request.meta.operation_id, manifest.object_ref, plaintext)
            plaintext = await store.read(
                request.meta.operation_id,
                manifest.object_ref,
                manifest.encryption_key_version,
                8 * 1024 * 1024,
            )
            if hashlib.sha256(plaintext).hexdigest() != manifest.checksum:
                raise MemoryFault("storage_unavailable", "Export checksum mismatch")
            package = read_package(plaintext)
            if (
                package.owner_user_id != request.scope.user_id
                or package.companion_id != request.scope.companion_id
            ):
                raise MemoryFault(
                    "scope_mismatch", "Export owner proof does not match this account"
                )
            conflicts = []
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                for record in package.bundle.records:
                    if record.scope != request.scope or await db.get(
                        Tombstone, (account.id, record.ref.object_id, "memory")
                    ):
                        conflicts.append("Package contains deleted or out-of-scope memory")
                    for pointer in record.sources:
                        if await db.get(Tombstone, (account.id, pointer.event_id, "source_raw")):
                            conflicts.append("Package evidence has been deleted")
                if request.operation in {"import_dry_run", "import_apply"} and not isinstance(
                    package, PortableBundle
                ):
                    conflicts.append(
                        "Legacy v2 package needs provenance review; it cannot be promoted to confirmed facts by importing"
                    )
                if request.operation in {"import_dry_run", "import_apply"} and isinstance(
                    package, PortableBundle
                ):
                    conflicts.extend(await self.graph_conflicts(db, request, package))
                if request.operation == "create":
                    if isinstance(package, PortableBundle) and (
                        package.memory_revision != account.memory_revision
                        or package.source_watermark != account.source_watermark
                    ):
                        raise MemoryFault("version_conflict", "Export snapshot changed during I/O")
                    catalog = await db.get(ObjectManifest, manifest.export_id)
                    if catalog is None or catalog.state == "invalidated":
                        raise MemoryFault("privacy_changed", "Export was revoked during I/O")
                    manifest = manifest.model_copy(update={"state": "ready"})
                    catalog.state, catalog.manifest_json = "ready", manifest.model_dump_json()
                elif request.operation in {"verify", "import_dry_run"}:
                    catalog = await db.get(ObjectManifest, manifest.export_id)
                    if catalog is None or catalog.state in {"invalidated", "revoked"}:
                        raise MemoryFault("privacy_changed", "Export was revoked during I/O")
                    catalog.state = "needs_review" if conflicts else "verified"
                visibility = self._visibility(account, None, [], [])
            if request.operation == "import_apply" and not conflicts:
                if isinstance(package, PortableBundle):
                    await self.restore_portable_graph(request, package)
                async with self.database.sessions() as db:
                    account = await self._account(db, request.scope)
                    visibility = self._visibility(account, None, [], [])
            state = (
                "needs_review"
                if conflicts
                else "ready"
                if request.operation == "create"
                else "imported"
                if request.operation == "import_apply"
                else "verified"
            )
            output = ExportOutput(
                export_id=manifest.export_id,
                state=state,
                manifest_ref=manifest.export_id,
                record_count=manifest.record_count,
                schema_version=package.schema_version,
                conflicts=list(dict.fromkeys(conflicts))[:128],
                delivery_handle=manifest.export_id if state == "ready" else None,
            )
            if request.operation == "create":
                async with self.database.sessions.begin() as db:
                    account = await self._account(db, request.scope, lock=True)
                    self._privacy(account, request)
                    control = await db.get(ControlOperation, (account.id, control.idempotency_key))
                    control.result_json = output.model_dump_json()
            return ModuleResult[ExportOutput](
                status="ok",
                data=output,
                visibility=visibility,
            )

        if store and store.remaining not in self.executor.work_providers:
            self.executor.work_providers.append(store.remaining)
        return await self.executor.run(request, operation)

    async def expire_exports(self, store, batch_size=16):
        from sqlalchemy import delete, or_

        from .storage import Account

        if not 1 <= batch_size <= 32:
            raise ValueError("Invalid export expiry batch")
        async with self.database.sessions.begin() as db:
            rows = list(
                await db.scalars(
                    select(ObjectManifest)
                    .join(Account, Account.id == ObjectManifest.account_id)
                    .where(
                        Account.workspace_id == self.workspace_id,
                        ObjectManifest.kind == "export",
                        or_(
                            ObjectManifest.expires_at <= self.clock(),
                            ObjectManifest.state == "invalidated",
                        ),
                    )
                    .order_by(ObjectManifest.expires_at, ObjectManifest.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            objects = []
            for row in rows:
                row.state = "invalidated"
                objects.append(
                    (row.id, ExportManifest.model_validate_json(row.manifest_json).object_ref)
                )
        for manifest_id, object_ref in objects:
            await store.revoke("expiry:" + manifest_id, object_ref)
            async with self.database.sessions.begin() as db:
                await db.execute(
                    delete(ObjectManifest).where(
                        ObjectManifest.id == manifest_id, ObjectManifest.state == "invalidated"
                    )
                )
        return len(objects)
