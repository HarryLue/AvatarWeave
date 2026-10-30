import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from avatarweave_agent import practice as service
from avatarweave_agent.api import app
from avatarweave_agent.config import settings
from avatarweave_agent.db import Practice, Session, save_message
from livekit import api as livekit_api


@pytest.fixture
def client(monkeypatch):
    # Exercise the same quota boundaries without depending on deployment allowances.
    monkeypatch.setattr(settings, "practice_daily_limit", 5)
    monkeypatch.setattr(settings, "practice_daily_attempt_limit", 25)
    service.signup_windows.clear()
    with TestClient(app) as client:
        yield client


def visitor(client):
    response = client.post("/api/practice/guest")
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["token"]}


def start(client, headers):
    response = client.post("/api/practice/sessions", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_guest_auth_isolation_and_server_owned_dispatch(client):
    assert client.get("/api/practice/sessions").status_code == 401
    alice, bob = visitor(client), visitor(client)
    item = start(client, alice)
    claims = livekit_api.TokenVerifier(
        settings.livekit_api_key, settings.livekit_api_secret
    ).verify(item["credentials"]["participant_token"])
    metadata = json.loads(claims.room_config.agents[0].metadata)
    assert metadata["context"] == {"practice_id": item["id"]}
    assert claims.video.room.startswith("practice-")
    assert "test-secret" not in json.dumps(item)
    for suffix, method in [
        ("", "get"),
        ("/finish", "post"),
        ("/report", "post"),
        ("/help", "post"),
    ]:
        response = getattr(client, method)(
            f"/api/practice/sessions/{item['id']}{suffix}",
            headers=bob,
            **({"json": {"kind": "hint"}} if suffix == "/help" else {}),
        )
        assert response.status_code == 404
    assert client.get("/api/practice/sessions", headers=bob).json() == []
    assert client.get("/api/practice/sessions", headers=alice).json()[0]["id"] == item["id"]


def test_active_session_daily_quota_and_idempotent_report(client):
    headers = visitor(client)
    item = start(client, headers)
    assert client.post("/api/practice/sessions", headers=headers).status_code == 409
    path = f"/api/practice/sessions/{item['id']}"
    assert client.post(path + "/report", headers=headers).status_code == 409
    for _ in range(2):
        assert client.post(path + "/finish", headers=headers).json()["status"] == "ended"
    report = client.post(path + "/report", headers=headers)
    assert report.status_code == 200
    assert report.json()["user_turns"] == 0
    assert report.json()["corrections"] == []
    assert client.post(path + "/report", headers=headers).json() == report.json()
    assert client.get("/api/practice/quota", headers=headers).json()["remaining"] == 5
    for _ in range(5):
        item = start(client, headers)
        client.portal.call(
            save_message,
            item["credentials"]["room_name"],
            str(uuid4()),
            "user",
            "Hello, I would like to check in.",
        )
        client.portal.call(
            save_message,
            item["credentials"]["room_name"],
            str(uuid4()),
            "assistant",
            "What is your reservation name?",
        )
        client.post(f"/api/practice/sessions/{item['id']}/finish", headers=headers)
    assert client.post("/api/practice/sessions", headers=headers).status_code == 429
    assert client.get("/api/practice/quota", headers=headers).json()["remaining"] == 0


def test_failed_connections_and_opening_greeting_do_not_charge_quota(client, monkeypatch):
    monkeypatch.setattr(settings, "practice_daily_attempt_limit", 6)
    headers = visitor(client)
    for _ in range(6):
        item = start(client, headers)
        room = item["credentials"]["room_name"]
        client.portal.call(save_message, room, str(uuid4()), "assistant", "Welcome to the hotel!")
        client.portal.call(save_message, room, str(uuid4()), "user", "Can you hear me?")
        client.post(f"/api/practice/sessions/{item['id']}/finish", headers=headers)
    quota = client.get("/api/practice/quota", headers=headers).json()
    assert quota["used"] == 0
    assert quota["remaining"] == 5
    assert quota["attempts_remaining"] == 0
    assert client.post("/api/practice/sessions", headers=headers).status_code == 429


@pytest.mark.parametrize("exists,status", [(False, 201), (True, 409), (None, 503)])
def test_restart_orphan_reconciled_against_actual_room(client, monkeypatch, exists, status):
    headers = visitor(client)
    previous = start(client, headers)
    client.portal.call(service.join_practice, previous["id"], previous["credentials"]["room_name"])

    async def lookup(name):
        assert name == previous["credentials"]["room_name"]
        return exists

    monkeypatch.setattr(service, "room_exists", lookup)
    response = client.post("/api/practice/sessions", headers=headers)
    assert response.status_code == status
    rows = client.get("/api/practice/sessions", headers=headers).json()
    old = next(item for item in rows if item["id"] == previous["id"])
    assert old["status"] == ("ended" if exists is False else "active")
    assert len(rows) == (2 if exists is False else 1)


@pytest.mark.parametrize("exists,status", [(False, "ended"), (None, "ending")])
def test_pending_connection_grace_and_orphan_finish(client, monkeypatch, exists, status):
    headers = visitor(client)
    item = start(client, headers)

    async def absent(name):
        return exists

    monkeypatch.setattr(service, "room_exists", absent)
    assert client.post("/api/practice/sessions", headers=headers).status_code == 409
    client.portal.call(service.join_practice, item["id"], item["credentials"]["room_name"])
    response = client.post(f"/api/practice/sessions/{item['id']}/finish", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == status


def test_help_limits_and_expired_guest(client):
    headers = visitor(client)
    item = start(client, headers)
    path = f"/api/practice/sessions/{item['id']}/help"
    assert client.post(path, headers=headers, json={"kind": "arbitrary"}).status_code == 422
    for _ in range(12):
        result = client.post(path, headers=headers, json={"kind": "hint"})
        assert result.status_code == 200
        assert result.json()["message"] == service.HELP["hint"]
    assert client.post(path, headers=headers, json={"kind": "hint"}).status_code == 409


def test_goal_order_quote_provenance_and_worker_finish(client):
    headers = visitor(client)
    item = start(client, headers)
    room = item["credentials"]["room_name"]

    async def exercise():
        await service.join_practice(item["id"], room)
        with pytest.raises(ValueError):
            await service.join_practice(item["id"], room)
        await save_message(room, str(uuid4()), "assistant", "I'm staying for two nights.")
        await save_message(room, str(uuid4()), "user", service.HELP["hint"])
        assert "依次" in await service.complete_goal(
            item["id"], "stay", "I'm staying for two nights."
        )
        assert "证据" in await service.complete_goal(
            item["id"], "reservation", service.HELP["hint"]
        )
        assert "证据" in await service.complete_goal(
            item["id"], "reservation", "Not spoken by user"
        )
        texts = [
            "I'd like to check in. My reservation is under Alex Chen.",
            "I'm staying for two nights.",
            "What time is breakfast?",
        ]
        for goal, text in zip(service.GOALS, texts):
            await save_message(room, str(uuid4()), "user", text)
            assert "目标已完成" in await service.complete_goal(item["id"], goal["id"], text)
        await service.end_practice(item["id"])

    client.portal.call(exercise)
    response = client.post(f"/api/practice/sessions/{item['id']}/report", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["user_turns"] == 3
    assert len(response.json()["goals"]) == 3
    assert response.json()["mode"] == "mock"


def test_report_failure_is_retryable_without_losing_messages(client, monkeypatch):
    headers = visitor(client)
    item = start(client, headers)
    path = f"/api/practice/sessions/{item['id']}"
    client.portal.call(
        save_message, item["credentials"]["room_name"], str(uuid4()), "user", "I want check in."
    )
    client.post(path + "/finish", headers=headers)

    async def broken(messages):
        raise ValueError("provider unavailable")

    monkeypatch.setattr(service, "generate_review", broken)
    assert client.post(path + "/report", headers=headers).status_code == 503
    assert client.get(path, headers=headers).json()["status"] == "report_failed"
    assert len(client.get(path, headers=headers).json()["messages"]) == 1

    async def recovered(messages):
        return service.Review(summary="继续练习", corrections=[], next_focus="礼貌表达")

    monkeypatch.setattr(service, "generate_review", recovered)
    assert client.post(path + "/report", headers=headers).status_code == 200


def test_review_rejects_invented_evidence(client, monkeypatch):
    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self.chunks()

        async def chunks(self):
            from livekit.agents.llm import ChatChunk, ChoiceDelta

            yield ChatChunk(
                id="test",
                delta=ChoiceDelta(
                    content=json.dumps(
                        {
                            "summary": "test",
                            "next_focus": "test",
                            "corrections": [
                                {
                                    "original": "something never said",
                                    "revised": "better",
                                    "reason": "test",
                                }
                            ],
                        }
                    )
                ),
            )

    class Model:
        closed = False

        def chat(self, **kwargs):
            return Stream()

        async def aclose(self):
            self.closed = True

    model = Model()
    monkeypatch.setattr(
        service, "load_pipeline", lambda: type("Config", (), {"mode": "pipeline", "llm": None})()
    )
    monkeypatch.setattr(service, "build_provider", lambda *args: model)

    async def exercise():
        with pytest.raises(ValueError, match="not spoken"):
            await service.generate_review([{"role": "user", "text": "Hello there"}])
        assert model.closed

    client.portal.call(exercise)


def test_deadline_watchdog_flushes_before_end(client):
    from avatarweave_agent.worker import watch_practice

    headers = visitor(client)
    item = start(client, headers)

    async def exercise():
        async with Session.begin() as db:
            await db.execute(
                update(Practice)
                .where(Practice.id == item["id"])
                .values(expires_at=service.now() - timedelta(seconds=1))
            )
        order = []
        disconnected = asyncio.Event()

        class Room:
            async def disconnect(self):
                async with Session() as db:
                    row = await db.get(Practice, item["id"])
                assert row.status == "ended"
                order.append("disconnect")
                disconnected.set()

        class Context:
            room = Room()

            def __init__(self):
                self.callbacks = []

            def add_shutdown_callback(self, callback):
                self.callbacks.append(callback)

        async def flush():
            order.append("flush")

        context = Context()
        watch_practice(context, item["id"], flush)
        await asyncio.wait_for(disconnected.wait(), timeout=3)
        assert order == ["flush", "disconnect"]
        await context.callbacks[0]()

    client.portal.call(exercise)
