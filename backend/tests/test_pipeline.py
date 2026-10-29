import hashlib
import json
from pathlib import Path

import pytest
import yaml
from avatarweave_agent.pipeline import (
    ProviderConfig,
    build_provider,
    create_session,
    load_pipeline,
)


def document(tmp_path, data):
    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_select_only_active_profile_and_no_unused_secrets(tmp_path):
    path = document(
        tmp_path,
        {
            "version": 1,
            "mode": "pipeline",
            "profile": "local",
            "profiles": {
                "local": {
                    kind: {"provider": "openai", "options": {"api_key": "${TEST_KEY}"}}
                    for kind in ("llm", "stt", "tts")
                },
                "unused": {
                    "stt": {
                        "provider": "huoshan",
                        "options": {"app_key": "${UNSET_SECRET}"},
                    }
                },
            },
        },
    )
    config = load_pipeline(path, env={"TEST_KEY": "credential-not-for-logs"})
    assert config.stt.provider == "openai"
    assert "credential-not-for-logs" not in repr(config)
    assert config.summary()["stt"] == "openai"
    with pytest.raises(ValueError, match="UNSET_SECRET"):
        load_pipeline(path, env={"PIPELINE_PROFILE": "unused"})


def test_mock_does_not_resolve_models_and_invalid_provider_fails(tmp_path):
    path = document(tmp_path, {"version": 1, "mode": "mock", "profiles": {}})
    assert load_pipeline(path, env={}).mode == "mock"
    path = document(
        tmp_path,
        {
            "version": 1,
            "mode": "pipeline",
            "profile": "bad",
            "profiles": {
                "bad": {kind: {"provider": "typo"} for kind in ("llm", "stt", "tts")}
            },
        },
    )
    with pytest.raises(ValueError, match="llm.provider"):
        load_pipeline(path, env={})


def test_all_legacy_source_files_are_identical_to_reviewed_originals():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "provider-provenance.json").read_text())
    assert len(manifest) >= 20
    for name, details in manifest.items():
        assert (
            hashlib.sha256((root / "livekit/plugins" / name).read_bytes()).hexdigest()
            == details["sha256"]
        ), name


def test_huoshan_constructor_and_session_compatibility(tmp_path, monkeypatch):
    asr = build_provider(
        "stt",
        ProviderConfig(
            provider="huoshan",
            options={"app_key": "test", "access_key": "test", "language": "zh-CN"},
        ),
    )
    tts = build_provider(
        "tts",
        ProviderConfig(
            provider="huoshan",
            options={"app_id": "test", "access_token": "test", "voice": "S_test"},
        ),
    )
    assert asr.capabilities.streaming and asr.capabilities.interim_results
    assert tts.capabilities.streaming and tts.resource_id == "volc.megatts.default"
    with pytest.raises(ValueError, match="misspelled"):
        build_provider(
            "stt", ProviderConfig(provider="huoshan", options={"misspelled": True})
        )


@pytest.mark.asyncio
async def test_selected_session_uses_streaming_asr_and_local_vad():
    from types import SimpleNamespace

    from avatarweave_agent.pipeline import PipelineConfig
    from livekit.plugins import silero

    config = PipelineConfig.model_validate(
        {
            "mode": "pipeline",
            "profile": "test",
            "llm": {"provider": "openai", "options": {"api_key": "test"}},
            "stt": {
                "provider": "openai",
                "options": {
                    "api_key": "test",
                    "model": "gpt-4o-mini-transcribe",
                    "language": "zh",
                    "use_realtime": True,
                },
            },
            "tts": {
                "provider": "huoshan",
                "options": {"app_id": "test", "access_token": "test"},
            },
        }
    )
    session = create_session(
        SimpleNamespace(proc=SimpleNamespace(userdata={"vad": silero.VAD.load()})),
        config,
    )
    assert session.stt.capabilities.streaming
    assert session.tts.capabilities.streaming
    await session.aclose()
