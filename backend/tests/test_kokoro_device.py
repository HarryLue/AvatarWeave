"""Protect FP32-only selection and explicit CUDA startup semantics."""

import pytest
from avatarweave_agent.config import Settings, settings
from avatarweave_agent.kokoro_service import Engine
from avatarweave_agent.pipeline import ProviderConfig, provider_class
from livekit.plugins.kokoro import TTS
from pydantic import ValidationError


def test_plugin_is_the_single_registered_provider():
    assert provider_class("tts", ProviderConfig(provider="kokoro")) is TTS
    assert TTS.__module__ == "livekit.plugins.kokoro.tts"


def test_quantized_precision_is_rejected():
    with pytest.raises(ValidationError):
        Settings(kokoro_precision="int8", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(kokoro_device="automatic", _env_file=None)


def assets(tmp_path, monkeypatch):
    for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
        (tmp_path / name).touch()
    monkeypatch.setattr(settings, "kokoro_model_dir", tmp_path)
    monkeypatch.setattr(settings, "kokoro_device", "gpu")


def test_gpu_without_cuda_provider_fails_instead_of_using_cpu(tmp_path, monkeypatch):
    import onnxruntime as ort

    assets(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ort, "get_available_providers", lambda: ["CPUExecutionProvider"]
    )
    with pytest.raises(RuntimeError, match="CUDAExecutionProvider unavailable"):
        Engine()


def test_cuda_init_failure_is_detected_and_gpu_id_forwarded(tmp_path, monkeypatch):
    import onnxruntime as ort

    assets(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "kokoro_gpu_id", 2)
    monkeypatch.setattr(
        ort,
        "get_available_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setattr(ort, "preload_dlls", lambda **kwargs: None, raising=False)
    seen = {}

    class Session:
        def __init__(self, path, *, sess_options, providers):
            seen["path"], seen["providers"] = path, providers

        def disable_fallback(self):
            pass

        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setattr(ort, "InferenceSession", Session)
    with pytest.raises(RuntimeError, match="refusing silent CPU fallback"):
        Engine()
    assert seen["path"].endswith("kokoro-v1.0.onnx")
    assert seen["providers"][0] == (
        "CUDAExecutionProvider",
        {"device_id": 2, "use_tf32": 0},
    )
