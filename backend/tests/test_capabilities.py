import json
from types import SimpleNamespace

import pytest
import yaml
from avatarweave_agent.api import app
from avatarweave_agent.avatars import AvatarDocument, get_avatar
from avatarweave_agent.config import settings
from avatarweave_agent.pipeline import PipelineConfig, ProviderConfig
from avatarweave_agent.session_options import merge_options, read_options
from avatarweave_agent.voices import Voice, VoiceService, open_preview, voice_service
from fastapi.testclient import TestClient
from livekit import api
from pydantic import ValidationError

HEADERS = {"Authorization": "Bearer test-secret"}


def config(provider, **options):
    return PipelineConfig(
        mode="pipeline",
        profile="test",
        llm=ProviderConfig(provider="openai"),
        stt=ProviderConfig(provider="openai"),
        tts=ProviderConfig(provider=provider, options=options),
    )


def test_two_provider_catalogs_and_voice_options_are_generic(tmp_path, monkeypatch):
    path = tmp_path / "voices.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "providers": {
                    "zhiai": {
                        "voices": [
                            {
                                "id": "new-speaker",
                                "name": "新声音",
                                "language": "fr",
                                "language_label": "法语",
                                "description": "配置的法语声音",
                            }
                        ]
                    }
                },
            }
        )
    )
    monkeypatch.setattr(settings, "voice_config", path)
    catalog = voice_service(config("zhiai", speaker="existing"))
    assert catalog.describe()["capabilities"] == {
        "voice_selection": True,
        "preview": True,
    }
    selected, _ = catalog.select("new-speaker")
    assert selected.options["speaker"] == "new-speaker"
    assert "voice" not in selected.options
    assert catalog.spec.options["speaker"] == "existing"
    other = voice_service(config("openai", voice="coral"))
    assert other.select("coral")[0].options["voice"] == "coral"
    assert "api_key" not in json.dumps(other.describe())
    with pytest.raises(ValueError):
        other.select("new-speaker")


def test_metadata_is_validated_from_both_request_paths(monkeypatch):
    monkeypatch.setattr(settings, "mock_tts_preview", True)
    with TestClient(app) as client:
        response = client.post(
            "/api/token",
            headers=HEADERS,
            json={
                "avatar_id": "kei",
                "tts_voice": "zm_yunxi",
                "room_config": {
                    "agents": [
                        {
                            "agent_name": settings.agent_name,
                            "metadata": json.dumps({"context": {"custom": "keep"}}),
                        }
                    ]
                },
            },
        )
        assert response.status_code == 201
        claims = api.TokenVerifier(
            settings.livekit_api_key, settings.livekit_api_secret
        ).verify(response.json()["participant_token"])
        metadata = json.loads(claims.room_config.agents[0].metadata)
        assert metadata["context"]["custom"] == "keep"
        assert metadata["avatarweave_session"] == {
            "version": 1,
            "avatar_id": "kei",
            "tts_voice": "zm_yunxi",
        }
        assert (
            client.post(
                "/api/token", headers=HEADERS, json={"avatar_id": "missing"}
            ).status_code
            == 422
        )
        for envelope in [
            {"version": 2},
            {"version": 1, "tts_voice": "invalid"},
            {"version": 1, "avatar_id": "invalid"},
        ]:
            assert (
                client.post(
                    "/api/token",
                    headers=HEADERS,
                    json={
                        "room_config": {
                            "agents": [
                                {
                                    "agent_name": settings.agent_name,
                                    "metadata": json.dumps(
                                        {"avatarweave_session": envelope}
                                    ),
                                }
                            ]
                        }
                    },
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/api/token",
                headers=HEADERS,
                json={
                    "tts_voice": "zm_yunxi",
                    "room_config": {
                        "agents": [
                            {
                                "agent_name": settings.agent_name,
                                "metadata": "opaque-user-data",
                            }
                        ]
                    },
                },
            ).status_code
            == 422
        )
        assert client.get("/api/avatars").json()["avatars"][0]["id"] == "kei"
        assert (
            client.post("/api/tts/preview", json={"voice": "zm_yunxi"}).status_code
            == 401
        )


def test_strict_metadata_and_disabled_voice_selection():
    disabled = PipelineConfig(mode="mock")
    assert not voice_service(disabled).describe()["enabled"]
    with pytest.raises(ValueError):
        merge_options("", avatar_id=None, tts_voice="x", config=disabled)
    for metadata in [
        "opaque-metadata",
        "[]",
        '{"custom":"keep"}',
        '{"avatar_session":{}}',
        '{"avatar_tts_voice":"zm_yunxi"}',
    ]:
        with pytest.raises(ValueError):
            read_options(metadata)


