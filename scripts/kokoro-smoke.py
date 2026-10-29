"""Exercise the actual FP32 inference runtime without cloud model calls."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from avatarweave_agent.config import settings
from avatarweave_agent.kokoro_service import Engine

engine = Engine()
assert engine.device == settings.kokoro_device
assert engine.model_name == "kokoro-v1.0.onnx"
start = time.perf_counter()
pcm = engine.synthesize("你好，这是本地模型的真实合成验证。", "zf_xiaoxiao", 1.0)
assert len(pcm) > 4800 and len(pcm) % 2 == 0
print(
    json.dumps(
        {
            "device": engine.device,
            "providers": engine.providers,
            "precision": "fp32",
            "audio_seconds": len(pcm) / 48000,
            "synthesis_ms": round((time.perf_counter() - start) * 1000),
        }
    )
)
