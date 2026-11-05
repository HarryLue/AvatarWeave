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
        meta=meta(service, lane="dialogue", budget_ms=1000),
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
    service.clock = lambda: now + timedelta(seconds=37)
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
    from avatarweave_agent.memory.management_http import router as management_router

    app.include_router(management_router)
    from avatarweave_agent.memory.portability_http import router as portability_router

    app.include_router(portability_router)
    app.add_exception_handler(MemoryFault, fault_handler)
    app.state.memory_runtime = MemoryRuntime(settings, foundation.database, foundation, identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as client:
        yield client, app.state.memory_runtime, app


@pytest.mark.asyncio
async def test_login_redirects_to_https_origin_before_creating_secure_state(memory_http):
    client, rt, _ = memory_http
    for origin in ("http://memory.test", "https://wrong-host.test"):
        result = await client.get(origin + "/api/v2/auth/login")
        assert result.status_code == 303
        assert result.headers["location"] == rt.settings.public_origin + "/api/v2/auth/login"
        assert "set-cookie" not in result.headers
        assert result.headers["cache-control"] == "no-store"


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
    resumed = await client.post(
        f"/api/v2/sessions/{view['session_id']}/resume",
        json={
            "last_event_sequence": 0,
            "capabilities": capabilities().model_dump(),
            "idempotency_key": "resume-http",
        },
    )
    assert resumed.status_code == 200 and resumed.json()["data"]["session_id"] == view["session_id"]
    missing_sequence = await client.post(
        f"/api/v2/sessions/{view['session_id']}/resume",
        json={"capabilities": capabilities().model_dump(), "idempotency_key": "bad-resume-http"},
    )
    assert missing_sequence.status_code == 422
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


def candidate_for(write, *, predicate="current_city", value="北京", start=None, candidate_id=None):
    from avatarweave_agent.memory.contracts import (
        CandidateMemory,
        EvidenceRef,
        Statement,
        TimeWindow,
    )

    event = write.event
    return CandidateMemory(
        candidate_id=candidate_id or uuid4().hex,
        namespace="life",
        content_kind="fact",
        assertion_kind="user_report",
        interpretation="asserted",
        sensitivity="normal",
        proposed_importance=0.6,
        display_text=value,
        statement=Statement(
            subject_id=write.scope.user_id,
            predicate=predicate,
            value_type="text",
            canonical_value=value,
            polarity="positive",
            valid_time=TimeWindow(start=start, precision="day" if start else "unknown"),
        ),
        sources=[
            EvidenceRef(
                event_id=event.event_id,
                source_revision=event.revision,
                start_char=0,
                end_char=len(event.content),
                quote=event.content,
                origin=event.origin,
            )
        ],
    )


async def plan_commit(service, write, candidate, operation_id=None):
    from avatarweave_agent.memory.contracts import CommitInput, ConflictInput
    from avatarweave_agent.memory.lifecycle import rule_for

    planned = await service.conflict(
        ConflictInput(
            meta=meta(service, budget_ms=1000),
            scope=write.scope,
            fence=write.fence,
            candidate=candidate,
            predicate_rule=rule_for(candidate),
            comparable_records=[],
        )
    )
    assert planned.status == "ok", planned
    read = await service.repository(
        RepositoryInput(
            meta=meta(service, budget_ms=1000),
            scope=write.scope,
            fence=write.fence,
            operation="scan_fact_key",
            refs=[],
            subject_id=candidate.statement.subject_id,
            predicate=candidate.statement.predicate,
        )
    )
    request_meta = meta(service, budget_ms=1000)
    if operation_id:
        request_meta = request_meta.model_copy(update={"operation_id": operation_id})
    request = CommitInput(
        meta=request_meta,
        scope=write.scope,
        fence=write.fence,
        candidate=candidate,
        proposals=planned.data.proposals,
        expected_refs=[r.ref for r in read.data.records],
    )
    return await service.commit(request), request


@pytest.mark.asyncio
async def test_fact_commit_evidence_capsule_and_idempotence(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我现在住在北京"})}
    )
    await service.input_event(write)
    result, request = await plan_commit(service, write, candidate_for(write))
    assert result.status == "ok", result
    again = await service.commit(request)
    assert again.data.committed == result.data.committed
    read = await service.repository(
        RepositoryInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            operation="read_versions",
            refs=result.data.committed,
        )
    )
    assert read.data.records[0].statement.canonical_value == "北京"
    assert read.data.records[0].sources[0].level == "evidence_capsule"
    from avatarweave_agent.memory.tables import Capsule, FactVersion

    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == 1
        assert await db.scalar(select(func.count()).select_from(Capsule)) == 1


@pytest.mark.asyncio
async def test_life_changes_preserve_a_b_a_bitemporal_stages(foundation):
    from avatarweave_agent.memory.tables import FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    first_time = datetime(2020, 1, 1, tzinfo=UTC)
    for index, city in enumerate(["北京", "上海", "北京"]):
        source = write.event.model_copy(
            update={
                "event_id": uuid4().hex,
                "turn_id": uuid4().hex,
                "content": f"我从{2020 + index}年起搬到{city}",
            }
        )
        current = write.model_copy(update={"event": source})
        assert (await service.input_event(current)).status == "ok"
        result, _ = await plan_commit(
            service,
            current,
            candidate_for(current, value=city, start=first_time.replace(year=2020 + index)),
        )
        assert result.status == "ok", result
    async with service.database.sessions() as db:
        rows = list(
            await db.scalars(
                select(FactVersion)
                .where(FactVersion.system_to.is_(None))
                .order_by(FactVersion.valid_start)
            )
        )
        assert len(rows) == 3
        assert [utc_time.valid_start.year for utc_time in rows] == [2020, 2021, 2022]
        assert rows[0].valid_end.year == 2021 and rows[1].valid_end.year == 2022
        assert rows[2].valid_end is None


