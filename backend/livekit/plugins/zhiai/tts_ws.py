import asyncio
import json
import websockets
from dataclasses import dataclass, field
from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

# 默认 WebSocket 地址 (基于你的示例代码)
# 注意：如果是在容器或远程运行，请将 0.0.0.0 替换为实际 IP，例如 182.18.34.71
WS_URL = "ws://182.18.34.71:11992/ws/tts_stream"

@dataclass
class TTSOptions:
    """
    Manages all configurable parameters for the TTS service.
    用于管理TTS服务所有可配置参数的数据类。
    """
    speaker: str = "liubei" 
    role: str = "system"
    speed: float = 1.0
    language: str = "zh"
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
        更新TTS选项。支持部分更新。
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
        self._opts = TTSOptions(**kwargs)
        self.url = url if url else WS_URL
        super().__init__(
            # 这里标记为 streaming=True，因为 WebSocket 本质就是流式的
            capabilities=tts.TTSCapabilities(streaming=False), 
            sample_rate=24000,
            num_channels=1,
        )

    def update_options(self, **kwargs) -> None:
        self._opts.update_options(**kwargs)

    def synthesize(self, text: str, *, conn_options=DEFAULT_API_CONNECT_OPTIONS) -> tts.ChunkedStream:
        return TTSChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    @property
    def name(self):
        return f"Zhiai WS-TTS-{self._opts.speaker}"


class TTSChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: TTS, input_text: str, conn_options):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._audio_generated = False
    
    async def _run(self, output_emitter: tts.AudioEmitter):
        """
        Core execution loop for WebSocket streaming.
        WebSocket 流式传输的核心执行循环。
        """
        if self._audio_generated:
            return

        current_opts = self._tts._opts
        
        # 构造 WebSocket 发送的参数
        payload = {
            "text": self.input_text,
            "speaker": current_opts.speaker,
            "max_tokens_per_sentence": current_opts.max_tokens_per_sentence,
            "speed": current_opts.speed,
            "role": current_opts.role,
            # 如果服务端支持 language 参数，可以在此添加
        }

        try:
            # 建立 WebSocket 连接
            async with websockets.connect(self._tts.url) as websocket:
                # 1. 发送 JSON 配置参数
                await websocket.send(json.dumps(payload))
                
                # 初始化 AudioEmitter，告知 LiveKit 音频格式
                # 假设服务端返回的是 raw PCM 或者 LiveKit 能自动识别的格式
                output_emitter.initialize(
                    request_id=str(id(self)),
                    sample_rate=self._tts.sample_rate,
                    num_channels=1,
                    # 如果服务端发回的是带 WAV 头的流，LiveKit 可能会处理，
                    # 但通常流式 TTS 建议发送 raw pcm。
                    mime_type="audio/pcm", 
                )

                # 2. 循环接收数据
                async for message in websocket:
                    if isinstance(message, bytes):
                        # 收到二进制音频数据，推送到 LiveKit 缓冲区
                        output_emitter.push(message)
                    
                    elif isinstance(message, str):
                        # 处理可能的控制信息或错误信息
                        try:
                            msg_json = json.loads(message)
                            if msg_json.get("status") == "error":
                                error_msg = msg_json.get("error", "Unknown server error")
                                raise RuntimeError(f"Server returned error: {error_msg}")
                        except json.JSONDecodeError:
                            pass # 忽略非 JSON 的文本消息

                # 循环结束（WebSocket 关闭），刷新缓冲区
                output_emitter.flush()
                self._audio_generated = True

        except websockets.exceptions.ConnectionClosed:
            # 连接正常或异常关闭，如果数据已生成则视为完成
            if not self._audio_generated:
                raise RuntimeError("WebSocket connection closed unexpectedly before audio generation.")
        except Exception as e:
            raise RuntimeError(f"Zhiai TTS synthesis failed: {str(e)}")
        finally:
            # 只有在没有抛出异常导致外层捕获时，才需要手动结束输入
            # LiveKit 的 _run 如果抛出异常，外层会自动处理
            pass