@pytest.mark.asyncio
async def test_action_mapping_comes_from_avatar_configuration(tmp_path, monkeypatch):
    document = {
        "version": 1,
        "default_avatar": "custom",
        "avatars": {
            "custom": {
                "name": "自定义",
                "assets": {"core_url": "/core.js", "model_url": "/other.model3.json"},
                "default_action": "wave",
                "actions": {
                    "wave": {
                        "label": "挥手",
                        "type": "motion",
                        "name": "Wave",
                        "index": 7,
                    },
                    "happy": {"label": "高兴", "type": "expression", "name": "Smile"},
                },
            }
        },
    }
    path = tmp_path / "avatars.yaml"
    path.write_text(yaml.safe_dump(document))
    monkeypatch.setattr(settings, "avatar_config", path)
    _, avatar = get_avatar()
    assert avatar.default_action == "wave"
    from avatarweave_agent.worker import publish_action

    received = []

    async def publish(payload, **options):
        received.append((json.loads(payload), options))

    ctx = SimpleNamespace(
        room=SimpleNamespace(local_participant=SimpleNamespace(publish_data=publish))
    )
    await publish_action(ctx, avatar)
    await publish_action(ctx, avatar, "happy")
    assert received[0][0]["name"] == "Wave" and received[0][0]["index"] == 7
    assert received[1][0]["type"] == "expression" and received[1][0]["name"] == "Smile"
    await publish_action(ctx, avatar, "not-configured")
    assert len(received) == 2
    document["avatars"]["custom"]["default_action"] = "missing"
    with pytest.raises(ValidationError):
        AvatarDocument.model_validate(document)


@pytest.mark.asyncio
async def test_preview_releases_resources_and_streams_before_completion(monkeypatch):
    import avatarweave_agent.voices as module

    class Frame:
        sample_rate = 16000
        num_channels = 1
        data = SimpleNamespace(tobytes=lambda: b"\x01\x00" * 160)

    class Stream:
        def __init__(self):
            self.closed = False
            self.sent = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.sent += 1
            if self.sent > 2:
                raise StopAsyncIteration
            return SimpleNamespace(frame=Frame())

        async def aclose(self):
            self.closed = True

    class Engine:
        def __init__(self):
            self.closed = False
            self.stream = Stream()

        def synthesize(self, text):
            return self.stream

        async def aclose(self):
            self.closed = True

    engine = Engine()
    monkeypatch.setattr(module, "build_provider", lambda *args: engine)
    service = VoiceService(
        ProviderConfig(provider="openai", options={"voice": "test"}),
        [Voice(id="test", name="test")],
    )
    preview = await open_preview(service, "test")
    assert engine.stream.sent == 1
    assert preview.headers["X-Audio-Sample-Rate"] == "16000"
    chunks = preview.chunks()
    assert await anext(chunks) == b"\x01\x00" * 160
    await chunks.aclose()
    assert engine.closed and engine.stream.closed


@pytest.mark.asyncio
async def test_preview_closes_even_if_response_headers_cannot_be_sent():
    from avatarweave_agent.api import PreviewResponse

    class Playback:
        headers = {}
        closed = False

        async def chunks(self):
            yield b"\x00\x00"

        async def close(self):
            self.closed = True

    playback = Playback()

    async def send(message):
        raise OSError("client disconnected before body")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(Exception):
        await PreviewResponse(playback)(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
        )
    assert playback.closed


def test_mock_preview_resolves_only_tts_credentials(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "mock",
                "profile": "local",
                "profiles": {
                    "local": {
                        "llm": {
                            "provider": "openai",
                            "options": {"api_key": "${UNUSED_TEST_LLM_SECRET}"},
                        },
                        "stt": {
                            "provider": "openai",
                            "options": {"api_key": "${UNUSED_TEST_ASR_SECRET}"},
                        },
                        "tts": {
                            "provider": "openai",
                            "options": {"voice": "coral", "api_key": "test"},
                        },
                    }
                },
            }
        )
    )
    monkeypatch.setenv("PIPELINE_CONFIG", str(path))
    monkeypatch.setattr(settings, "mock_tts_preview", True)
    catalog = voice_service(PipelineConfig(mode="mock", profile="local"))
    assert catalog.spec.provider == "openai"
    assert catalog.describe()["default_voice"] == "coral"