@pytest.mark.asyncio
async def test_forged_quote_stale_commit_and_ambiguous_conflict_rejected(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京，也提到上海"})}
    )
    await service.input_event(write)
    candidate = candidate_for(write)
    bad = candidate.model_copy(
        update={
            "sources": [
                candidate.sources[0].model_copy(update={"quote": "x" * len(write.event.content)})
            ]
        }
    )
    from avatarweave_agent.memory.contracts import ConflictInput
    from avatarweave_agent.memory.lifecycle import rule_for

    rejected = await service.conflict(
        ConflictInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            candidate=bad,
            predicate_rule=rule_for(bad),
            comparable_records=[],
        )
    )
    assert rejected.error.code == "evidence_missing"
    first, request = await plan_commit(service, write, candidate)
    assert first.status == "ok"
    stale = request.model_copy(update={"meta": meta(service)})
    assert (await service.commit(stale)).error.code == "version_conflict"
    other, _ = await plan_commit(service, write, candidate_for(write, value="上海"))
    assert other.error.code == "manual_review_required"


@pytest.mark.asyncio
async def test_source_revision_immediately_invalidates_its_fact(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京，也提到上海"})}
    )
    await service.input_event(write)
    result, _ = await plan_commit(service, write, candidate_for(write))
    revised = write.model_copy(
        update={
            "expected_turn_revision": 1,
            "event": write.event.model_copy(
                update={
                    "revision": 2,
                    "content": "转写有误",
                    "supersedes": VersionRef(object_id=write.event.event_id, version=1),
                }
            ),
        }
    )
    assert (await service.input_event(revised)).status == "ok"
    read = await service.repository(
        RepositoryInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            operation="read_versions",
            refs=result.data.committed,
        )
    )
    assert read.data.records[0].status == "uncertain"


@pytest.mark.asyncio
async def test_delete_revokes_versions_capsules_and_old_jobs(foundation):
    from avatarweave_agent.memory.contracts import DeleteMemoryBody, PrivacyInput
    from avatarweave_agent.memory.http import fence_for
    from avatarweave_agent.memory.tables import Capsule, Tombstone

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    committed, commit_request = await plan_commit(service, write, candidate_for(write))
    ref = committed.data.committed[-1]
    body = DeleteMemoryBody(expected_version=ref.version, confirmed=True, idempotency_key="delete")
    event, _ = await service.control_event(
        write.scope,
        key="delete",
        operation="delete_memory",
        body=body,
        text="delete_memory:" + ref.object_id,
    )
    async with service.database.sessions() as db:
        current = await db.get(Account, write.scope.user_id)
    request = PrivacyInput(
        meta=meta(service, budget_ms=1000),
        scope=write.scope,
        fence=fence_for(current),
        operation="delete_memory",
        preferences=None,
        target_refs=[ref],
        target_source_ids=[],
        target_time=None,
        expected_preference_version=1,
        confirmed_control_event_id=event.event_id,
        deletion_job_id=None,
    )
    deleted = await service.privacy(request)
    assert deleted.status == "ok", deleted
    assert deleted.data.state == "revoked_cleaning"
    assert (await service.commit(commit_request)).error.code == "privacy_changed"
    with pytest.raises(MemoryFault):
        await service.public_memory(write.scope, ref.object_id)
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Capsule)) == 0
        assert await db.get(Tombstone, (write.scope.user_id, ref.object_id, "memory"))
        assert (await db.get(SourceRevision, (write.event.event_id, 1))).source_json is None
        assert (
            (await db.get(Outbox, committed.data.outbox_ids[0])).state == "obsolete"
            if committed.data.outbox_ids
            else True
        )


@pytest.mark.asyncio
async def test_literal_correction_retracts_wrong_knowledge_instead_of_a_life_stage(foundation):
    from avatarweave_agent.memory.contracts import CorrectMemoryBody
    from avatarweave_agent.memory.tables import FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在上海"})}
    )
    await service.input_event(write)
    committed, _ = await plan_commit(service, write, candidate_for(write, value="上海"))
    ref = committed.data.committed[-1]
    updated, pending = await service.correct_memory(
        write.scope,
        ref.object_id,
        CorrectMemoryBody(expected_version=1, correction_text="北京", idempotency_key="correct"),
    )
    assert pending is False and updated.version == 2 and updated.text == "北京"
    repeat, pending = await service.correct_memory(
        write.scope,
        ref.object_id,
        CorrectMemoryBody(expected_version=1, correction_text="北京", idempotency_key="correct"),
    )
    assert repeat.version == 2
    async with service.database.sessions() as db:
        current = list(await db.scalars(select(FactVersion).where(FactVersion.system_to.is_(None))))
        assert len(current) == 1 and current[0].version == 2


@pytest.mark.asyncio
async def test_natural_language_correction_suppresses_old_fact_immediately(foundation):
    from avatarweave_agent.memory.contracts import CorrectMemoryBody

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在上海"})}
    )
    await service.input_event(write)
    committed, _ = await plan_commit(service, write, candidate_for(write, value="上海"))
    ref = committed.data.committed[-1]
    view, pending = await service.correct_memory(
        write.scope,
        ref.object_id,
        CorrectMemoryBody(
            expected_version=1,
            correction_text="我从没住过上海，之前是转写错误",
            idempotency_key="clarify",
        ),
    )
    assert pending is True and view.state == "uncertain"


@pytest.mark.asyncio
async def test_durable_job_extracts_then_marks_structured_not_indexed(foundation):
    from avatarweave_agent.memory.contracts import AdmissionOutput

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)

    class Extractor:
        async def extract(self, request):
            return AdmissionOutput(
                candidates=[candidate_for(write)], decisions=[], input_watermark=0
            )

    assert await service.next_job("worker", Extractor()) is True
    async with service.database.sessions() as db:
        source = await db.get(SourceRevision, (write.event.event_id, 1))
        assert source.state == "structured"
        assert source.structured_settled and not source.index_settled
        from avatarweave_agent.memory.tables import Job

        assert (await db.scalar(select(Job))).state == "done"
    assert await service.next_job("worker", Extractor()) is False


