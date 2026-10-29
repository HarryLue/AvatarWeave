import asyncio
import requests
import numpy as np
import time
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

from dataclasses import dataclass, field


# Define the URL for your TTS service
# 定义你的TTS服务的URL
TTS_URL = "http://182.18.34.71:11992/tts_url/stream_embed"


@dataclass
class TTSOptions:
    """
    Manages all configurable parameters for the TTS service.
    用于管理TTS服务所有可配置参数的数据类。
    """
    speaker: str = "speaker1" # "guanyu"
    role: str = "system"
    speed: float = 1.0
    language: str = "zh"
    # Other parameters can be added here
    # 可以在这里添加其他参数
    max_tokens_per_sentence: int = 120

    def update_options(
        self,
        *,
        speaker: NotGivenOr[str] = NOT_GIVEN,
        role: NotGivenOr[str] = NOT_GIVEN,
        speed: NotGivenOr[float] = NOT_GIVEN,
        language: NotGivenOr[str] = NOT_GIVEN,
        max_tokens_per_sentence: NotGivenOr[int] = NOT_GIVEN
    ) -> None:
        """
        Updates the TTS options. This method allows for partial updates.
        更新TTS选项。此方法支持部分参数更新。
        """
        if speaker is not NOT_GIVEN:
            self.speaker = speaker
        if role is not NOT_GIVEN:
            self.role = role
        if speed is not NOT_GIVEN:
            self.speed = speed
        if language is not NOT_GIVEN:
            self.language = language
        if max_tokens_per_sentence is not NOT_GIVEN:
            self.max_tokens_per_sentence = max_tokens_per_sentence


class TTS(tts.TTS):
    def __init__(self, url=None, **kwargs):
        # Initialize the TTS options with provided arguments or defaults.
        # 使用提供的参数或默认值初始化TTS选项。
        self._opts = TTSOptions(**kwargs)
        self.url = url if url else TTS_URL
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )

    def update_options(self, **kwargs) -> None:
        """
        Dynamically updates the TTS configuration, allowing for runtime changes
        to speaker, speed, etc.
        动态更新TTS配置，允许在运行时更改音色、语速等。
        """
        self._opts.update_options(**kwargs)

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return TTSChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    @property
    def name(self):
        return f"Zhiai TTS-{self._opts.speaker}"


class TTSChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: TTS, input_text: str, conn_options):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._audio_generated = False
        self._loop = asyncio.get_event_loop()

    async def _run(self, output_emitter: tts.AudioEmitter):
        if self._audio_generated:
            return

        # Access the current TTS options from the parent class instance.
        # 从父类实例中访问当前的TTS选项。
        current_opts = self._tts._opts
        data = {
            "text": self.input_text,
            "speaker": current_opts.speaker,
            "max_tokens_per_sentence": current_opts.max_tokens_per_sentence,
            "speed": current_opts.speed,
            "role": current_opts.role,
        }

        try:
            def sync_request():
                return requests.post(TTS_URL, json=data, stream=True)

            response = await self._loop.run_in_executor(None, sync_request)

            if response.status_code != 200:
                raise RuntimeError(f"Zhiai TTS service returned an error: {response.status_code}, {response.text}")

            output_emitter.initialize(
                request_id=str(id(self)),
                sample_rate=self._tts.sample_rate,
                num_channels=1,
                mime_type="audio/pcm",
            )

            chunk_size = 24000
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    output_emitter.push(chunk)

            output_emitter.flush()
            self._audio_generated = True

        except Exception as e:
            raise RuntimeError(f"Custom TTS synthesis failed: {str(e)}")
        finally:
            try:
                output_emitter.end_input()
            except:
                pass
