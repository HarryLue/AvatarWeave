"""Protect the existing Volcengine fixes with a real local WebSocket peer (no vendor credentials)."""

import asyncio
import gzip
import json
import struct

import aiohttp
import pytest
from aiohttp import web
from livekit import rtc
from livekit.agents import stt as lk_stt
from livekit.plugins.huoshan import STT
from livekit.plugins.huoshan.stt import ResponseParser


def response(utterances, sequence=1):
    body = gzip.compress(json.dumps({"result": {"utterances": utterances}}).encode())
    return bytes([0x11, 0x91, 0x11, 0]) + struct.pack(">iI", sequence, len(body)) + body


def test_binary_error_response_exposes_business_error_code():
    payload = json.dumps({"message": "Wait for packet timeout"}).encode()
    raw = (
        bytes([0x11, 0xF0, 0x10, 0])
        + struct.pack(">iI", 45000081, len(payload))
        + payload
    )
    parsed = ResponseParser.parse_response(raw)
    assert parsed.code == 45000081
    assert parsed.payload_msg["message"] == "Wait for packet timeout"


@pytest.mark.asyncio
async def test_huoshan_packet_heartbeat_empty_final_dedup_and_cleanup():
    handshake = {}
    packets = []
    got_heartbeat = asyncio.Event()

    async def ws_handler(request):
        assert request.headers["X-Api-App-Key"] == "test-app"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        first = await ws.receive_bytes()
        handshake.update(json.loads(gzip.decompress(first[12:])))
        # Empty results are not transcripts; repeated finalized utterances must be ignored.
        await ws.send_bytes(response([{"text": " ", "definite": True}]))
        utterance = {
            "text": "你好",
            "definite": True,
            "start_time": 0,
            "end_time": 300,
            "words": [],
        }
        await ws.send_bytes(response([utterance], 2))
        await ws.send_bytes(response([utterance], 3))
        async for message in ws:
            if message.type == aiohttp.WSMsgType.BINARY:
                pcm = gzip.decompress(message.data[12:])
                packets.append((struct.unpack(">i", message.data[4:8])[0], pcm))
                if len(pcm) == 6400 and not any(pcm):
                    got_heartbeat.set()
        return ws

    app = web.Application()
    app.router.add_get("/asr", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async with aiohttp.ClientSession() as http:
        asr = STT(
            app_key="test-app",
            access_key="test-access",
            endpoint_url=f"ws://127.0.0.1:{port}/asr",
            http_session=http,
        )
        stream = asr.stream()
        events = []

        async def consume():
            async for event in stream:
                events.append(event)

        consumer = asyncio.create_task(consume())
        try:
            stream.push_frame(
                rtc.AudioFrame(
                    data=bytes([1, 0]) * 3200,
                    sample_rate=16000,
                    num_channels=1,
                    samples_per_channel=3200,
                )
            )
            await asyncio.wait_for(got_heartbeat.wait(), 3)
            assert handshake["request"]["end_window_size"] == 800
            assert handshake["request"]["enable_nonstream"] is False
            final = [
                e for e in events if e.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT
            ]
            assert [e.alternatives[0].text for e in final] == ["你好"]
            assert any(len(pcm) == 6400 and any(pcm) for _, pcm in packets)
            sequences = [seq for seq, _ in packets]
            assert sequences == sorted(set(sequences))
        finally:
            await stream.aclose()
            await consumer
    await runner.cleanup()


@pytest.mark.asyncio
async def test_huoshan_tts_streams_pcm_and_preserves_text_spaces(monkeypatch):
    import importlib

    import websockets

    tts_module = importlib.import_module("livekit.plugins.huoshan.tts")
    from livekit.plugins.huoshan.protocols import EventType, Message

    sent_text = []
    auth = {}

    def event_reply(event, session="", payload=b"{}", audio=False):
        result = bytearray([0x11, 0xB4 if audio else 0x94, 0x10, 0])
        result.extend(struct.pack(">i", event))
        identity = (
            b"connection" if event == EventType.ConnectionStarted else session.encode()
        )
        result.extend(struct.pack(">I", len(identity)))
        result.extend(identity)
        result.extend(struct.pack(">I", len(payload)))
        result.extend(payload)
        return bytes(result)

    async def peer(ws):
        async for raw in ws:
            message = Message.from_bytes(raw)
            if message.event == EventType.StartConnection:
                await ws.send(event_reply(EventType.ConnectionStarted))
            elif message.event == EventType.StartSession:
                await ws.send(event_reply(EventType.SessionStarted, message.session_id))
            elif message.event == EventType.TaskRequest:
                sent_text.append(json.loads(message.payload)["req_params"]["text"])
            elif message.event == EventType.FinishSession:
                pcm = bytes([1, 0]) * 4800
                await ws.send(
                    event_reply(
                        EventType.TTSResponse, message.session_id, pcm, audio=True
                    )
                )
                await ws.send(
                    event_reply(EventType.SessionFinished, message.session_id)
                )
                return

    original_connect = websockets.connect
    async with websockets.serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        def connect(_endpoint, **options):
            auth.update(options["additional_headers"])
            return original_connect(f"ws://127.0.0.1:{port}", **options)

        monkeypatch.setattr(tts_module.websockets, "connect", connect)
        tts = tts_module.TTS(app_id="app", access_token="access", voice="test-voice")
        chunks = []
        async with tts.synthesize("Hello world*") as stream:
            async with asyncio.timeout(5):
                async for event in stream:
                    chunks.append(event.frame)
        assert auth["X-Api-App-Key"] == "app"
        assert "".join(sent_text) == "Hello world"
        # LiveKit may append zero padding to align the final audio frame.
        assert sum(sample != 0 for frame in chunks for sample in frame.data) == 4800