def tiny_profile():
    from avatarweave_agent.memory.contracts import EmbeddingProfile

    return EmbeddingProfile(
        profile_id="test.v1",
        model_id="test-model",
        revision="r1",
        dimensions=3,
        metric="cosine",
        normalized=True,
    )


@pytest.mark.asyncio
async def test_real_vector_projection_scoped_recall_and_generation_fence(foundation):
    import httpx

    from avatarweave_agent.memory.contracts import Budget, EmbeddingInput, QuerySpec, RetrievalInput
    from avatarweave_agent.memory.embedding import EmbeddingGateway, render_record

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    committed, _ = await plan_commit(service, write, candidate_for(write))
    read = await service.repository(
        RepositoryInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            operation="read_versions",
            refs=committed.data.committed,
        )
    )
    record = read.data.records[0]

    def embed_response(request):
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}],
                "usage": {"total_tokens": 3},
            },
        )

    gateway = EmbeddingGateway(
        base_url="https://embedding.test/v1",
        api_key="test",
        profile=tiny_profile(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(embed_response)),
    )
    try:
        indexed = await service.embedding(
            EmbeddingInput(
                meta=meta(service, lane="maintenance", budget_ms=1000),
                scope=write.scope,
                fence=write.fence,
                profile=tiny_profile(),
                purpose="index",
                items=[(record.ref, render_record(record))],
            ),
            gateway,
        )
        assert indexed.status == "ok", indexed
        request = RetrievalInput(
            meta=meta(service, budget_ms=1000),
            scope=write.scope,
            fence=write.fence,
            queries=[
                QuerySpec(
                    query_id="q", text="北京", route="semantic", entity_ids=[], purpose="answer"
                )
            ],
            profile=tiny_profile(),
            budget=Budget(),
            include_historical=False,
        )
        recalled = await service.retrieve(request, gateway)
        assert recalled.status == "ok", recalled
        assert recalled.data.selected_refs == [record.ref]
        other = await account(service)
        denied = await service.retrieve(request.model_copy(update={"scope": other}), gateway)
        assert denied.data.candidates == []
        generation, fence = await service.begin_generation(
            write.scope, write.session_id, write.fence.owner_epoch
        )
        assert await service.generation_valid(write.scope, write.session_id, generation, fence)
        await service.cancel_generation(write.scope, write.session_id, generation)
        assert not await service.generation_valid(write.scope, write.session_id, generation, fence)
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_private_conversation_context_works_for_text_and_forgets_prior_fence(foundation):
    from avatarweave_agent.memory.contracts import PersonaCard
    from avatarweave_agent.memory.conversation import PrivateConversation

    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    conversation = await PrivateConversation.bind(
        service,
        view.session_id,
        view.room_name,
        "worker",
        persona=PersonaCard(
            persona_id="companion",
            version=1,
            authored_identity="友好的中文助手",
            behavior_boundaries=["不编造"],
            preferred_style=[],
            lore_refs=[],
            approved_at=service.clock(),
        ),
    )
    prepared = await conversation.prepare("turn-a", "你好")
    assert prepared.blocks[-1].text == "你好"
    old_generation, old_fence = conversation.generation_id, conversation.fence
    await conversation.prepare("turn-b", "第二句")
    with pytest.raises(MemoryFault):
        await conversation.check(old_generation, old_fence)
    assert [e.content for e in await conversation.source_overlay()] == ["你好"]


@pytest.mark.asyncio
async def test_output_publication_is_not_completion_and_late_receipts_do_not_replay(foundation):
    from avatarweave_agent.memory.contracts import OutputInput, OutputObservation, OutputPlan

    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    generation, fence = await service.begin_generation(scope, view.session_id, owner)
    plan = OutputPlan(
        output_id="output",
        session_id=view.session_id,
        turn_id="turn",
        generation_id=generation,
        speech_id=None,
        spoken_text=None,
        action_ids=[],
        sequence=0,
        expires_at=service.clock() + timedelta(seconds=30),
    )
    request = OutputInput(
        meta=meta(service),
        scope=scope,
        fence=fence,
        operation="plan",
        plan=plan,
        capabilities=capabilities(),
        observations=[],
    )
    assert (await service.output(request)).data.state == "planned"
    assert (
        await service.output(request.model_copy(update={"operation": "publish"}))
    ).data.state == "published"
    observation = OutputObservation(
        observation_id="receipt",
        output_id="output",
        generation_id=generation,
        observed_at=service.clock(),
        received_at=service.clock(),
        client_sequence=1,
        state="completed",
        source="client",
        segment_id=None,
        position_ms=None,
        media_clock_id=None,
    )
    result = await service.output(
        request.model_copy(update={"operation": "observe", "observations": [observation]})
    )
    assert result.error.code == "unsupported_capability"
    await service.cancel_generation(scope, view.session_id, generation)
    late = await service.output(
        request.model_copy(update={"operation": "observe", "observations": [observation]})
    )
    assert late.error.code == "stale_generation"


