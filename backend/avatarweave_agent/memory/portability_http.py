"""Authenticated data portability; signed open JSON, bounded encrypted staging."""

import asyncio
import hashlib
import hmac
from datetime import timedelta
from functools import wraps
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import Field, ValidationError
from sqlalchemy import func, select

from .contracts import (
    Contract,
    CreateExportBody,
    ExportInput,
    Identifier,
    ImportBody,
    PublicApiResult,
    PublicExportView,
)
from .execution import MemoryFault
from .http import body_as, body_openapi, fence_for, invalid_body, meta, response, runtime, scope_for
from .portability import ExportManifest
from .portable_graph import PortablePackage, read_package
from .tables import ObjectManifest

router = APIRouter(prefix="/api/v2")


def bounded_transfer(fn):
    @wraps(fn)
    async def run(*args, **kwargs):
        request = kwargs.get("request") or next(arg for arg in args if isinstance(arg, Request))
        rt = runtime(request)
        account = await rt.identity.authenticate(request, write=request.method != "GET")
        if len(rt.transfers) >= 2 or account.id in rt.transfers:
            raise MemoryFault("overloaded", "Data transfer capacity is busy", retryable=True)
        rt.transfers.add(account.id)
        try:
            async with asyncio.timeout(15):
                return await fn(*args, **kwargs)
        except TimeoutError:
            raise MemoryFault(
                "deadline_exceeded", "Data transfer deadline exceeded", retryable=True
            ) from None
        except (OSError, ValueError):
            raise MemoryFault(
                "storage_unavailable", "Package integrity or storage check failed"
            ) from None
        finally:
            rt.transfers.discard(account.id)

    return run


class PortableFile(Contract):
    package: PortablePackage
    key_version: Identifier
    signature: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def store_for(rt):
    if rt.store is None:
        raise MemoryFault("storage_unavailable", "Encrypted archive storage is not configured")
    return rt.store


async def catalog_for(rt, account, export_id):
    async with rt.database.sessions() as db:
        await rt.service._account(db, scope_for(rt, account))
        row = await db.scalar(
            select(ObjectManifest).where(
                ObjectManifest.id == export_id,
                ObjectManifest.account_id == account.id,
                ObjectManifest.companion_id == rt.settings.companion_id,
                ObjectManifest.kind == "export",
            )
        )
        if row is None or row.state in {"invalidated", "revoked", "staged"}:
            raise MemoryFault("not_found", "Export is unavailable")
        manifest = ExportManifest.model_validate_json(row.manifest_json)
        if (
            manifest.expires_at <= rt.service.clock()
            or manifest.privacy_epoch != account.privacy_epoch
        ):
            raise MemoryFault("privacy_changed", "Export authorization expired")
        return manifest


