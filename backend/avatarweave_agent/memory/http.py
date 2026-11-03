"""Public P1 adapter: authentication constructs scope; clients cannot supply fences.

RTC credentials deliberately remain absent until P3/P5 hooks and cancellation
are verified. Creating a session does not dispatch a private voice Worker yet.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from .config import MemorySettings
from .contracts import (
    ClientCapabilities,
    CreateSessionBody,
    EndSessionBody,
    ErrorInfo,
    ExecutionControl,
    PrivacyPreference,
    ProcessingStatusQuery,
    PublicApiResult,
    PublicEventProcessing,
    PublicIdentityView,
    PublicProcessingStatus,
    PublicSessionView,
    RequestMeta,
    ResumeSessionBody,
    Scope,
    SessionInput,
    UpdatePreferencesBody,
    VersionFence,
    VisibilityReadInput,
)
from .execution import MemoryFault
from .identity import AUTH_COOKIE, CSRF_COOKIE, IdentityService
from .service import MemoryService
from .storage import MemoryDatabase, PreferenceReceipt

router = APIRouter(prefix="/api/v2")


@dataclass
class MemoryRuntime:
    settings: MemorySettings
    database: MemoryDatabase
    service: MemoryService
    identity: IdentityService

    @classmethod
    async def open(cls, settings):
        database = MemoryDatabase(settings.database_url)
        try:
            await database.check_schema()
        except BaseException:
            await database.close()
            raise
        service = MemoryService(
            database, workspace_id=settings.workspace_id, companions={settings.companion_id}
        )
        return cls(settings, database, service, IdentityService(settings, service))


def runtime(request):
    value = getattr(request.app.state, "memory_runtime", None)
    if value is None:
        raise MemoryFault("unsupported_capability", "Memory account service is not configured")
    return value


def response(data, *, status=200, error=None):
    result = PublicApiResult(
        status="partial" if error else "ok", data=data, error=error, trace_id=uuid4().hex
    )
    return JSONResponse(
        result.model_dump(mode="json"), status_code=status, headers={"Cache-Control": "no-store"}
    )


async def fault_handler(request, exc):
    codes = {
        "unauthorized": 401,
        "not_found": 404,
        "scope_mismatch": 404,
        "version_conflict": 409,
        "privacy_changed": 409,
        "invalid_transition": 409,
        "invalid_model_output": 422,
        "overloaded": 429,
        "quota_exceeded": 429,
        "memory_pending": 202,
    }
    result = PublicApiResult(status="error", error=exc.info, trace_id=uuid4().hex)
    headers = {"Cache-Control": "no-store"}
    if exc.info.retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, (exc.info.retry_after_ms + 999) // 1000))
    return JSONResponse(
        result.model_dump(mode="json"), status_code=codes.get(exc.info.code, 503), headers=headers
    )


async def body_as(request, model):
    # Read incrementally: do not allocate an arbitrarily large chunked body.
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > 65536:
            raise MemoryFault("quota_exceeded", "Request body exceeds 64 KiB")
        payload.extend(chunk)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise MemoryFault("invalid_model_output", "JSON request body required")
    try:
        return model.model_validate_json(bytes(payload))
    except ValidationError:
        # Do not return Pydantic's input values (possibly private text).
        return None


def invalid_body():
    result = PublicApiResult(
        status="error",
        error=ErrorInfo(
            code="invalid_model_output", message="Invalid request body", retryable=False
        ),
        trace_id=uuid4().hex,
    )
    return JSONResponse(
        result.model_dump(mode="json"), status_code=422, headers={"Cache-Control": "no-store"}
    )


def body_openapi(model):
    """Inline the small public input schemas while retaining strict JSON parsing."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(value):
        if isinstance(value, list):
            return [inline(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].split("/")[-1]])
            return {key: inline(item) for key, item in value.items()}
        return value

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": inline(schema)}},
        }
    }


def meta(service, lane="control", *, budget_ms=300):
    now = service.clock()
    return RequestMeta(
        operation_id=uuid4().hex,
        trace_id=uuid4().hex,
        now=now,
        deadline_at=now + timedelta(milliseconds=budget_ms),
        algorithm_version="foundation.v1",
        execution=ExecutionControl(
            lane=lane,
            budget_profile_id="foundation-control.v1",
            cancellation_token_id=uuid4().hex,
            budget_ms=budget_ms,
            max_queue_wait_ms=0,
        ),
    )


