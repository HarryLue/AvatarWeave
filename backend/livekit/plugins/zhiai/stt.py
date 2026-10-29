
from livekit.agents import stt, APIConnectionError
from livekit.agents.types import NotGivenOr, NOT_GIVEN
from livekit.agents.utils import AudioBuffer
from livekit.plugins.zhiai.simple_asr_client import SimpleASRClient
import tempfile


class STT(stt.STT):
    def __init__(self, server_url: str, model_name: str):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.server_url = server_url
        self.model_name = model_name
        self.asr_client = None

    async def connect(self):
        # 初始化SimpleASRClient实例
        self.asr_client = SimpleASRClient(server_url=self.server_url, model_name=self.model_name)
        # 这里封装connect为异步，如果没有异步接口，建议用线程池或者直接同步调用
        connected = self.asr_client.connect()
        if not connected:
            raise APIConnectionError("无法连接到ASR服务器")
        return connected

    async def close(self):
        if self.asr_client:
            self.asr_client.close()
            self.asr_client = None

    async def _recognize_impl(self, buffer: AudioBuffer, *, language=NOT_GIVEN, **kwargs) -> stt.SpeechEvent:
        if not self.asr_client:
            await self.connect()

        wav_bytes = buffer.to_wav_bytes()

        # 使用临时文件保存wav bytes
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp_wav:
            tmp_wav.write(wav_bytes)
            tmp_wav.flush()  # 确保写入磁盘

            result_text = self.asr_client.recognize(tmp_wav.name)

        if result_text:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(text=result_text, language=language if language is not NOT_GIVEN else "zh")],
            )
        else:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[],
            )