async def run_export(request, body, operation, export_id=None):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    store = store_for(rt)
    scope = scope_for(rt, account)
    event, _ = await rt.service.control_event(
        scope,
        key=body.idempotency_key,
        operation="export_control",
        body=body,
        text=operation + ":" + (export_id or "new"),
        target=export_id or "new",
    )
    # The service rechecks ownership and all tombstones after object I/O.
    result = await rt.service.export(
        ExportInput(
            meta=meta(rt.service, lane="deep_recall", budget_ms=30000),
            scope=scope,
            fence=fence_for(account),
            operation=operation,
            export_id=export_id,
            selected_namespaces=["life", "relationship", "character_lore"],
            include_source_capsules=getattr(body, "include_source_capsules", True),
            include_raw_transcript=getattr(body, "include_raw_transcript", False),
            explicit_user_confirmation_event=event.event_id,
        ),
        store,
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    manifest = (
        await catalog_for(rt, account, result.data.export_id)
        if result.data.state == "ready"
        else None
    )
    return response(
        PublicExportView(
            export_id=result.data.export_id,
            state=result.data.state,
            download_handle=result.data.delivery_handle,
            expires_at=manifest.expires_at if manifest else None,
            conflicts=result.data.conflicts,
        )
    )


@router.post(
    "/exports",
    response_model=PublicApiResult[PublicExportView],
    openapi_extra=body_openapi(CreateExportBody),
)
async def create_export(request: Request):
    body = await body_as(request, CreateExportBody)
    return await run_export(request, body, "create") if body else invalid_body()


@router.get("/exports/{export_id}/download")
@bounded_transfer
async def download(export_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    store = store_for(rt)
    manifest = await catalog_for(rt, account, export_id)
    async with asyncio.timeout(10):
        payload = await store.read(
            uuid4().hex, manifest.object_ref, manifest.encryption_key_version, 8 * 1024 * 1024
        )
    if hashlib.sha256(payload).hexdigest() != manifest.checksum:
        raise MemoryFault("storage_unavailable", "Export integrity check failed")
    package = read_package(payload)
    encoded = package.model_dump_json().encode()
    signature = hmac.new(
        store.keys.get_key(manifest.encryption_key_version), encoded, "sha256"
    ).hexdigest()
    account = await rt.identity.authenticate(request)
    await catalog_for(rt, account, export_id)
    file = PortableFile(
        package=package, key_version=manifest.encryption_key_version, signature=signature
    )
    return Response(
        file.model_dump_json(),
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="avatarweave-memory-v{package.schema_version}.json"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/imports/upload", response_model=PublicApiResult[PublicExportView])
@bounded_transfer
async def upload(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    store = store_for(rt)
    data = bytearray()
    async with asyncio.timeout(10):
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 9 * 1024 * 1024:
                raise MemoryFault("budget_exceeded", "Portable package exceeds upload budget")
    try:
        file = PortableFile.model_validate_json(data)
        plaintext = file.package.model_dump_json().encode()
        expected = hmac.new(store.keys.get_key(file.key_version), plaintext, "sha256").hexdigest()
        verified = hmac.compare_digest(expected, file.signature)
    except (ValidationError, ValueError, OSError):
        verified = False
    if not verified:
        raise MemoryFault("invalid_model_output", "Invalid package or unavailable signing key")
    scope = scope_for(rt, account)
    if file.package.owner_user_id != account.id or file.package.companion_id != scope.companion_id:
        raise MemoryFault("scope_mismatch", "Package owner does not match this account")
    manifest = ExportManifest(
        schema_version=file.package.schema_version,
        export_id=uuid4().hex,
        scope_user_id=account.id,
        companion_id=scope.companion_id,
        object_ref=uuid4().hex,
        encryption_key_version=store.key_version,
        checksum=hashlib.sha256(plaintext).hexdigest(),
        privacy_epoch=account.privacy_epoch,
        record_count=len(file.package.bundle.records),
        expires_at=rt.service.clock() + timedelta(minutes=15),
        state="staged",
    )
    async with rt.database.sessions.begin() as db:
        current = await rt.service._account(db, scope, lock=True)
        if current.privacy_epoch != manifest.privacy_epoch:
            raise MemoryFault("privacy_changed", "Package authorization changed")
        count = await db.scalar(
            select(func.count())
            .select_from(ObjectManifest)
            .where(ObjectManifest.account_id == account.id, ObjectManifest.kind == "export")
        )
        if count >= 32:
            raise MemoryFault("overloaded", "Export staging capacity is full", retryable=True)
        db.add(
            ObjectManifest(
                id=manifest.export_id,
                account_id=account.id,
                companion_id=scope.companion_id,
                kind="export",
                state="staged",
                manifest_json=manifest.model_dump_json(),
                expires_at=manifest.expires_at,
            )
        )
    async with asyncio.timeout(10):
        await store.write(manifest.export_id, manifest.object_ref, plaintext)
    async with rt.database.sessions.begin() as db:
        current = await rt.service._account(db, scope, lock=True)
        row = await db.get(ObjectManifest, manifest.export_id)
        if current.privacy_epoch != manifest.privacy_epoch or row.state == "invalidated":
            raise MemoryFault("privacy_changed", "Package authorization changed during upload")
        manifest = manifest.model_copy(update={"state": "ready"})
        row.state, row.manifest_json = "ready", manifest.model_dump_json()
    return response(
        PublicExportView(
            export_id=manifest.export_id,
            state="ready",
            download_handle=None,
            expires_at=manifest.expires_at,
            conflicts=[],
        ),
        status=201,
    )


@router.post(
    "/imports/dry-run",
    response_model=PublicApiResult[PublicExportView],
    openapi_extra=body_openapi(ImportBody),
)
@router.post(
    "/imports",
    response_model=PublicApiResult[PublicExportView],
    openapi_extra=body_openapi(ImportBody),
)
async def import_package(request: Request):
    body = await body_as(request, ImportBody)
    if body is None:
        return invalid_body()
    dry_run = request.url.path.endswith("/dry-run") or body.dry_run
    if not dry_run and not body.confirmed:
        raise MemoryFault(
            "invalid_transition", "Explicit confirmation is required for import apply"
        )
    return await run_export(
        request, body, "import_dry_run" if dry_run else "import_apply", body.uploaded_package_handle
    )
