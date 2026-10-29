"""Verify streamed audio precedes end-of-text and per-room voice stays isolated."""

import asyncio
import json
import struct

import pytest
from aiohttp import web
from livekit import api
from livekit.plugins.kokoro import TTS
from livekit.plugins.kokoro.voices import VOICES


@pytest.mark.asyncio
async def test_audio_arrives_before_llm_finishes_and_voice_is_preserved():
    received = []

    async def speech(request):
        received.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "audio/pcm"})
        await response.prepare(request)
        await response.write(struct.pack("<h", 5000) * 2400)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/audio/speech", speech)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    engine = TTS(
        base_url=f"http://127.0.0.1:{port}/v1", voice="zm_yunxi", min_phrase_chars=4
    )
    stream = engine.stream()
    try:
        stream.push_text("你好，欢迎来到这里。")
        # Input is deliberately kept open. Whole-response buffering would time out.
        first = await asyncio.wait_for(anext(stream), 5)
        assert first.frame.sample_rate == 24000
        assert max(first.frame.data) == 5000
        stream.push_text("我们继续测试第二句话。")
        stream.end_input()
        events = [first] + [event async for event in stream]
        # LiveKit may append a final 10 ms silent frame when flushing.
        assert (
            sum(sample != 0 for event in events for sample in event.frame.data) == 4800
        )
        assert 4800 <= sum(e.frame.samples_per_channel for e in events) <= 5280
        assert [r["input"] for r in received] == [
            "你好，欢迎来到这里。",
            "我们继续测试第二句话。",
        ]
        assert all(r["voice"] == "zm_yunxi" and r["stream"] for r in received)
    finally:
        await stream.aclose()
        await engine.aclose()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_interruption_cancels_inflight_http_request():
    entered, disconnected = asyncio.Event(), asyncio.Event()

    async def speech(request):
        response = web.StreamResponse()
        await response.prepare(request)
        entered.set()
        try:
            while True:
                await response.write(struct.pack("<h", 5000) * 2400)
                await asyncio.sleep(0.03)
        except (ConnectionResetError, asyncio.CancelledError):
            disconnected.set()
        return response

    app = web.Application()
    app.router.add_post("/audio/speech", speech)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    engine = TTS(base_url=f"http://127.0.0.1:{port}", min_phrase_chars=1)
    stream = engine.stream()
    try:
        stream.push_text("中断测试。")
        await asyncio.wait_for(entered.wait(), 3)
        await stream.aclose()
        await asyncio.wait_for(disconnected.wait(), 3)
    finally:
        await stream.aclose()
        await engine.aclose()
        await runner.cleanup()


def test_catalog_has_distinct_chinese_and_english_voices():
    assert len(VOICES) == 36
    assert sum(v["language"] == "zh" for v in VOICES.values()) == 8
    assert all(v["description"] and v["preview_text"] for v in VOICES.values())
    with pytest.raises(ValueError):
        TTS(voice="../arbitrary-model")


def test_selected_voice_is_signed_in_dispatch_metadata(monkeypatch):
    # Imports here share the API test module's isolated database/auth fixture.
    from avatarweave_agent.api import app
    from avatarweave_agent.config import settings
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "mock_tts_preview", True)
    with TestClient(app) as client:
        headers = (
            {"Authorization": "Bearer " + settings.api_access_key}
            if settings.api_access_key
            else {}
        )
        response = client.post(
            "/api/token", json={"tts_voice": "zm_yunxi"}, headers=headers
        )
        assert response.status_code == 201
        claims = api.TokenVerifier(
            settings.livekit_api_key, settings.livekit_api_secret
        ).verify(response.json()["participant_token"])
        assert (
            json.loads(claims.room_config.agents[0].metadata)["avatarweave_session"][
                "tts_voice"
            ]
            == "zm_yunxi"
        )
        assert (
            client.post(
                "/api/token", json={"tts_voice": "unknown"}, headers=headers
            ).status_code
            == 422
        )
        assert client.get("/api/tts/voices").json()["voices"][0]["description"]