@pytest.mark.asyncio
async def test_archive_roundtrip_corruption_and_online_copy_retained(foundation, tmp_path):
    from cryptography.fernet import Fernet

    from avatarweave_agent.memory.archive import EncryptedObjectStore, FileKeyProvider
    from avatarweave_agent.memory.contracts import ArchiveInput

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    committed, _ = await plan_commit(service, write, candidate_for(write))
    keyroot = tmp_path / "keys"
    keyroot.mkdir(mode=0o700)
    key = keyroot / "v1.key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    store = EncryptedObjectStore(tmp_path / "objects", FileKeyProvider(keyroot))
    try:
        request = ArchiveInput(
            meta=meta(service, lane="maintenance", budget_ms=2000),
            scope=write.scope,
            fence=write.fence,
            operation="write",
            record_refs=committed.data.committed,
            archive=None,
        )
        written = await service.archive(request, store)
        assert written.status == "ok", written
        assert written.data.verified and not written.data.may_remove_online_copy
        verified = await service.archive(
            request.model_copy(
                update={
                    "meta": meta(service, lane="maintenance", budget_ms=2000),
                    "operation": "verify",
                    "archive": written.data.manifest,
                }
            ),
            store,
        )
        assert verified.status == "ok", verified
        reading = request.model_copy(
            update={
                "meta": meta(service, lane="deep_recall", budget_ms=2000),
                "operation": "read",
                "archive": verified.data.manifest,
            }
        )
        restored = await service.archive(reading, store)
        assert restored.status == "ok" and restored.data.restored_records[0].display_text == "北京"
        path = store._path(verified.data.manifest.object_ref)
        path.write_bytes(b"corrupt encrypted archive")
        corrupted = await service.archive(
            reading.model_copy(update={"meta": meta(service, budget_ms=1000)}), store
        )
        assert corrupted.error.code == "storage_unavailable"
        assert (
            await service.public_memory(write.scope, committed.data.committed[-1].object_id)
        ).text == "北京"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_object_io_retains_a_bounded_slot_until_really_finished(
    foundation, tmp_path
):
    import time

    from avatarweave_agent.memory.archive import EncryptedObjectStore, FileKeyProvider

    service = foundation
    write = await event_request(service, await account(service))
    store = EncryptedObjectStore(tmp_path, FileKeyProvider(tmp_path), max_running=1)
    request = write.model_copy(update={"meta": meta(service, budget_ms=10)})
    service.executor.work_providers.append(store.remaining)
    result = await service.executor.run(
        request, lambda: store._io(request.meta.operation_id, time.sleep, 0.1)
    )
    assert result.execution.cleanup == "detached_bounded"
    assert len(result.execution.remaining_work_handles) == 1
    assert result.error.code == "deadline_exceeded"
    assert len(store.tasks) == 1
    with pytest.raises(MemoryFault) as busy:
        await store._io("another", lambda: None)
    assert busy.value.info.code == "overloaded"
    await store.close()
    assert not store.tasks


@pytest_asyncio.fixture
async def encrypted_store(tmp_path):
    from cryptography.fernet import Fernet

    from avatarweave_agent.memory.archive import EncryptedObjectStore, FileKeyProvider

    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    key = keys / "v1.key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    store = EncryptedObjectStore(tmp_path / "objects", FileKeyProvider(keys))
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_repeated_evidence_does_not_create_confirmation_or_capsule(foundation):
    from avatarweave_agent.memory.tables import Capsule, FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    first, _ = await plan_commit(service, write, candidate_for(write))
    again, _ = await plan_commit(service, write, candidate_for(write))
    assert again.data.committed == first.data.committed
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == 1
        assert await db.scalar(select(func.count()).select_from(Capsule)) == 1


@pytest.mark.asyncio
async def test_retry_reuses_extraction_and_commit_when_embedding_temporarily_fails(foundation):
    from avatarweave_agent.memory.contracts import AdmissionOutput, EmbeddedItem, EmbeddingOutput
    from avatarweave_agent.memory.tables import Capsule, FactVersion, Job

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)

    class Extractor:
        calls = 0

        async def extract(self, request):
            self.calls += 1
            return AdmissionOutput(
                candidates=[candidate_for(write)], decisions=[], input_watermark=0
            )

    class Embedding:
        profile = tiny_profile()
        calls = 0

        async def embed(self, request):
            self.calls += 1
            if self.calls == 1:
                raise MemoryFault("provider_unavailable", "Temporarily unavailable", retryable=True)
            return EmbeddingOutput(
                profile=self.profile,
                items=[EmbeddedItem(ref=ref, values=[1.0, 0.0, 0.0]) for ref, _ in request.items],
                input_tokens=1,
                index_generation="index:r1",
            )

    extractor, embedding = Extractor(), Embedding()
    await service.next_job("worker", extractor, embedding)
    async with service.database.sessions() as db:
        assert (await db.scalar(select(Job))).state == "retry_wait"
    now = service.clock()
    service.clock = lambda: now + timedelta(seconds=37)
    await service.next_job("worker", extractor, embedding)
    assert extractor.calls == 1 and embedding.calls == 2
    async with service.database.sessions() as db:
        assert (await db.scalar(select(Job))).state == "done"
        assert (await db.get(SourceRevision, (write.event.event_id, 1))).state == "indexed"
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == 1
        assert await db.scalar(select(func.count()).select_from(Capsule)) == 1


@pytest.mark.asyncio
async def test_typed_summary_job_persists_result_and_is_idempotent(foundation):
    from sqlalchemy import update

    from avatarweave_agent.memory.contracts import CompressionInput, CompressionPolicy, TimeWindow
    from avatarweave_agent.memory.facts import load_record
    from avatarweave_agent.memory.tables import DerivedView, Job

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write))
    from avatarweave_agent.memory.tables import FactVersion

    async with service.database.sessions.begin() as db:
        record = load_record(await db.scalar(select(FactVersion)))
        await db.execute(update(Outbox).values(state="done"))
    payload = CompressionInput(
        meta=meta(service, lane="maintenance", budget_ms=2000),
        scope=write.scope,
        fence=write.fence,
        source_records=[record],
        previous_summaries=[],
        level="session",
        period=TimeWindow(precision="unknown"),
        mandatory_refs=[record.ref],
        policy=CompressionPolicy(verifier_version="extractive.v1"),
    )
    spec = await service.enqueue_maintenance("summarize", payload)
    assert await service.enqueue_maintenance("summarize", payload) == spec
    assert await service.next_job("worker", None)
    async with service.database.sessions() as db:
        assert (await db.get(Job, spec.job_id)).state == "done"
        assert (await db.get(DerivedView, (spec.job_id + ":result", 1))).document_json
        summary = await db.scalar(select(DerivedView).where(DerivedView.kind == "summary"))
        assert summary.state == "valid"
    assert await service.enqueue_maintenance("summarize", payload) == spec


