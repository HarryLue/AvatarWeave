
from __future__ import annotations

import asyncio
from typing import NotGivenOr

from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    stt,
)
from livekit.agents.stt import SpeechEventType, STTCapabilities, SpeechEvent, SpeechData
from livekit.agents.types import NOT_GIVEN, is_given
from livekit.agents.utils import AudioBuffer

# 假设原始同步 ASR 客户端在另一个模块中
# 注意：对于生产用途，考虑使用 aiohttp 等库或将同步调用包装在执行器中，
# 使此客户端变为异步。
from.simple_asr_client import SimpleASRClient

class SimpleASRClientSTT(stt.STT):
    """
    一个用于简单、非流式 ASR 客户端的 LiveKit STT 插件。
    
    此插件将一个同步、基于文件的 ASR 客户端适配到异步、
    基于 AudioBuffer 的 LiveKit 框架。它在返回最终转录之前
    一次性处理整个音频缓冲区。
    """
    def __init__(
        self,
        server_url: str,
        model_name: str,
        language_code: NotGivenOr[str] = NOT_GIVEN,
    ) -> None:
        """
        初始化 SimpleASRClientSTT 插件。

        Args:
            server_url (str): ASR 服务器的 URL。
            model_name (str): 要使用的 ASR 模型名称。
            language_code (NotGivenOr[str]): 可选的 STT 模型语言代码。
        """
        # 底层 ASR 客户端是非流式的，并提供单个最终结果。
        # 因此，我们将 streaming 设置为 False，interim_results 设置为 True。
        super().__init__(capabilities=STTCapabilities(streaming=False, interim_results=True))

        # 初始化底层同步 ASR 客户端
        self._asr_client = SimpleASRClient(server_url=server_url, model_name=model_name)
        self._language_code = language_code if is_given(language_code) else "en-US"

        # 在初始化时尝试连接到 ASR 服务器
        if not self._asr_client.connect():
            raise APIConnectionError("Failed to connect to ASR server during initialization.")

    def _ensure_connected(self):
        # 一个辅助方法，用于在识别前确保连接处于活动状态。
        if not self._asr_client.is_connected():
            if not self._asr_client.connect():
                raise APIConnectionError("Lost connection to ASR server.")
    
    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        """
        非流式语音识别的核心实现。
        
        此方法聚合音频帧，将它们转换为 WAV 字节，
        调用底层 ASR 客户端，并将结果转换为
        LiveKit SpeechEvent。
        """
        # 步骤 1：处理语言代码覆盖（如果提供）
        if is_given(language):
            current_language_code = language
        else:
            current_language_code = self._language_code
            
        # 步骤 2：将分散的音频帧聚合成一个单一、连贯的帧。
        try:
            combined_frame = rtc.combine_audio_frames(buffer)
        except ValueError as e:
            # 这处理了空缓冲区或属性不一致的帧等情况。
            raise APIStatusError(f"Audio frame aggregation failed: {e}") from e

        # 步骤 3：将组合后的音频帧转换为 WAV 格式的字节。
        try:
            wav_bytes = combined_frame.to_wav_bytes()
        except Exception as e:
            raise APIConnectionError(f"Failed to convert audio frames to WAV: {e}") from e

        extracted_text = None
        try:
            # 步骤 4：在线程池内调用同步 ASR 客户端逻辑
            # 以防止阻塞 asyncio 事件循环。
            loop = asyncio.get_event_loop()
            extracted_text = await loop.run_in_executor(
                None,  # 使用默认线程池执行器
                self._asr_client.recognize,
                wav_bytes
            )
        except asyncio.TimeoutError as e:
            # LiveKit 框架本身可能会施加超时，我们可以在此处捕获。
            raise APITimeoutError() from e
        except Exception as e:
            # 用于捕获底层客户端的任何其他错误的通用捕获。
            # 这对于防止代理进程崩溃至关重要。
            raise APIConnectionError(f"ASR recognition failed: {e}") from e

        if not extracted_text:
            # 如果识别返回空结果，则将其视为失败。
            raise APIStatusError("ASR recognition returned no text.")

        # 步骤 5：将原始文本输出转换为 LiveKit SpeechEvent。
        return self._transcription_to_speech_event(text=extracted_text, language=current_language_code)

    def _transcription_to_speech_event(self, text: str, language: str) -> stt.SpeechEvent:
        """
        辅助方法，用于从字符串创建标准化的 SpeechEvent。
        """
        return stt.SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[SpeechData(text=text, language=language)],
        )
