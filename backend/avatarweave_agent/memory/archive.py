"""M18 encrypted open-format archive with verify-before-switch and current read barriers."""

import asyncio
import fcntl
import hashlib
import os
import re
import zlib
from pathlib import Path
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, ValidationError
from sqlalchemy import select

from .contracts import (
    ArchiveManifest,
    ArchiveOutput,
    Contract,
    EvidenceCapsule,
    MemoryRecord,
    ModuleResult,
    SourceDescriptor,
)
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .tables import Capsule, FactVersion, ObjectManifest, Tombstone
from .utils import utc


class ArchiveBundle(Contract):
    schema_version: Literal[2] = 2
    records: Annotated[list[MemoryRecord], Field(max_length=1000)]
    capsules: Annotated[list[EvidenceCapsule], Field(max_length=1000)]
    source_metadata: Annotated[list[SourceDescriptor], Field(max_length=1000)]


class KeyProvider(Protocol):
    def get_key(self, version: str) -> bytes: ...


class FileKeyProvider:
    """Development adapter. Production can supply a reviewed KMS-backed provider."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def get_key(self, version):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", version):
            raise ValueError("Invalid key version")
        path = self.root / (version + ".key")
        if path.stat().st_mode & 0o077:
            raise PermissionError("Archive key file must not be group/world readable")
        return path.read_bytes().strip()


class EncryptedObjectStore:
    def __init__(self, root, keys: KeyProvider, key_version="v1", max_running=2):
        self.root = Path(root).resolve()
        self.keys, self.key_version, self.max_running = keys, key_version, max_running
        self.tasks = {}

    def _path(self, object_ref):
        if not re.fullmatch(r"[a-f0-9]{32}", object_ref):
            raise ValueError("Invalid object reference")
        return self.root / (object_ref + ".fernet")

    async def _io(self, operation_id, fn, *args):
        if len(self.tasks) >= self.max_running:
            raise MemoryFault("overloaded", "Object I/O capacity is busy", retryable=True)
        handle = uuid4().hex
        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        self.tasks[handle] = (operation_id, task)

        def finished(done):
            self.tasks.pop(handle, None)
            # Retrieve the exception without serializing potentially private bytes.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def remaining(self, operation_id):
        return [
            key
            for key, (owner, task) in self.tasks.items()
            if owner == operation_id and not task.done()
        ]

    async def close(self):
        await asyncio.shield(
            asyncio.gather(*(task for _, task in list(self.tasks.values())), return_exceptions=True)
        )

    def _write(self, object_ref, plaintext):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(object_ref)
        with path.with_suffix(".lock").open("a+b") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.with_suffix(".revoked").exists():
                raise ValueError("Object write was revoked")
            encrypted = Fernet(self.keys.get_key(self.key_version)).encrypt(
                zlib.compress(plaintext)
            )
            temporary = path.with_suffix("." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as output:
                    os.chmod(temporary, 0o600)
                    output.write(encrypted)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def _revoke(self, object_ref):
        """Persistent object tombstone also fences delayed writers in other processes."""
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(object_ref)
        with path.with_suffix(".lock").open("a+b") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = path.with_suffix(".revoked")
            with marker.open("ab") as output:
                os.chmod(marker, 0o600)
                output.flush()
                os.fsync(output.fileno())
            path.unlink(missing_ok=True)

    async def revoke(self, operation_id, object_ref):
        await self._io(operation_id, self._revoke, object_ref)

    def _read(self, object_ref, key_version, limit):
        path = self._path(object_ref)
        if path.stat().st_size > limit * 2 + 4096:
            raise ValueError("Encrypted object exceeds the read budget")
        decrypted = Fernet(self.keys.get_key(key_version)).decrypt(path.read_bytes())
        decoder = zlib.decompressobj()
        plaintext = decoder.decompress(decrypted, limit + 1)
        if len(plaintext) > limit or decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("Archive decompression exceeds the read budget")
        return plaintext

    async def write(self, operation_id, object_ref, plaintext):
        if len(plaintext) > 8 * 1024 * 1024:
            raise MemoryFault("budget_exceeded", "Archive shard exceeds 8 MiB")
        await self._io(operation_id, self._write, object_ref, plaintext)

    async def read(self, operation_id, object_ref, key_version, limit):
        return await self._io(operation_id, self._read, object_ref, key_version, limit)


class ArchiveMixin:
    async def archive(self, request, store):
        async def operation():
            if store is None:
                raise MemoryFault("storage_unavailable", "Archive/key provider is not configured")
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())

                revision = account.memory_revision
                if request.operation == "write":
                    if not request.record_refs:
                        raise MemoryFault(
                            "invalid_transition", "Archive write requires explicit records"
                        )
                    records, capsules, descriptors = [], {}, {}
                    for ref in request.record_refs:
                        row = await db.scalar(
                            select(FactVersion).where(
                                *fact_scope(request.scope),
                                FactVersion.memory_id == ref.object_id,
                                FactVersion.version == ref.version,
                                FactVersion.system_to.is_(None),
                                FactVersion.status == "active",
                            )
                        )
                        if row is None:
                            raise MemoryFault("not_found", "Archive source is unavailable")
                        record = load_record(row)
                        records.append(record)
                        for pointer in record.sources:
                            source = await self._source(
                                db, request.scope, pointer.event_id, pointer.source_revision
                            )
                            capsule_ids = []
                            if pointer.capsule_id:
                                capsule = await db.scalar(
                                    select(Capsule).where(
                                        Capsule.id == pointer.capsule_id,
                                        Capsule.account_id == account.id,
                                        Capsule.companion_id == request.scope.companion_id,
                                    )
                                )
                                if capsule:
                                    value = EvidenceCapsule.model_validate_json(
                                        capsule.capsule_json
                                    )
                                    capsules[value.capsule_id] = value
                                    capsule_ids.append(value.capsule_id)
                            descriptors[(source.event_id, source.revision)] = SourceDescriptor(
                                event_id=source.event_id,
                                revision=source.revision,
                                scope=request.scope,
                                session_id=source.session_id,
                                origin=source.origin,
                                occurred_at=utc(source.occurred_at),
                                raw_available=False,
                                capsule_ids=capsule_ids,
                                revoked=False,
                            )
                    bundle = ArchiveBundle(
                        records=records,
                        capsules=list(capsules.values()),
                        source_metadata=list(descriptors.values()),
                    )
                    plaintext = bundle.model_dump_json().encode()
                    if len(plaintext) > 8 * 1024 * 1024:
                        raise MemoryFault("budget_exceeded", "Archive input requires another shard")
                    manifest = ArchiveManifest(
                        archive_id=hashlib.sha256(
                            (account.id + request.meta.operation_id).encode()
                        ).hexdigest()[:32],
                        scope=request.scope,
                        time_range=records[0].statement.valid_time,
                        object_ref=uuid4().hex,
                        content_sha256=hashlib.sha256(plaintext).hexdigest(),
                        schema_version=2,
                        encryption_key_version=store.key_version,
                        record_count=len(records),
                        memory_revision=revision,
                        privacy_epoch=account.privacy_epoch,
                        state="staged",
                    )
                else:
                    if request.archive is None:
                        raise MemoryFault("invalid_transition", "Archive manifest is required")
                    row = await db.scalar(
                        select(ObjectManifest).where(
                            ObjectManifest.id == request.archive.archive_id,
                            ObjectManifest.account_id == account.id,
                            ObjectManifest.companion_id == request.scope.companion_id,
                            ObjectManifest.kind == "archive",
                        )
                    )
                    if row is None or row.state == "invalidated":
                        raise MemoryFault("not_found", "Archive is unavailable")
                    manifest = ArchiveManifest.model_validate_json(row.manifest_json)
                    if (
                        manifest.privacy_epoch != account.privacy_epoch
                        or manifest != request.archive
                    ):
                        raise MemoryFault(
                            "privacy_changed", "Archive authorization or version changed"
                        )
            if request.operation == "write":
                # Persist the object intent before scheduling non-cancellable I/O.
                async with self.database.sessions.begin() as db:
                    account = await self._account(db, request.scope, lock=True)
                    self._privacy(account, request)
                    if account.memory_revision != revision:
                        raise MemoryFault("version_conflict", "Archive source changed")
                    previous = await db.get(ObjectManifest, manifest.archive_id)
                    if previous:
                        stored = ArchiveManifest.model_validate_json(previous.manifest_json)
                        if (
                            previous.state == "invalidated"
                            or stored.content_sha256 != manifest.content_sha256
                        ):
                            raise MemoryFault(
                                "version_conflict", "Archive operation input was changed or revoked"
                            )
                        manifest = stored
                    else:
                        db.add(
                            ObjectManifest(
                                id=manifest.archive_id,
                                account_id=account.id,
                                companion_id=request.scope.companion_id,
                                kind="archive",
                                state="staged",
                                manifest_json=manifest.model_dump_json(),
                            )
                        )
            try:
                if request.operation == "write":
                    await store.write(request.meta.operation_id, manifest.object_ref, plaintext)
                plaintext = await store.read(
                    request.meta.operation_id,
                    manifest.object_ref,
                    manifest.encryption_key_version,
                    min(request.max_read_bytes, 8 * 1024 * 1024),
                )
                if hashlib.sha256(plaintext).hexdigest() != manifest.content_sha256:
                    raise ValueError("Archive checksum mismatch")
                bundle = ArchiveBundle.model_validate_json(plaintext)
                if len(bundle.records) != manifest.record_count:
                    raise ValueError("Archive record count mismatch")
            except (OSError, ValueError, InvalidToken, ValidationError):
                raise MemoryFault(
                    "storage_unavailable", "Archive integrity verification failed"
                ) from None
            async with self.database.sessions.begin() as db:
                account = await self._account(db, request.scope, lock=True)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())

                for record in bundle.records:
                    if record.scope != request.scope or await db.get(
                        Tombstone, (account.id, record.ref.object_id, "memory")
                    ):
                        raise MemoryFault(
                            "privacy_changed", "Archive contains an unavailable memory"
                        )
                    current = await db.scalar(
                        select(FactVersion).where(
                            *fact_scope(request.scope),
                            FactVersion.memory_id == record.ref.object_id,
                            FactVersion.version == record.ref.version,
                            FactVersion.status == "active",
                            FactVersion.system_to.is_(None),
                        )
                    )
                    if current is None:
                        raise MemoryFault("version_conflict", "Archive source version changed")
                    for pointer in record.sources:
                        await self._source(
                            db, request.scope, pointer.event_id, pointer.source_revision
                        )
                for capsule in bundle.capsules:
                    stored = await db.get(Capsule, capsule.capsule_id)
                    if (
                        capsule.scope != request.scope
                        or stored is None
                        or stored.account_id != account.id
                        or stored.companion_id != request.scope.companion_id
                        or stored.capsule_json != capsule.model_dump_json()
                    ):
                        raise MemoryFault(
                            "privacy_changed", "Archive capsule authorization changed"
                        )
                catalog = await db.get(ObjectManifest, manifest.archive_id)
                if catalog is None or catalog.state == "invalidated":
                    raise MemoryFault("privacy_changed", "Archive was revoked during I/O")
                if request.operation == "verify":
                    manifest = manifest.model_copy(update={"state": "active"})
                    catalog = await db.get(ObjectManifest, manifest.archive_id)
                    catalog.state, catalog.manifest_json = "active", manifest.model_dump_json()
                return ModuleResult[ArchiveOutput](
                    status="ok",
                    data=ArchiveOutput(
                        manifest=manifest,
                        restored_records=bundle.records
                        if request.operation in {"read", "promote"}
                        else [],
                        restored_capsules=bundle.capsules
                        if request.operation in {"read", "promote"}
                        else [],
                        source_metadata=bundle.source_metadata,
                        verified=True,
                        next_cursor=None,
                        bytes_processed=len(plaintext),
                        may_remove_online_copy=False,
                    ),
                    visibility=self._visibility(account, None, [], []),
                    warnings=["Online copy retained; physical eviction requires catalog migration"],
                )

        if store and store.remaining not in self.executor.work_providers:
            self.executor.work_providers.append(store.remaining)
        return await self.executor.run(request, operation)