@pytest.mark.asyncio
async def test_object_revocation_prevents_delayed_writer_resurrection(encrypted_store):
    store = encrypted_store
    await store.revoke("delete", "a" * 32)
    with pytest.raises(ValueError):
        await store.write("late-writer", "a" * 32, b"private bytes")
    assert not store._path("a" * 32).exists()
    assert not store.tasks


@pytest.mark.asyncio
async def test_pin_and_open_loop_changes_require_owned_current_versions(foundation):
    from avatarweave_agent.memory.contracts import PageRequest, UpdateOpenLoopBody
    from avatarweave_agent.memory.organizer import PinMemoryBody

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我计划下周去北京"})}
    )
    await service.input_event(write)
    candidate = candidate_for(write, predicate="plan").model_copy(
        update={"content_kind": "future_intent"}
    )
    result, _ = await plan_commit(service, write, candidate)
    ref = result.data.committed[-1]
    pinned = await service.pin_memory(
        write.scope,
        ref.object_id,
        PinMemoryBody(expected_version=1, pinned=True, idempotency_key="pin"),
    )
    assert pinned.pinned and pinned.version == 2
    loops = await service.list_open_loops(write.scope, PageRequest())
    assert len(loops.items) == 1 and loops.items[0].status == "planned"
    loop = loops.items[0]
    body = UpdateOpenLoopBody(
        expected_version=loop.version,
        status="completed",
        event_time=None,
        user_allows_followup=False,
        idempotency_key="complete",
    )
    updated = await service.update_open_loop(write.scope, loop.loop_id, body)
    assert updated.status == "completed"
    assert (await service.update_open_loop(write.scope, loop.loop_id, body)) == updated
    other = await account(service, "other-loop-user")
    with pytest.raises(MemoryFault):
        await service.update_open_loop(
            other, loop.loop_id, body.model_copy(update={"idempotency_key": "forged"})
        )


@pytest.mark.asyncio
async def test_voice_followup_requires_both_permissions_cools_down_and_cancels(foundation):
    from avatarweave_agent.memory.contracts import (
        PageRequest,
        PersonaCard,
        PrivacyPreference,
        TimeWindow,
        UpdateOpenLoopBody,
    )
    from avatarweave_agent.memory.conversation import PrivateConversation

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我计划参加面试"})}
    )
    await service.input_event(write)
    candidate = candidate_for(write, predicate="plan", value="参加面试").model_copy(
        update={"content_kind": "future_intent"}
    )
    committed, _ = await plan_commit(service, write, candidate)
    assert committed.status == "ok"
    view, _ = await session(service, write.scope)
    private = await PrivateConversation.bind(
        service,
        view.session_id,
        view.room_name,
        "followup-test",
        persona=PersonaCard(
            persona_id="companion",
            version=1,
            authored_identity="友好助手",
            behavior_boundaries=["不推断事项结果"],
            preferred_style=[],
            lore_refs=[],
            approved_at=service.clock(),
        ),
    )
    first = await private.prepare("disabled", "你好")
    assert not any(block.kind == "open_loops" for block in first.blocks)
    async with service.database.sessions.begin() as db:
        row = await db.get(Account, write.scope.user_id)
        prefs = PrivacyPreference.model_validate_json(row.preferences_json)
        row.preferences_json = prefs.model_copy(
            update={"allow_proactive_followup": True}
        ).model_dump_json()
    second = await private.prepare("global-only", "今天有空聊聊")
    assert not any(block.kind == "open_loops" for block in second.blocks)
    loop = (await service.list_open_loops(write.scope, PageRequest())).items[0]
    opted_in = await service.update_open_loop(
        write.scope,
        loop.loop_id,
        UpdateOpenLoopBody(
            expected_version=loop.version,
            status="planned",
            event_time=TimeWindow(start=service.clock() - timedelta(days=2), precision="day"),
            user_allows_followup=True,
            idempotency_key="allow-loop",
        ),
    )
    prepared = await private.prepare("permitted", "今天有空聊聊")
    blocks = [block for block in prepared.blocks if block.kind == "open_loops"]
    assert len(blocks) == 1 and "参加面试" in blocks[0].text
    assert all(not block.trusted_instruction for block in blocks)
    assert any(
        entry.ref.object_id == loop.loop_id and entry.decision == "clarify_only"
        for entry in prepared.manifest
    )
    assert await service.generation_valid(
        write.scope, private.session.id, private.generation_id, private.fence
    )
    assert not await service.reserve_followup(
        write.scope, private.fence, "parallel-session-generation"
    )
    await service.update_open_loop(
        write.scope,
        loop.loop_id,
        UpdateOpenLoopBody(
            expected_version=opted_in.version,
            status="cancelled",
            event_time=None,
            user_allows_followup=True,
            idempotency_key="cancel-loop",
        ),
    )
    assert not await service.generation_valid(
        write.scope, private.session.id, private.generation_id, private.fence
    )
    following = await private.prepare("cancelled", "继续聊")
    assert not any(block.kind == "open_loops" for block in following.blocks)