def scope_for(rt, account, companion=None):
    return Scope(
        workspace_id=rt.settings.workspace_id,
        user_id=account.id,
        companion_id=companion or rt.settings.companion_id,
    )


def fence_for(account):
    return VersionFence(
        privacy_epoch=account.privacy_epoch, memory_revision=account.memory_revision, owner_epoch=0
    )


@router.get("/auth/login")
async def login(request: Request):
    rt = runtime(request)
    # The signed temporary cookie is HttpOnly and bounded to the login handshake.
    request.session.clear()
    return await rt.identity.oauth.account.authorize_redirect(
        request, rt.settings.public_origin + "/api/v2/auth/callback"
    )


@router.get("/auth/callback")
async def callback(request: Request):
    rt = runtime(request)
    try:
        token = await rt.identity.oauth.account.authorize_access_token(
            request,
            claims_options={"iss": {"essential": True, "values": [rt.settings.oidc_issuer]}},
            leeway=30,
        )
        if "id_token" not in token or "userinfo" not in token:
            raise MemoryFault("unauthorized", "OIDC ID token required")
        result = RedirectResponse(rt.settings.public_origin + "/", status_code=303)
        await rt.identity.establish(token["userinfo"], result)
        request.session.clear()
        result.headers["Cache-Control"] = "no-store"
        return result
    except MemoryFault:
        request.session.clear()
        raise
    except Exception:  # noqa: BLE001 -- sanitize every external-provider error at this boundary
        # Provider exceptions may include tokens/code/claims. Do not serialize or log them.
        request.session.clear()
        raise MemoryFault("unauthorized", "OIDC sign-in failed") from None


@router.post("/auth/logout")
async def logout(request: Request):
    rt = runtime(request)
    await rt.identity.logout(request)
    result = response(
        PublicIdentityView(
            state="anonymous",
            companion_ids=[rt.settings.companion_id],
            long_term_save_allowed=False,
            preferences_version=1,
        )
    )
    for name in (AUTH_COOKIE, CSRF_COOKIE):
        result.delete_cookie(name, secure=True, httponly=name == AUTH_COOKIE, samesite="lax")
    return result


@router.get("/me", response_model=PublicApiResult[PublicIdentityView])
async def me(request: Request):
    rt = runtime(request)
    if AUTH_COOKIE not in request.cookies:
        return response(
            PublicIdentityView(
                state="anonymous",
                companion_ids=[rt.settings.companion_id],
                long_term_save_allowed=False,
                preferences_version=1,
            )
        )
    account = await rt.identity.authenticate(request)
    prefs = PrivacyPreference.model_validate_json(account.preferences_json)
    return response(
        PublicIdentityView(
            state="account",
            companion_ids=[rt.settings.companion_id],
            long_term_save_allowed=True,
            preferences_version=prefs.preference_version,
        )
    )


