"""LiveKit streaming text -> local Kokoro phrase requests -> incremental PCM audio."""

from __future__ import annotations

import re
from uuid import uuid4

import aiohttp
from livekit.agents import APIConnectionError, APIStatusError, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

from .voices import get_voice


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8880/v1",
        voice: str = "zf_xiaoxiao",
        speed: float = 1.0,
        min_phrase_chars: int = 12,
        max_phrase_chars: int = 80,
    ):
        get_voice(voice)
        if (
            not 0.5 <= speed <= 2
            or not 1 <= min_phrase_chars <= max_phrase_chars <= 200
        ):
            raise ValueError("Invalid Kokoro speed or phrase bounds")
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=24000,
            num_channels=1,
        )
        self.base_url, self.voice, self.speed = base_url.rstrip("/"), voice, speed
        self.min_phrase_chars, self.max_phrase_chars = (
            min_phrase_chars,
            max_phrase_chars,
        )
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self):
        return "kokoro-82m"

    @property
    def provider(self):
        return "kokoro-local"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ):
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS):
        return KokoroStream(self, conn_options)

    def session(self):
        if self._session is None or self._session.closed:
            # A local inference endpoint must not be routed through the cloud API proxy.
            self._session = aiohttp.ClientSession(
                trust_env=False, timeout=aiohttp.ClientTimeout(total=120, connect=5)
            )
        return self._session

    async def aclose(self):
        if self._session:
            await self._session.close()


class KokoroStream(tts.SynthesizeStream):
    def __init__(self, engine: TTS, conn_options: APIConnectOptions):
        super().__init__(tts=engine, conn_options=conn_options)
        self.engine = engine
        # Snapshot per synthesis; changing a later session cannot alter an in-flight utterance.
        self.voice, self.speed = engine.voice, engine.speed

    async def _run(self, output_emitter: tts.AudioEmitter):
        request_id = uuid4().hex
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=request_id)

        async def speak(text):
            if not text.strip():
                return
            payload = {
                "model": "kokoro",
                "input": text,
                "voice": self.voice,
                "speed": self.speed,
                "response_format": "pcm",
                "stream": True,
            }
            try:
                async with self.engine.session().post(
                    self.engine.base_url + "/audio/speech", json=payload
                ) as response:
                    if response.status != 200:
                        raise APIStatusError(
                            "Local Kokoro synthesis failed", status_code=response.status
                        )
                    async for chunk in response.content.iter_chunked(4800):
                        output_emitter.push(chunk)
                    output_emitter.flush()
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise APIConnectionError("Cannot reach local Kokoro TTS") from exc

        buffer = ""
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                await speak(buffer)
                buffer = ""
                continue
            buffer += item
            while buffer:
                boundaries = list(
                    re.finditer(r"[。！？!?；;\n]|\.(?=\s|$)|[，,：:]\s*", buffer)
                )
                boundary = next(
                    (
                        m.end()
                        for m in boundaries
                        if m.group()[0] in "。！？!?；;\n."
                        or m.end() >= self.engine.min_phrase_chars
                    ),
                    None,
                )
                if boundary is None and len(buffer) < self.engine.max_phrase_chars:
                    break
                cut = min(
                    boundary or self.engine.max_phrase_chars,
                    self.engine.max_phrase_chars,
                )
                await speak(buffer[:cut])
                buffer = buffer[cut:]
        await speak(buffer)
        output_emitter.end_segment()