@pytest.mark.asyncio
async def test_ui_transcript_revision_after_session_end_invalidates_old_fact(foundation):
    from avatarweave_agent.memory.contracts import CorrectTranscriptBody
    from avatarweave_agent.memory.storage import MemorySession
    from avatarweave_agent.memory.tables import FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={
            "event": write.event.model_copy(
                update={"content": "我住在上海", "origin": "user_transcript"}
            )
        }
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write, value="上海"))
    async with service.database.sessions.begin() as db:
        session = await db.get(MemorySession, write.session_id)
        session.status = "ended"
    body = CorrectTranscriptBody(
        source_event_id=write.event.event_id,
        expected_source_revision=1,
        corrected_text="我住在北京",
        idempotency_key="transcript",
    )
    result = await service.correct_transcript(write.scope, body)
    assert result.source_revision == 2 and result.structured_state == "pending"
    assert await service.correct_transcript(write.scope, body) == result
    async with service.database.sessions() as db:
        assert (await db.get(EventHead, write.event.event_id)).revision == 2
        assert (await db.scalar(select(FactVersion))).status == "uncertain"
        assert (await db.get(SourceRevision, (write.event.event_id, 1))).source_json is None


@pytest.mark.asyncio
async def test_portable_export_dry_run_import_and_current_scope(foundation, encrypted_store):
    from avatarweave_agent.memory.contracts import CreateExportBody, ExportInput

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write))
    body = CreateExportBody(
        include_source_capsules=True,
        include_raw_transcript=True,
        confirmed=True,
        idempotency_key="export",
    )
    event, _ = await service.control_event(
        write.scope, key="export", operation="export_control", body=body, text="export"
    )
    request = ExportInput(
        meta=meta(service, lane="deep_recall", budget_ms=5000),
        scope=write.scope,
        fence=write.fence,
        operation="create",
        export_id=None,
        selected_namespaces=["life"],
        include_source_capsules=True,
        include_raw_transcript=True,
        explicit_user_confirmation_event=event.event_id,
    )
    result = await service.export(request, encrypted_store)
    assert result.status == "ok", result
    assert result.data.record_count == 1 and result.data.state == "ready"
    again = await service.export(request, encrypted_store)
    assert again.data.export_id == result.data.export_id
    preflight = request.model_copy(
        update={
            "meta": meta(service, lane="deep_recall", budget_ms=5000),
            "operation": "import_dry_run",
            "export_id": result.data.export_id,
        }
    )
    checked = await service.export(preflight, encrypted_store)
    assert checked.data.state == "verified", checked
    applied = await service.export(
        preflight.model_copy(
            update={
                "meta": meta(service, lane="deep_recall", budget_ms=5000),
                "operation": "import_apply",
            }
        ),
        encrypted_store,
    )
    assert applied.data.state == "imported", applied


@pytest.mark.asyncio
async def test_stale_maintenance_lease_cannot_commit(foundation):
    from avatarweave_agent.memory.job_context import JOB_LEASE

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    _, request = await plan_commit(service, write, candidate_for(write))
    token = JOB_LEASE.set(("missing-job", "old-token", 1))
    try:
        result = await service.commit(request)
        assert result.error.code == "version_conflict"
    finally:
        JOB_LEASE.reset(token)


@pytest.mark.asyncio
async def test_signed_http_export_upload_preflight_rejects_tampering(memory_http, encrypted_store):
    import httpx
    from starlette.responses import Response

    from avatarweave_agent.memory.identity import CSRF_COOKIE

    client, rt, _ = memory_http
    rt.store = encrypted_store
    scope = await account(rt.service, "portable-browser")
    write = await event_request(rt.service, scope)
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await rt.service.input_event(write)
    await plan_commit(rt.service, write, candidate_for(write))
    issued = Response()
    await rt.identity.establish({"iss": "https://issuer.test", "sub": "portable-browser"}, issued)
    for raw in issued.raw_headers:
        if raw[0] == b"set-cookie":
            client.cookies.extract_cookies(
                httpx.Response(
                    200, headers=[raw], request=httpx.Request("GET", "https://memory.test/")
                )
            )
    client.headers.update(
        {"Origin": rt.settings.public_origin, "X-CSRF-Token": client.cookies[CSRF_COOKIE]}
    )
    created = await client.post(
        "/api/v2/exports",
        json={
            "include_source_capsules": True,
            "include_raw_transcript": False,
            "confirmed": True,
            "idempotency_key": "http-export",
        },
    )
    assert created.status_code == 200, created.text
    export_id = created.json()["data"]["export_id"]
    downloaded = await client.get(f"/api/v2/exports/{export_id}/download")
    assert downloaded.status_code == 200 and downloaded.headers["cache-control"] == "no-store"
    package = downloaded.json()
    uploaded = await client.post("/api/v2/imports/upload", json=package)
    assert uploaded.status_code == 201, uploaded.text
    handle = uploaded.json()["data"]["export_id"]
    checked = await client.post(
        "/api/v2/imports/dry-run",
        json={
            "uploaded_package_handle": handle,
            "idempotency_key": "preflight",
            "dry_run": True,
            "confirmed": False,
        },
    )
    assert checked.json()["data"]["state"] == "verified", checked.text
    refused = await client.post(
        "/api/v2/imports",
        json={
            "uploaded_package_handle": handle,
            "idempotency_key": "apply",
            "dry_run": False,
            "confirmed": False,
        },
    )
    assert refused.status_code == 409
    package["package"]["bundle"]["records"][0]["display_text"] = "篡改的内容"
    forged = await client.post("/api/v2/imports/upload", json=package)
    assert forged.status_code == 422
    assert not rt.transfers


@pytest.mark.asyncio
async def test_independent_deletion_journal_detects_old_restore_and_tampering(
    foundation, encrypted_store, tmp_path
):
    import sqlite3

    from sqlalchemy import delete

    from avatarweave_agent.memory.deletion_journal import FileDeletionJournal, sync_journal
    from avatarweave_agent.memory.tables import Tombstone

    service = foundation
    scope = await account(service)
    async with service.database.sessions.begin() as db:
        db.add(
            Tombstone(
                account_id=scope.user_id,
                object_id="deleted-fact",
                kind="memory",
                privacy_epoch=1,
                created_at=service.clock(),
            )
        )
    journal = FileDeletionJournal(tmp_path / "independent" / "deletions.sqlite", encrypted_store)
    assert await sync_journal(service, journal) == 1
    assert await sync_journal(service, journal) == 0
    assert await journal.verify_reconciled(service.database, scope, 1, 128) == 1
    async with service.database.sessions.begin() as db:
        await db.execute(delete(Tombstone).where(Tombstone.account_id == scope.user_id))
    with pytest.raises(MemoryFault, match="privacy_changed"):
        await journal.verify_reconciled(service.database, scope, 1, 128)
    db = sqlite3.connect(journal.path)
    db.execute("UPDATE marks SET signature='invalid'")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="integrity"):
        await journal.read(scope)


