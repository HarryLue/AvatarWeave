"""P0/P1 adversarial regression; runs on SQLite AND CI's real PostgreSQL."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from avatarweave_agent.memory.contracts import (
    ClientCapabilities,
    InputEventInput,
    MemoryVisibility,
    RepositoryInput,
    Scope,
    SessionInput,
    SourceEvent,
    VersionFence,
    VersionRef,
    VisibilityReadInput,
)
from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.service import MemoryService, default_preferences
from avatarweave_agent.memory.storage import (
    Account,
    EventHead,
    MemoryBase,
    MemoryDatabase,
    Outbox,
    SourceRevision,
)


@pytest_asyncio.fixture
async def foundation(tmp_path):
    url = (
        os.environ["POSTGRES_TEST_URL"]
        if os.getenv("TEST_DATABASE") == "postgres"
        else f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"
    )
    database = MemoryDatabase(url)
    await database.create_schema()
    await database.check_schema()
    service = MemoryService(database, companions={"companion", "other"})
    try:
        yield service
    finally:
        # Only the new schema in the explicitly isolated TEST database.
        async with database.engine.begin() as conn:
            await conn.run_sync(MemoryBase.metadata.drop_all)
        await database.close()


def capabilities():
    return ClientCapabilities(
        command_versions=[1],
        action_cancel=False,
        action_completed_receipt=False,
        playback_granularity="none",
        media_clock_supported=False,
    )


async def account(service, sub=None):
    account_id = await service.resolve_account("https://issuer.test", sub or uuid4().hex)
    return Scope(workspace_id="default", user_id=account_id, companion_id="companion")


async def session(service, scope, *, enabled=True, key=None):
    async with service.database.sessions.begin() as db:
        row = await db.get(Account, scope.user_id)
        prefs = default_preferences().model_copy(update={"long_term_memory_enabled": enabled})
        row.preferences_json = prefs.model_dump_json()
    request = SessionInput(
        meta=meta(service),
        scope=scope,
        fence=VersionFence(privacy_epoch=0, memory_revision=0, owner_epoch=0),
        operation="create",
        session_id=None,
        capabilities=capabilities(),
        memory_enabled=enabled,
        idempotency_key=key or uuid4().hex,
    )
    result = await service.session(request)
    assert result.status == "ok", result
    return result.data, request


async def event_request(service, scope, *, enabled=True):
    view, _ = await session(service, scope, enabled=enabled)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    now = service.clock()
    event = SourceEvent(
        event_id=uuid4().hex,
        revision=1,
        session_id=view.session_id,
        turn_id=uuid4().hex,
        scope=scope,
        origin="user_transcript",
        content="我喜欢喝茶🌱",
        occurred_at=now,
        received_at=now,
        is_final=True,
    )
    return InputEventInput(
        meta=meta(service, budget_ms=1000),
        scope=scope,
        fence=VersionFence(privacy_epoch=0, memory_revision=0, owner_epoch=owner),
        session_id=view.session_id,
        event=event,
        memory_enabled=enabled,
        expected_turn_revision=0,
    )


def read_request(service, write, *, scope=None, version=1):
    return RepositoryInput(
        meta=meta(service),
        scope=scope or write.scope,
        fence=write.fence,
        operation="read_sources",
        refs=[VersionRef(object_id=write.event.event_id, version=version)],
    )


@pytest.mark.asyncio
async def test_oidc_mapping_stable_and_issuer_separated(foundation):
    service = foundation
    first, second = await asyncio.gather(
        service.resolve_account("https://issuer.test", "stable-sub"),
        service.resolve_account("https://issuer.test", "stable-sub"),
    )
    assert first == second
    assert await service.resolve_account("https://other.test", "stable-sub") != first


@pytest.mark.asyncio
async def test_final_source_atomic_and_idempotent(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    results = await asyncio.gather(service.input_event(request), service.input_event(request))
    assert [r.status for r in results] == ["ok", "ok"]
    assert all(r.data.persisted for r in results)
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SourceRevision)) == 1
        assert await db.scalar(select(func.count()).select_from(Outbox)) == 1
        row = await db.get(Account, request.scope.user_id)
        assert row.source_watermark == 1
    result = await service.repository(read_request(service, request))
    assert result.data.sources == [request.event]
    assert result.execution.cleanup == "released"


@pytest.mark.asyncio
async def test_revision_invalidates_old_source_and_task(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    assert (await service.input_event(request)).status == "ok"
    revised = request.model_copy(
        update={
            "expected_turn_revision": 1,
            "event": request.event.model_copy(
                update={
                    "revision": 2,
                    "content": "我喜欢喝水",
                    "supersedes": VersionRef(object_id=request.event.event_id, version=1),
                }
            ),
        }
    )
    result = await service.input_event(revised)
    assert result.status == "ok"
    assert result.visibility.source_watermark == 2
    assert result.visibility.structured_settled_watermark == 1
    assert (await service.repository(read_request(service, request))).error.code == "not_found"
    assert (await service.repository(read_request(service, request, version=2))).data.sources[
        0
    ].content == "我喜欢喝水"
    assert (await service.input_event(request)).error.code == "version_conflict"
    async with service.database.sessions() as db:
        jobs = list(await db.scalars(select(Outbox).order_by(Outbox.source_revision)))
        assert [j.state for j in jobs] == ["obsolete", "queued"]


@pytest.mark.asyncio
async def test_same_revision_different_content_fails(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    await service.input_event(request)
    other = request.model_copy(
        update={"event": request.event.model_copy(update={"content": "篡改"})}
    )
    assert (await service.input_event(other)).error.code == "version_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["account", "companion", "workspace"])
async def test_read_isolation(foundation, mode):
    service = foundation
    request = await event_request(service, await account(service))
    await service.input_event(request)
    scope = (
        await account(service)
        if mode == "account"
        else request.scope.model_copy(
            update={"companion_id": "other"} if mode == "companion" else {"workspace_id": "other"}
        )
    )
    result = await service.repository(read_request(service, request, scope=scope))
    assert result.status == "error" and result.error.code == "not_found"
    assert request.event.content not in result.model_dump_json()


@pytest.mark.asyncio
async def test_no_memory_has_no_source_body_or_outbox(foundation):
    service = foundation
    request = await event_request(service, await account(service), enabled=False)
    result = await service.input_event(request)
    assert result.status == "ok" and result.data.persisted is False
    async with service.database.sessions() as db:
        for table in (SourceRevision, Outbox, EventHead):
            assert await db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.asyncio
async def test_interim_scope_owner_and_privacy_fences(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    changes = [
        ({"event": request.event.model_copy(update={"is_final": False})}, "invalid_transition"),
        (
            {
                "event": request.event.model_copy(
                    update={"scope": request.scope.model_copy(update={"user_id": "other"})}
                )
            },
            "scope_mismatch",
        ),
        ({"fence": request.fence.model_copy(update={"owner_epoch": 0})}, "stale_generation"),
        ({"fence": request.fence.model_copy(update={"privacy_epoch": 99})}, "privacy_changed"),
    ]
    for change, code in changes:
        result = await service.input_event(request.model_copy(update=change))
        assert result.error.code == code
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Outbox)) == 0


@pytest.mark.asyncio
async def test_queue_rejection_rolls_back_source(foundation):
    service = foundation
    service.max_user_outbox = 0
    request = await event_request(service, await account(service))
    result = await service.input_event(request)
    assert result.error.code == "overloaded"
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SourceRevision)) == 0
        assert (await db.get(Account, request.scope.user_id)).source_watermark == 0


@pytest.mark.asyncio
async def test_contiguous_watermark_does_not_jump_holes(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    await service.input_event(request)
    another = request.model_copy(
        update={
            "event": request.event.model_copy(
                update={"event_id": uuid4().hex, "turn_id": uuid4().hex}
            )
        }
    )
    await service.input_event(another)
    async with service.database.sessions.begin() as db:
        last = await db.get(SourceRevision, (another.event.event_id, 1))
        last.state = "rejected"
        last.structured_settled = last.index_settled = True
        await db.flush()
        row = await db.get(Account, request.scope.user_id)
        await service.advance_watermarks(db, row)
        assert row.structured_watermark == row.index_watermark == 0
        first = await db.get(SourceRevision, (request.event.event_id, 1))
        first.state = "rejected"
        first.structured_settled = first.index_settled = True
        await db.flush()
        await service.advance_watermarks(db, row)
        assert row.structured_watermark == row.index_watermark == 2


@pytest.mark.asyncio
async def test_cross_session_status_is_provisional(foundation):
    service = foundation
    scope = await account(service)
    request = await event_request(service, scope)
    await service.input_event(request)
    second, _ = await session(service, scope)
    result = await service.visibility(
        VisibilityReadInput(
            meta=meta(service),
            scope=scope,
            fence=request.fence,
            session_id=second.session_id,
            target_event_ids=[request.event.event_id],
        )
    )
    assert result.data.source_readable_event_ids == [request.event.event_id]
    assert result.data.structured_ready_event_ids == []
    assert result.visibility.overlay_event_ids == []
    assert result.visibility.target_states[0].state == "persisted"


@pytest.mark.asyncio
async def test_retention_reads_metadata_and_erases_raw(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    await service.input_event(request)
    future = service.clock() + timedelta(days=91)
    service.clock = lambda: future
    service.executor.clock = service.clock
    result = await service.repository(read_request(service, request))
    assert result.status == "ok"
    assert result.data.sources == []
    assert result.data.source_metadata[0].raw_available is False
    assert await service.expire_raw_batch() == 1
    async with service.database.sessions() as db:
        assert (await db.get(SourceRevision, (request.event.event_id, 1))).source_json is None


@pytest.mark.asyncio
async def test_worker_lease_takeover_and_room_binding(foundation):
    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    with pytest.raises(MemoryFault):
        await service.claim_owner(scope, view.session_id, "different-room", "worker")
    assert await service.claim_owner(scope, view.session_id, view.room_name, "first") == 1
    with pytest.raises(MemoryFault):
        await service.claim_owner(scope, view.session_id, view.room_name, "second")
    now = service.clock()
    service.clock = lambda: now + timedelta(seconds=31)
    assert await service.claim_owner(scope, view.session_id, view.room_name, "second") == 2
    with pytest.raises(MemoryFault):
        await service.renew_owner(scope, view.session_id, "first", 1)


@pytest.mark.asyncio
async def test_session_create_end_idempotence_and_quota(foundation):
    service = foundation
    scope = await account(service)
    view, create = await session(service, scope)
    assert (await service.session(create)).data.session_id == view.session_id
    await session(service, scope)
    third = create.model_copy(update={"idempotency_key": "third"})
    assert (await service.session(third)).error.code == "quota_exceeded"
    end = create.model_copy(
        update={"operation": "end", "session_id": view.session_id, "idempotency_key": "end"}
    )
    assert (await service.session(end)).data.status == "ended"
    epoch = (await service.session(end)).data.owner_epoch
    assert epoch == 1
    assert (await service.session(third)).status == "ok"


@pytest.mark.asyncio
async def test_cancellation_releases_slot(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(service.executor.run(request, work))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.executor.running == 0 and not service.executor.users


@pytest.mark.asyncio
async def test_deadline_releases_slot_and_reports(foundation):
    service = foundation
    request = await event_request(service, await account(service))
    request = request.model_copy(update={"meta": meta(service, budget_ms=5)})

    async def work():
        await asyncio.Event().wait()

    result = await service.executor.run(request, work)
    assert result.error.code == "deadline_exceeded"
    assert result.execution.cleanup == "released"
    assert service.executor.running == 0


def test_visibility_contract_rejects_impossible_watermarks():
    with pytest.raises(ValueError):
        MemoryVisibility(
            source_watermark=1,
            structured_settled_watermark=2,
            index_settled_watermark=0,
            overlay_session_id=None,
            overlay_event_ids=[],
            target_states=[],
            observed_at=datetime.now(UTC),
        )


@pytest_asyncio.fixture
async def memory_http(foundation):
    import httpx
    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware

    from avatarweave_agent.memory.config import MemorySettings
    from avatarweave_agent.memory.http import MemoryRuntime, fault_handler, router
    from avatarweave_agent.memory.identity import IdentityService

    settings = MemorySettings(
        _env_file=None,
        enabled=False,
        public_origin="https://memory.test",
        oidc_issuer="https://issuer.test",
        oidc_client_id="test-client",
        session_secret="test-only-signing-secret-at-least-32-characters",
    )
    identity = IdentityService(settings, foundation)
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="__Host-avatarweave-oidc",
        https_only=True,
        max_age=600,
    )
    app.include_router(router)
    app.add_exception_handler(MemoryFault, fault_handler)
    app.state.memory_runtime = MemoryRuntime(settings, foundation.database, foundation, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as client:
        yield client, app.state.memory_runtime, app


@pytest.mark.asyncio
async def test_http_auth_csrf_schema_and_opaque_views(memory_http):
    from starlette.responses import Response

    from avatarweave_agent.memory.identity import CSRF_COOKIE

    client, rt, app = memory_http
    assert (await client.get("/api/v2/me")).json()["data"]["state"] == "anonymous"
    assert (await client.post("/api/v2/sessions", json={})).status_code == 401
    issued = Response()
    await rt.identity.establish({"iss": "https://issuer.test", "sub": "browser-account"}, issued)
    for raw in issued.raw_headers:
        if raw[0] == b"set-cookie":
            client.cookies.extract_cookies(
                __import__("httpx").Response(
                    200,
                    headers=[raw],
                    request=__import__("httpx").Request("GET", "https://memory.test/"),
                )
            )
    assert (await client.get("/api/v2/me")).json()["data"]["state"] == "account"
    body = {
        "companion_id": "companion",
        "memory_enabled": False,
        "capabilities": capabilities().model_dump(),
        "idempotency_key": "create",
    }
    assert (await client.post("/api/v2/sessions", json=body)).status_code == 401
    client.headers.update(
        {"Origin": rt.settings.public_origin, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
    )
    bad = await client.post("/api/v2/sessions", json={**body, "scope": {"user_id": "other"}})
    assert bad.status_code == 422
    assert (
        await client.post("/api/v2/sessions", json={**body, "memory_enabled": "false"})
    ).status_code == 422
    created = await client.post("/api/v2/sessions", json=body)
    assert created.status_code == 201
    view = created.json()["data"]
    assert view["rtc"] is None
    assert all(key not in view for key in ("scope", "owner_epoch", "room_name", "privacy_epoch"))
    assert (await client.post("/api/v2/sessions", json=body)).json()["data"]["session_id"] == view[
        "session_id"
    ]
    huge = await client.post(
        "/api/v2/sessions", content=b"x" * 65537, headers={"Content-Type": "application/json"}
    )
    assert huge.status_code == 429 and "Retry-After" not in huge.headers
    schema = app.openapi()["paths"]["/api/v2/sessions"]["post"]["requestBody"]
    assert schema["content"]["application/json"]["schema"]["additionalProperties"] is False
    assert (await client.post("/api/v2/auth/logout")).status_code == 200
    assert (await client.post("/api/v2/sessions", json=body)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "nonce", "issuer", "audience", "expired", "state"])
async def test_oidc_code_pkce_signature_and_claim_checks(memory_http, invalid):
    import base64
    import hashlib
    import time
    from urllib.parse import parse_qs, urlsplit

    from authlib.integrations.httpx_client._compat import httpx2 as oauth_http
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from avatarweave_agent.memory.identity import AUTH_COOKIE

    client, rt, _ = memory_http
    key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    handshake = {}

    def provider(request):
        if request.url.path == "/.well-known/openid-configuration":
            return oauth_http.Response(
                200,
                json={
                    "issuer": "https://issuer.test",
                    "authorization_endpoint": "https://issuer.test/authorize",
                    "token_endpoint": "https://issuer.test/token",
                    "jwks_uri": "https://issuer.test/jwks",
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
            )
        if request.url.path == "/jwks":
            return oauth_http.Response(200, json={"keys": [key.as_dict(private=False)]})
        assert request.url.path == "/token"
        form = parse_qs(request.content.decode())
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == handshake["code_challenge"][0]
        now = int(time.time())
        claims = {
            "iss": "https://issuer.test",
            "sub": "oidc-account",
            "aud": "test-client",
            "iat": now,
            "exp": now + 300,
            "nonce": handshake["nonce"][0],
        }
        if invalid == "nonce":
            claims["nonce"] = "wrong"
        elif invalid == "issuer":
            claims["iss"] = "https://other.test"
        elif invalid == "audience":
            claims["aud"] = "other-client"
        elif invalid == "expired":
            claims["exp"] = now - 600
        return oauth_http.Response(
            200,
            json={
                "access_token": "provider-secret",
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": jwt.encode({"alg": "RS256", "kid": "test-key"}, claims, key),
            },
        )

    rt.identity.oauth.account.client_kwargs["transport"] = oauth_http.MockTransport(provider)
    start = await client.get("/api/v2/auth/login")
    assert start.status_code == 302
    handshake.update(parse_qs(urlsplit(start.headers["location"]).query))
    assert handshake["response_type"] == ["code"]
    assert handshake["code_challenge_method"] == ["S256"]
    state = "wrong" if invalid == "state" else handshake["state"][0]
    finish = await client.get(
        "/api/v2/auth/callback", params={"state": state, "code": "one-use-code"}
    )
    if invalid:
        assert finish.status_code == 401
        assert AUTH_COOKIE not in client.cookies
    else:
        assert finish.status_code == 303
        assert AUTH_COOKIE in client.cookies
        assert (await client.get("/api/v2/me")).json()["data"]["state"] == "account"
    assert "provider-secret" not in finish.text
    assert "one-use-code" not in finish.text


@pytest.mark.asyncio
async def test_initial_preferences_have_durable_idempotency(memory_http):
    import httpx
    from starlette.responses import Response

    from avatarweave_agent.memory.identity import CSRF_COOKIE

    client, rt, _ = memory_http
    issued = Response()
    await rt.identity.establish({"iss": "https://issuer.test", "sub": "consent-account"}, issued)
    client.cookies.extract_cookies(
        httpx.Response(
            200, headers=issued.raw_headers, request=httpx.Request("GET", "https://memory.test/")
        )
    )
    client.headers.update(
        {"Origin": rt.settings.public_origin, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
    )
    preference = {
        **default_preferences().model_dump(exclude={"preference_version"}),
        "expected_version": 1,
        "idempotency_key": "consent",
        "long_term_memory_enabled": True,
    }
    first = await client.put("/api/v2/memory-preferences", json=preference)
    assert first.status_code == 200
    assert first.json()["data"]["preference_version"] == 2
    repeated = await client.put("/api/v2/memory-preferences", json=preference)
    assert repeated.json()["data"] == first.json()["data"]
    conflict = await client.put(
        "/api/v2/memory-preferences", json={**preference, "long_term_memory_enabled": False}
    )
    assert conflict.status_code == 409
    assert (await client.get("/api/v2/memory-preferences")).json()["data"] == first.json()["data"]


@pytest.mark.asyncio
async def test_public_demo_token_cannot_join_private_room():
    import httpx

    from avatarweave_agent.api import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/token",
            json={"room_name": "vm-guess"},
            headers={"Authorization": "Bearer test-secret"},
        )
        assert result.status_code == 403
        disabled = await client.get("/api/v2/me")
        assert disabled.status_code == 503
        assert disabled.json()["error"]["code"] == "unsupported_capability"
