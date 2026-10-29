import pytest
from avatarweave_agent.api import app
from avatarweave_agent.config import settings
from fastapi.testclient import TestClient
from livekit import api


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_token_standard_contract_and_dispatch(client, monkeypatch):
    monkeypatch.setattr(settings, "rtc_public_url", "")
    response = client.post(
        "/api/token", json={}, headers={"Authorization": "Bearer test-secret"}
    )
    assert response.status_code == 201
    body = response.json()
    claims = api.TokenVerifier(
        settings.livekit_api_key, settings.livekit_api_secret
    ).verify(body["participant_token"])
    assert body["server_url"] == settings.livekit_url
    assert claims.video.room == body["room_name"]
    assert claims.room_config.agents[0].agent_name == "avatar-assistant"


def test_auth_and_dispatch_restrictions(client):
    assert client.post("/api/token", json={}).status_code == 401
    headers = {"Authorization": "Bearer test-secret"}
    assert (
        client.post(
            "/api/token",
            json={"room_config": {"agents": [{"agent_name": "other"}]}},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/token",
            json={"participant_attributes": {"lk.agent.state": "speaking"}},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/token", json={"room_name": "../outside"}, headers=headers
        ).status_code
        == 422
    )
    assert client.get("/api/health").json()["status"] == "ok"


def test_token_uses_public_rtc_url(client, monkeypatch):
    monkeypatch.setattr(settings, "rtc_public_url", "wss://avatar.example/realtime")
    response = client.post(
        "/api/token", json={}, headers={
            "Authorization": "Bearer test-secret",
            "X-AvatarWeave-RTC-URL": "ws://proxy.example/realtime",
        }
    )
    assert response.status_code == 201
    assert response.json()["server_url"] == "wss://avatar.example/realtime"


@pytest.mark.parametrize("url", ["ws://111.56.189.4/realtime", "wss://avatar.example:8443/realtime"])
def test_token_uses_nginx_rtc_url(client, monkeypatch, url):
    monkeypatch.setattr(settings, "rtc_public_url", "")
    response = client.post("/api/token", json={}, headers={
        "Authorization": "Bearer test-secret",
        "X-AvatarWeave-RTC-URL": url,
    })
    assert response.status_code == 201
    assert response.json()["server_url"] == url


@pytest.mark.asyncio
async def test_persist_and_read_history():
    from avatarweave_agent.db import engine, get_messages, init_db, save_message

    await init_db()
    await save_message("test-room", "msg-1", "user", "你好")
    await save_message("test-room", "msg-1", "user", "你好")
    await save_message("test-room", "msg-2", "assistant", "你好，我是数字人")
    rows = await get_messages("test-room")
    assert len(rows) == 2
    assert rows[0]["text"] == "你好"
    assert rows[1]["role"] == "assistant"
    await engine.dispose()