@pytest.mark.asyncio
async def test_account_deletion_revokes_sessions_and_physically_sweeps_in_batches(foundation):
    from avatarweave_agent.memory.contracts import CreateExportBody, PrivacyInput
    from avatarweave_agent.memory.http import fence_for
    from avatarweave_agent.memory.storage import MemorySession
    from avatarweave_agent.memory.tables import AccountBarrier, FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write))
    body = CreateExportBody(
        include_source_capsules=False,
        include_raw_transcript=False,
        confirmed=True,
        idempotency_key="delete-all",
    )
    event, _ = await service.control_event(
        write.scope, key="delete-all", operation="delete_account", body=body, text="delete_account"
    )
    async with service.database.sessions() as db:
        current = await db.get(Account, write.scope.user_id)
    result = await service.privacy(
        PrivacyInput(
            meta=meta(service, budget_ms=1000),
            scope=write.scope,
            fence=fence_for(current),
            operation="delete_account",
            preferences=None,
            target_refs=[],
            target_source_ids=[],
            target_time=None,
            expected_preference_version=1,
            confirmed_control_event_id=event.event_id,
            deletion_job_id=None,
        )
    )
    assert result.data.state == "revoked_cleaning"
    async with service.database.sessions() as db:
        assert (await db.get(MemorySession, write.session_id)).status == "ended"
    for _ in range(20):
        await service.process_deletions(batch_size=1, backup_inventory_verified=True)
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == 0
        assert (await db.get(AccountBarrier, write.scope.user_id)).state == "deleted"
        assert (await db.get(Account, write.scope.user_id)).subject.startswith("deleted:")


def test_unicode_alignment_uses_exact_unique_source_quotes_only():
    import json

    from avatarweave_agent.memory.gateway import align_evidence

    scope = Scope(workspace_id="default", user_id="u", companion_id="companion")
    now = datetime.now(UTC)
    event = SourceEvent(
        event_id="e",
        revision=1,
        session_id="s",
        turn_id="t",
        scope=scope,
        origin="user_text",
        content="你好，我喜欢🍵花茶。",
        occurred_at=now,
        received_at=now,
        is_final=True,
    )
    raw = {
        "candidates": [
            {
                "sources": [
                    {
                        "event_id": "e",
                        "source_revision": 1,
                        "quote": "🍵花茶",
                        "start_char": 0,
                        "end_char": 3,
                    }
                ]
            }
        ]
    }
    aligned = json.loads(align_evidence(json.dumps(raw), [event]))["candidates"][0]["sources"][0]
    assert event.content[aligned["start_char"] : aligned["end_char"]] == "我喜欢🍵花茶"
    assert aligned["quote"] == "我喜欢🍵花茶"
    raw["candidates"][0]["sources"][0]["event_id"] = "foreign"
    assert json.loads(align_evidence(json.dumps(raw), [event])) == raw


@pytest.mark.asyncio
async def test_extraction_preserves_clause_polarity_and_ignores_model_display_claims(foundation):
    from avatarweave_agent.memory.contracts import ConflictInput
    from avatarweave_agent.memory.lifecycle import rule_for

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我不喜欢咖啡"})}
    )
    assert (await service.input_event(write)).status == "ok"
    candidate = candidate_for(write, predicate="preference", value="咖啡")
    clipped = candidate.model_copy(
        update={
            "sources": [
                candidate.sources[0].model_copy(update={"start_char": 2, "quote": "喜欢咖啡"})
            ]
        }
    )
    for invalid in [clipped, candidate]:
        rejected = await service.conflict(
            ConflictInput(
                meta=meta(service),
                scope=write.scope,
                fence=write.fence,
                candidate=invalid,
                predicate_rule=rule_for(invalid),
                comparable_records=[],
            )
        )
        assert rejected.status == "error" and rejected.error.code == "manual_review_required"
    candidate = candidate.model_copy(
        update={
            "statement": candidate.statement.model_copy(update={"polarity": "negative"}),
            "interpretation": "negated",
            "display_text": "你喜欢咖啡，而且你已经结婚",
        }
    )
    committed, _ = await plan_commit(service, write, candidate)
    assert committed.status == "ok", committed
    read = await service.repository(
        RepositoryInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            operation="read_versions",
            refs=committed.data.committed,
        )
    )
    record = read.data.records[0]
    assert "明确否定" in record.display_text and "结婚" not in record.display_text


@pytest.mark.parametrize("years_ago", range(1, 101))
def test_relative_year_replay_anchors_to_event_clock(years_ago):
    from avatarweave_agent.memory.lifecycle import resolve_time

    now = datetime(2026, 11, 4, tzinfo=UTC)
    window, confidence, clarification = resolve_time(f"{years_ago}年前", now, "Asia/Shanghai")
    assert (
        window.start.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).year
        == 2026 - years_ago
    )
    assert confidence == "derived" and clarification is None


