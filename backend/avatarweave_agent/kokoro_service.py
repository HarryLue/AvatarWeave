"""Local FP32 Kokoro CPU/CUDA service. Phrase inference + streamed 24 kHz PCM, no cloud inference."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from livekit.plugins.kokoro.voices import VOICES, get_voice
from pydantic import BaseModel, ConfigDict, Field

from .config import settings

logger = logging.getLogger("uvicorn.error.kokoro")


def split_phrases(text: str, max_chars: int = 80):
    # Chinese comma/semicolon are useful inference boundaries for low first-audio latency.
    pieces = re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]*", text)
    for piece in pieces:
        while len(piece) > max_chars:
            cut = max(
                piece.rfind("，", 0, max_chars),
                piece.rfind(", ", 0, max_chars),
                piece.rfind(" ", 0, max_chars),
            )
            cut = cut + 1 if cut > max_chars // 3 else max_chars
            yield piece[:cut]
            piece = piece[cut:]
        if piece.strip():
            yield piece


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "kokoro"
    input: str = Field(min_length=1, max_length=5000)
    voice: str = "zf_xiaoxiao"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    response_format: str = "pcm"
    stream: bool = True


class Engine:
    def __init__(self):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro
        from misaki.zh import ZHG2P

        model_dir = settings.kokoro_model_dir
        model_name = "kokoro-v1.0.onnx"
        if (
            not (model_dir / model_name).exists()
            or not (model_dir / "voices-v1.0.bin").exists()
        ):
            raise RuntimeError(
                "Kokoro assets missing. Run: bash scripts/deploy.sh install"
            )
        options = ort.SessionOptions()
        options.intra_op_num_threads = settings.kokoro_threads
        options.inter_op_num_threads = 1
        self.device = settings.kokoro_device
        providers = ["CPUExecutionProvider"]
        if self.device == "gpu":
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise RuntimeError(
                    "CUDAExecutionProvider unavailable; run bash scripts/deploy.sh install with KOKORO_DEVICE=gpu"
                )
            if settings.kokoro_cuda_lib_dir:
                # CUDA is resolved by the system linker; load the existing cuDNN
                # directory without installing NVIDIA Python wheel packages.
                ort.preload_dlls(
                    cuda=False, cudnn=True, directory=settings.kokoro_cuda_lib_dir
                )
            else:
                ort.preload_dlls()
            providers = [
                (
                    "CUDAExecutionProvider",
                    {"device_id": settings.kokoro_gpu_id, "use_tf32": 0},
                ),
                "CPUExecutionProvider",
            ]
        session = ort.InferenceSession(
            str(model_dir / model_name),
            sess_options=options,
            providers=providers,
        )
        session.disable_fallback()
        self.providers = session.get_providers()
        if self.device == "gpu" and "CUDAExecutionProvider" not in self.providers:
            raise RuntimeError(
                "CUDA initialization failed; refusing silent CPU fallback. See Kokoro log and NVIDIA driver/runtime setup."
            )
        self.model_name = model_name
        logger.info(
            "Kokoro model=%s precision=fp32 device=%s gpu_id=%s providers=%s",
            model_name,
            self.device,
            settings.kokoro_gpu_id if self.device == "gpu" else None,
            self.providers,
        )
        self.model = Kokoro.from_session(session, str(model_dir / "voices-v1.0.bin"))
        self.zh = ZHG2P()
        missing = set(VOICES) - set(self.model.get_voices())
        if missing:
            raise RuntimeError(f"Kokoro voice pack missing voices: {sorted(missing)}")

    def synthesize(self, text: str, voice: str, speed: float):
        metadata = get_voice(voice)
        if metadata["language"] == "zh":
            text, _ = self.zh(text)
            samples, rate = self.model.create(
                text, voice=voice, speed=speed, is_phonemes=True
            )
        else:
            samples, rate = self.model.create(
                text, voice=voice, speed=speed, lang=metadata["language"]
            )
        import numpy as np

        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        if rate != 24000:
            raise RuntimeError("Unexpected Kokoro sample rate")
        return pcm


@asynccontextmanager
async def lifespan(app):
    app.state.engine = await asyncio.to_thread(Engine)
    app.state.inference_lock = asyncio.Lock()
    # Pre-warm Chinese phonemization and ONNX before the supervisor declares readiness.
    await asyncio.to_thread(app.state.engine.synthesize, "你好。", "zf_xiaoxiao", 1.0)
    yield


app = FastAPI(title="Local Kokoro TTS", lifespan=lifespan)


@app.get("/health")
async def health(request: Request):
    return {
        "status": "ok",
        "model": "kokoro-82m",
        "device": request.app.state.engine.device,
        "precision": "fp32",
        "model_file": request.app.state.engine.model_name,
        "gpu_id": settings.kokoro_gpu_id
        if request.app.state.engine.device == "gpu"
        else None,
        "providers": request.app.state.engine.providers,
        "sample_rate": 24000,
    }


@app.get("/v1/audio/voices")
async def voices():
    return {"voices": list(VOICES.values())}


@app.post("/v1/audio/speech")
async def speech(body: SpeechRequest, request: Request):
    if body.model != "kokoro" or body.response_format != "pcm" or not body.stream:
        raise HTTPException(422, "Use model=kokoro, response_format=pcm, stream=true")
    try:
        get_voice(body.voice)
    except ValueError as exc:
        raise HTTPException(422, "Unsupported Kokoro voice") from exc
    if not body.input.strip():
        raise HTTPException(422, "Text is empty")

    async def generate():
        start = time.perf_counter()
        first = True
        for phrase in split_phrases(body.input):
            if await request.is_disconnected():
                return
            async with request.app.state.inference_lock:
                inference = asyncio.create_task(
                    asyncio.to_thread(
                        request.app.state.engine.synthesize,
                        phrase,
                        body.voice,
                        body.speed,
                    )
                )
                try:
                    pcm = await asyncio.shield(inference)
                except asyncio.CancelledError:
                    # A cancelled client cannot stop an ONNX thread. Keep the lock until
                    # it finishes so the shared phonemizer never runs concurrently.
                    await inference
                    raise
            if await request.is_disconnected():
                return
            if first:
                logger.info(
                    "kokoro_first_audio_ms=%.1f voice=%s",
                    (time.perf_counter() - start) * 1000,
                    body.voice,
                )
                first = False
            # Chunked transfer starts after the first phrase, not after all phrases.
            for offset in range(0, len(pcm), 4800):
                yield pcm[offset : offset + 4800]
                await asyncio.sleep(0)

    return StreamingResponse(
        generate(),
        media_type="audio/pcm",
        headers={
            "X-Audio-Sample-Rate": "24000",
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-store",
        },
    )