@router.get("/memory-preferences", response_model=PublicApiResult[PrivacyPreference])
async def preferences(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    return response(PrivacyPreference.model_validate_json(account.preferences_json))


@router.put(
    "/memory-preferences",
    response_model=PublicApiResult[PrivacyPreference],
    openapi_extra=body_openapi(UpdatePreferencesBody),
)
async def update_preferences(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, UpdatePreferencesBody)
    if body is None:
        return invalid_body()
    # P1 can grant initial consent. Subsequent changes need M19 invalidation and
    # cancellation propagation, so fail explicitly instead of accepting unsafe updates.
    async with rt.database.sessions.begin() as db:
        row = await rt.service._account(db, scope_for(rt, account), lock=True)
        receipt = await db.get(PreferenceReceipt, (account.id, body.idempotency_key))
        if receipt:
            if receipt.request_json != body.model_dump_json():
                raise MemoryFault("version_conflict", "Idempotency key already used")
            return response(PrivacyPreference.model_validate_json(receipt.result_json))
        old = PrivacyPreference.model_validate_json(row.preferences_json)
        candidate = PrivacyPreference(
            **body.model_dump(exclude={"expected_version", "idempotency_key"}),
            preference_version=old.preference_version + 1,
        )
        if row.source_watermark != 0:
            raise MemoryFault(
                "unsupported_capability", "Preference changes after input require M19"
            )
        if old.preference_version != body.expected_version:
            raise MemoryFault("version_conflict", "Preferences changed")
        if candidate.cross_companion_share != "none" or candidate.permitted_shared_predicates:
            raise MemoryFault("unsupported_capability", "Cross-companion sharing is not enabled")
        row.preferences_json = candidate.model_dump_json()
        row.privacy_epoch += 1
        db.add(
            PreferenceReceipt(
                account_id=account.id,
                idempotency_key=body.idempotency_key,
                request_json=body.model_dump_json(),
                result_json=candidate.model_dump_json(),
            )
        )
    return response(candidate)


async def session_view(rt, account, session_id):
    async with rt.database.sessions() as db:
        session = await rt.service._session(db, scope_for(rt, account), session_id)
        return PublicSessionView(
            session_id=session.id,
            companion_id=session.companion_id,
            status=session.status,
            memory_enabled=session.memory_enabled,
            account_required_for_save=True,
            rtc=None,
            resume_watermark=account.source_watermark,
        )


@router.post(
    "/sessions",
    response_model=PublicApiResult[PublicSessionView],
    status_code=201,
    openapi_extra=body_openapi(CreateSessionBody),
)
async def create_session(request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    body = await body_as(request, CreateSessionBody)
    if body is None:
        return invalid_body()
    result = await rt.service.session(
        SessionInput(
            meta=meta(rt.service),
            scope=scope_for(rt, account, body.companion_id),
            fence=fence_for(account),
            operation="create",
            session_id=None,
            capabilities=body.capabilities,
            memory_enabled=body.memory_enabled,
            idempotency_key=body.idempotency_key,
        )
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    return response(await session_view(rt, account, result.data.session_id), status=201)


@router.post(
    "/sessions/{session_id}/resume",
    response_model=PublicApiResult[PublicSessionView],
    openapi_extra=body_openapi(ResumeSessionBody),
)
@router.post(
    "/sessions/{session_id}/end",
    response_model=PublicApiResult[PublicSessionView],
    openapi_extra=body_openapi(EndSessionBody),
)
async def change_session(session_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request, write=True)
    operation = "end" if request.url.path.endswith("/end") else "resume"
    body = await body_as(request, EndSessionBody if operation == "end" else ResumeSessionBody)
    if body is None:
        return invalid_body()
    async with rt.database.sessions() as db:
        session = await rt.service._session(db, scope_for(rt, account), session_id)
        caps = ClientCapabilities.model_validate_json(session.capabilities_json)
    result = await rt.service.session(
        SessionInput(
            meta=meta(rt.service),
            scope=scope_for(rt, account),
            fence=fence_for(account),
            operation=operation,
            session_id=session_id,
            capabilities=caps if operation == "end" else body.capabilities,
            memory_enabled=session.memory_enabled,
            idempotency_key=body.idempotency_key,
        )
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    return response(await session_view(rt, account, session_id))


@router.get(
    "/sessions/{session_id}/memory-status", response_model=PublicApiResult[PublicProcessingStatus]
)
async def processing(session_id: str, request: Request):
    rt = runtime(request)
    account = await rt.identity.authenticate(request)
    try:
        query = ProcessingStatusQuery(event_ids=request.query_params.getlist("event_ids"))
    except ValidationError:
        return invalid_body()
    result = await rt.service.visibility(
        VisibilityReadInput(
            meta=meta(rt.service),
            scope=scope_for(rt, account),
            fence=fence_for(account),
            session_id=session_id,
            target_event_ids=query.event_ids,
        )
    )
    if result.error:
        raise MemoryFault(result.error.code, result.error.message, retryable=result.error.retryable)
    data = result.data
    states = {s.event_id: s for s in data.visibility.target_states}
    return response(
        PublicProcessingStatus(
            items=[
                PublicEventProcessing(
                    event_id=event_id,
                    source_revision=states[event_id].revision,
                    source_saved=True,
                    usable_in_this_session=event_id in data.visibility.overlay_event_ids,
                    cross_session_source_visible=event_id in data.source_readable_event_ids,
                    structured_state=(
                        "completed"
                        if event_id in data.structured_ready_event_ids
                        else {"persisted": "pending", "provisional": "pending"}.get(
                            states[event_id].state, states[event_id].state
                        )
                    ),
                    index_state="completed" if event_id in data.indexed_event_ids else "pending",
                    provisional=event_id not in data.structured_ready_event_ids,
                    observed_at=data.visibility.observed_at,
                    retry_after_ms=data.retry_after_ms,
                )
                for event_id in query.event_ids
            ]
        )
    )