@pytest.mark.asyncio
async def test_evaluation_keeps_failed_and_timed_out_cases_in_denominator():
    from avatarweave_agent.memory.contracts import EvaluationCase, EvaluationInput
    from avatarweave_agent.memory.evaluation import ObservedCase, evaluate

    now = datetime.now(UTC)

    class Clock:
        clock = staticmethod(lambda: now)

    cases = [
        EvaluationCase(
            case_id=f"case-{i}",
            scenario="fact",
            simulated_now=now,
            event_refs=[],
            expected_refs=[],
            forbidden_refs=[],
            expected_behavior="independently checked answer",
            requires_human_review=False,
        )
        for i in range(3)
    ]
    request = EvaluationInput(
        meta=meta(Clock(), budget_ms=100),
        run_id="evaluation",
        dataset_version="dev.v1",
        implementation_version="v1",
        model_profile_version="v1",
        cases=cases,
        clock_mode="simulated",
    )

    class Runner:
        async def run(self, case):
            if case.case_id == "case-0":
                return ObservedCase(
                    retrieved_refs=[],
                    unsupported_claim_count=0,
                    tokens_used=0,
                    answer_result="answered",
                    adjudication_ref=None,
                )
            if case.case_id == "case-1":
                raise ValueError("failure")
            await asyncio.sleep(0.2)

    result = await evaluate(request, Runner())
    assert len(result.data.results) == 3
    assert result.data.human_review_pending == 1
    assert [row.answer_result for row in result.data.results] == [
        "answered",
        "system_failed",
        "timed_out",
    ]
    assert all(
        not row.passed and row.metric_counts[0].denominator == 1 for row in result.data.results
    )


@pytest.mark.asyncio
async def test_delayed_old_tts_task_cannot_adopt_a_new_speech_generation(foundation, monkeypatch):
    from types import SimpleNamespace

    from livekit.agents import Agent
    from livekit.agents.voice.agent_activity import _SpeechHandleContextVar

    from avatarweave_agent.memory.voice import MemoryAgentMixin

    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    old_generation, old_fence = await service.begin_generation(scope, view.session_id, owner)
    new_generation, new_fence = await service.begin_generation(scope, view.session_id, owner)

    async def check(generation, fence):
        if not await service.generation_valid(scope, view.session_id, generation, fence):
            raise MemoryFault("stale_generation", "Old generation")

    provider_calls = []

    async def provider(*args):
        provider_calls.append(True)
        yield object()

    monkeypatch.setattr(Agent.default, "tts_node", provider)

    class Subject(MemoryAgentMixin):
        async def memory_notice(self, *args, **kwargs):
            pass

    subject = Subject()
    subject.private = SimpleNamespace(
        service=service,
        scope=scope,
        session=SimpleNamespace(id=view.session_id),
        generation_id=new_generation,
        fence=new_fence,
        started=0,
        last_persisted=True,
        check=check,
    )
    subject._memory_speeches = {"old-speech": (old_generation, old_fence, 0, None)}
    token = _SpeechHandleContextVar.set(SimpleNamespace(id="old-speech", interrupted=False))
    try:
        frames = [frame async for frame in subject.tts_node(None, None)]
    finally:
        _SpeechHandleContextVar.reset(token)
    assert frames == [] and provider_calls == []
    assert await service.generation_valid(scope, view.session_id, new_generation, new_fence)


@pytest.mark.asyncio
async def test_resume_cancels_old_generation_without_displacing_owner_and_is_idempotent(foundation):
    service = foundation
    scope = await account(service)
    view, original = await session(service, scope)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    old, fence = await service.begin_generation(scope, view.session_id, owner)
    resume = original.model_copy(
        update={
            "meta": meta(service),
            "operation": "resume",
            "session_id": view.session_id,
            "idempotency_key": "resume",
        }
    )
    result = await service.session(resume)
    assert result.status == "ok" and result.data.owner_epoch == owner
    assert not await service.generation_valid(scope, view.session_id, old, fence)
    fresh, fresh_fence = await service.begin_generation(scope, view.session_id, owner)
    assert (await service.session(resume)).status == "ok"
    assert await service.generation_valid(scope, view.session_id, fresh, fresh_fence)


@pytest.mark.asyncio
async def test_private_bootstrap_echoes_nonce_and_seals_reconnected_generation(foundation):
    import json
    from types import SimpleNamespace

    from avatarweave_agent.memory.action_protocol import BOOTSTRAP_TOPIC, ClientHello
    from avatarweave_agent.memory.actions import PrivateActions
    from avatarweave_agent.memory.storage import MemorySession

    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    generation, fence = await service.begin_generation(scope, view.session_id, owner)
    async with service.database.sessions() as db:
        saved_session = await db.get(MemorySession, view.session_id)

    async def cancel():
        await service.cancel_generation(scope, view.session_id, generation)

    conversation = SimpleNamespace(
        service=service,
        scope=scope,
        session=saved_session,
        owner_epoch=owner,
        generation_id=generation,
        cancel=cancel,
    )
    packets = []

    class Room:
        async def publish_data(self, data, **kwargs):
            packets.append((kwargs["topic"], json.loads(data)))

        def on(self, *args):
            pass

        def off(self, *args):
            pass

    room = Room()
    room.local_participant = room
    bridge = PrivateActions(SimpleNamespace(room=room), conversation, None)
    bridge.sequence = 7
    try:
        await bridge.bootstrap(
            ClientHello(version=1, session_id=view.session_id, nonce="first-connection")
        )
        assert await service.generation_valid(scope, view.session_id, generation, fence)
        await bridge.bootstrap(
            ClientHello(version=1, session_id=view.session_id, nonce="second-connection")
        )
        assert not await service.generation_valid(scope, view.session_id, generation, fence)
        bootstrap = [body for topic, body in packets if topic == BOOTSTRAP_TOPIC][-1]
        assert bootstrap["nonce"] == "second-connection" and bootstrap["sequence_floor"] == 7
        assert bootstrap["owner_epoch"] == owner
        bridge._received(
            SimpleNamespace(
                topic="avatarweave.hello.v1",
                participant=SimpleNamespace(identity="foreign"),
                data=b"{}",
            )
        )
        assert bridge.queue.empty()
    finally:
        await bridge.close()
