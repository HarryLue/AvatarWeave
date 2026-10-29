# Copyright 2024
# LiveKit Agent STT Plugin for Volcengine (ByteDance) BigModel ASR

from __future__ import annotations

import asyncio
import gzip
import json
import os
import struct
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

import aiohttp

from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    stt,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer, is_given
from livekit.agents.voice.io import TimedString

from livekit.agents.log import logger


# --- Volcengine Protocol Constants ---

class ProtocolVersion:
    V1 = 0b0001

class MessageType:
    CLIENT_FULL_REQUEST = 0b0001
    CLIENT_AUDIO_ONLY_REQUEST = 0b0010
    SERVER_FULL_RESPONSE = 0b1001
    SERVER_ERROR_RESPONSE = 0b1111

class MessageTypeSpecificFlags:
    NO_SEQUENCE = 0b0000
    POS_SEQUENCE = 0b0001
    NEG_SEQUENCE = 0b0010
    NEG_WITH_SEQUENCE = 0b0011

class SerializationType:
    NO_SERIALIZATION = 0b0000
    JSON = 0b0001

class CompressionType:
    NO_COMPRESSION = 0b0000
    GZIP = 0b0001


# --- Protocol Helpers ---

class AsrRequestHeader:
    def __init__(self) -> None:
        self.message_type = MessageType.CLIENT_FULL_REQUEST
        self.message_type_specific_flags = MessageTypeSpecificFlags.POS_SEQUENCE
        self.serialization_type = SerializationType.JSON
        self.compression_type = CompressionType.GZIP
        self.reserved_data = bytes([0x00])

    def with_message_type(self, message_type: int) -> "AsrRequestHeader":
        self.message_type = message_type
        return self

    def with_message_type_specific_flags(self, flags: int) -> "AsrRequestHeader":
        self.message_type_specific_flags = flags
        return self

    def with_serialization_type(self, serialization_type: int) -> "AsrRequestHeader":
        self.serialization_type = serialization_type
        return self

    def with_compression_type(self, compression_type: int) -> "AsrRequestHeader":
        self.compression_type = compression_type
        return self

    def to_bytes(self) -> bytes:
        header = bytearray()
        header.append((ProtocolVersion.V1 << 4) | 1)  # header size = 1 (*4 bytes)
        header.append((self.message_type << 4) | self.message_type_specific_flags)
        header.append((self.serialization_type << 4) | self.compression_type)
        header.extend(self.reserved_data)
        return bytes(header)

    @staticmethod
    def default_header() -> "AsrRequestHeader":
        return AsrRequestHeader()


class AsrResponse:
    def __init__(self) -> None:
        self.code = 0
        self.event = 0
        self.is_last_package = False
        self.payload_sequence = 0
        self.payload_size = 0
        self.payload_msg: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "event": self.event,
            "is_last_package": self.is_last_package,
            "payload_sequence": self.payload_sequence,
            "payload_size": self.payload_size,
            "payload_msg": self.payload_msg,
        }


class ResponseParser:
    @staticmethod
    def parse_response(msg: bytes) -> AsrResponse:
        response = AsrResponse()

        header_size = msg[0] & 0x0F
        message_type = msg[1] >> 4
        message_type_specific_flags = msg[1] & 0x0F
        serialization_method = msg[2] >> 4
        message_compression = msg[2] & 0x0F

        payload = msg[header_size * 4 :]

        if message_type_specific_flags & 0x01:
            response.payload_sequence = struct.unpack(">i", payload[:4])[0]
            payload = payload[4:]
        if message_type_specific_flags & 0x02:
            response.is_last_package = True
        if message_type_specific_flags & 0x04:
            response.event = struct.unpack(">i", payload[:4])[0]
            payload = payload[4:]

        if message_type == MessageType.SERVER_FULL_RESPONSE:
            response.payload_size = struct.unpack(">I", payload[:4])[0]
            payload = payload[4:]
        elif message_type == MessageType.SERVER_ERROR_RESPONSE:
            response.code = struct.unpack(">i", payload[:4])[0]
            response.payload_size = struct.unpack(">I", payload[4:8])[0]
            payload = payload[8:]

        if not payload:
            return response

        if message_compression == CompressionType.GZIP:
            try:
                payload = gzip.decompress(payload)
            except Exception as e:
                logger.error(f"Failed to decompress payload: {e}")
                return response

        try:
            if serialization_method == SerializationType.JSON:
                response.payload_msg = json.loads(payload.decode("utf-8"))
        except Exception as e:
            logger.error(f"Failed to parse payload: {e}")

        return response


# --- LiveKit Plugin Implementation ---

@dataclass
class STTOptions:
    app_key: str
    access_key: str
    language: str
    model: str = "bigmodel"
    sample_rate: int = 16000
    endpoint_url: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    resource_id: str = "volc.bigasr.sauc.duration"
    #volc.bigasr.sauc.duration是豆包流式语音识别模型 1.0
    #volc.seedasr.sauc.duration是豆包流式语音识别模型 2.0
    enable_itn: bool = True
    enable_punc: bool = True
    enable_ddc: bool = False
    enable_nonstream: bool = False


class STT(stt.STT):
    def __init__(
        self,
        *,
        app_key: NotGivenOr[str] = NOT_GIVEN,
        access_key: NotGivenOr[str] = NOT_GIVEN,
        language: str = "zh-CN",
        model: str = "bigmodel",
        sample_rate: int = 16000,
        endpoint_url: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
        resource_id: str = "volc.bigasr.sauc.duration",
        enable_itn: bool = True,
        enable_punc: bool = True,
        enable_ddc: bool = False,
        enable_nonstream: bool = False,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Create a new instance of Volcengine STT.

        Args:
            app_key: Your Volcengine App Key. Fallbacks to VOLC_APP_KEY env var.
            access_key: Your Volcengine Access Key. Fallbacks to VOLC_ACCESS_KEY env var.
            language: Language code (e.g., zh-CN, en-US). Defaults to "zh-CN".
            model: The model name, default is "bigmodel".
            sample_rate: Audio sample rate in Hz. Default 16000.
            endpoint_url: WebSocket API endpoint.
            resource_id: Model resource ID.
        """
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                aligned_transcript="word",
                offline_recognize=False,
            )
        )

        volc_app_key = app_key if is_given(app_key) else os.environ.get("VOLC_APP_KEY")
        if not volc_app_key:
            raise ValueError("Volcengine app_key is required")

        volc_access_key = access_key if is_given(access_key) else os.environ.get("VOLC_ACCESS_KEY")
        if not volc_access_key:
            raise ValueError("Volcengine access_key is required")

        self._opts = STTOptions(
            app_key=volc_app_key,
            access_key=volc_access_key,
            language=language,
            model=model,
            sample_rate=sample_rate,
            endpoint_url=endpoint_url,
            resource_id=resource_id,
            enable_itn=enable_itn,
            enable_punc=enable_punc,
            enable_ddc=enable_ddc,
            enable_nonstream=enable_nonstream,
        )
        self._session = http_session
        self._streams = weakref.WeakSet[RecognizeStream]()

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Volcengine"

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        raise NotImplementedError(
            "Volcengine WebSocket API does not support non-streaming recognize directly. Use with a StreamAdapter."
        )

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> RecognizeStream:
        opts = self._opts
        if is_given(language):
            opts = STTOptions(**{**self._opts.__dict__, "language": language})

        stream = RecognizeStream(
            stt=self,
            conn_options=conn_options,
            opts=opts,
            http_session=self._ensure_session(),
        )
        self._streams.add(stream)
        return stream


class RecognizeStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: STT,
        opts: STTOptions,
        conn_options: APIConnectOptions,
        http_session: aiohttp.ClientSession,
    ) -> None:
        # LiveKit STT parent setup with explicitly requested sample rate
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=opts.sample_rate)
        self._opts = opts
        self._session = http_session

        self._speaking = False
        self._request_id = str(uuid.uuid4())
        self._seq = 1
        
        # State tracking for Volcengine utterance indexes
        self._last_finalized_index = 0

    def _reset_protocol_session_state(self) -> None:
        """Reset per-websocket protocol state before each (re)connect."""
        self._request_id = str(uuid.uuid4())
        self._seq = 1
        self._last_finalized_index = 0
        self._speaking = False

    def _build_auth_headers(self) -> dict[str, str]:
        return {
            "X-Api-Resource-Id": self._opts.resource_id,
            "X-Api-Request-Id": self._request_id,
            "X-Api-Access-Key": self._opts.access_key,
            "X-Api-App-Key": self._opts.app_key,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def _build_full_client_request(self) -> bytes:
        header = AsrRequestHeader.default_header().with_message_type_specific_flags(
            MessageTypeSpecificFlags.POS_SEQUENCE
        )

        payload = {
            "user": {"uid": "livekit_agent_stt"},
            "audio": {
                "format": "pcm",
                "codec": "raw",
                "rate": self._opts.sample_rate,
                "bits": 16,
                "channel": 1,
                "language": self._opts.language,
            },
            "request": {
                "model_name": self._opts.model,
                "enable_itn": self._opts.enable_itn,
                "enable_punc": self._opts.enable_punc,
                "enable_ddc": self._opts.enable_ddc,
                "show_utterances": True,
                "enable_nonstream": self._opts.enable_nonstream,
                "result_type": "full",

                # # ======== 新增下面这两个关键参数 ========
                "end_window_size": 800,         # 静音 800ms 直接强制判停，输出 definite=true
                "force_to_speech_time": 1000,   # 音频时长超过 1000ms 后才开始进行判停检测，防止极短噪音被误判
            },
        }

        print("Full Client Request Payload:", json.dumps(payload, indent=2))

        payload_bytes = json.dumps(payload).encode("utf-8")
        compressed_payload = gzip.compress(payload_bytes)

        request = bytearray()
        request.extend(header.to_bytes())
        request.extend(struct.pack(">i", self._seq))
        request.extend(struct.pack(">I", len(compressed_payload)))
        request.extend(compressed_payload)

        return bytes(request)

    def _build_audio_request(self, segment: bytes, is_last: bool = False) -> bytes:
        header = AsrRequestHeader.default_header()
        seq = self._seq

        if is_last:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.NEG_WITH_SEQUENCE)
            seq = -seq
        else:
            header.with_message_type_specific_flags(MessageTypeSpecificFlags.POS_SEQUENCE)

        header.with_message_type(MessageType.CLIENT_AUDIO_ONLY_REQUEST)
        header.with_serialization_type(SerializationType.NO_SERIALIZATION)

        compressed_segment = gzip.compress(segment)

        request = bytearray()
        request.extend(header.to_bytes())
        request.extend(struct.pack(">i", seq))
        request.extend(struct.pack(">I", len(compressed_segment)))
        request.extend(compressed_segment)

        return bytes(request)

    async def _run(self) -> None:
        closing_ws = False
        
        # 火山引擎推荐 200ms 分包。计算 200ms 16kHz 16-bit 单声道音频的字节数：
        # 16000 采样率 * 2 bytes (16位) * 1 声道 * 0.200 秒 = 6400 bytes
        bytes_per_200ms = int(self._opts.sample_rate * 2 * 1 * 0.200)

        @utils.log_exceptions(logger=logger)
        async def send_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            nonlocal closing_ws

            # 发送握手的 Full Client Request
            full_req = self._build_full_client_request()
            await ws.send_bytes(full_req)
            self._seq += 1

            pcm_buffer = bytearray()
            
            # 使用标准的异步迭代器，以便我们可以施加 wait_for 超时机制
            iterator = self._input_ch.__aiter__()

            while not closing_ws:
                try:
                    # 设定 0.15s 超时。如果 LiveKit 流暂停或静音，这里会触发 TimeoutError
                    data = await asyncio.wait_for(iterator.__anext__(), timeout=0.15)
                    
                    if isinstance(data, rtc.AudioFrame):
                        pcm_buffer.extend(data.data.tobytes())
                        
                        # 满 200ms 截断发一次包
                        while len(pcm_buffer) >= bytes_per_200ms:
                            chunk = bytes(pcm_buffer[:bytes_per_200ms])
                            del pcm_buffer[:bytes_per_200ms]
                            
                            audio_req = self._build_audio_request(chunk, is_last=False)
                            await ws.send_bytes(audio_req)
                            self._seq += 1

                    elif isinstance(data, self._FlushSentinel):
                        # 收到流结束信号，将最后尾部数据发空并标记 is_last=True
                        chunk = bytes(pcm_buffer)
                        pcm_buffer.clear()
                        
                        audio_req = self._build_audio_request(chunk, is_last=True)
                        await ws.send_bytes(audio_req)
                        closing_ws = True
                        break
                        
                except asyncio.TimeoutError:
                    # 【核心修复】：触发等包超时防护机制，作为心跳保活
                    if len(pcm_buffer) > 0:
                        # 如果缓冲中有残留碎片数据，优先发送碎片
                        chunk = bytes(pcm_buffer)
                        pcm_buffer.clear()
                        audio_req = self._build_audio_request(chunk, is_last=False)
                        await ws.send_bytes(audio_req)
                        self._seq += 1
                    else:
                        # 构造等同于 200ms 的完全静音帧 (全 0 字节) 发送给火山引擎
                        chunk = bytes(bytes_per_200ms)
                        audio_req = self._build_audio_request(chunk, is_last=False)
                        await ws.send_bytes(audio_req)
                        self._seq += 1
                        
                except StopAsyncIteration:
                    break

        @utils.log_exceptions(logger=logger)
        async def recv_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            nonlocal closing_ws
            while True:
                msg = await ws.receive()
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    if closing_ws or self._session.closed:
                        return
                    raise APIStatusError(
                        message="Volcengine connection closed unexpectedly",
                        status_code=ws.close_code or -1,
                    )

                if msg.type == aiohttp.WSMsgType.BINARY:
                    # 1. 解析数据（只捕获解析失败的异常）
                    try:
                        response = ResponseParser.parse_response(msg.data)
                    except Exception:
                        logger.exception("Failed to parse Volcengine response message")
                        continue
                        
                    # 2. 检查业务错误码（直接抛出，让 LiveKit 底层接管并执行重连）
                    if response.code != 0:
                        if response.code == 45000081:
                            # 用 info 级别记录即可，无需报 Error。这属于预期内的长连接保活重置。
                            logger.info("Volcengine STT: 45000081 等包超时 (Wait for packet timeout), 触发静默重连...")
                            raise APIStatusError(
                                message="Volcengine timeout (45000081)",
                                status_code=45000081
                            )
                        else:
                            logger.error(f"Volcengine STT Error Code {response.code}: {response.payload_msg}")
                            raise APIStatusError(
                                message=f"Volcengine returned error: {response.code}",
                                status_code=response.code
                            )
                    
                    # 3. 处理正常的流式事件
                    try:
                        self._process_stream_event(response)
                    except Exception:
                        logger.exception("Failed to process stream event")
                        continue
                        
                    if response.is_last_package:
                        if self._speaking:
                            self._speaking = False
                            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
                        return

        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            ws = await self._connect_ws()
            tasks = [
                asyncio.create_task(send_task(ws)),
                asyncio.create_task(recv_task(ws)),
            ]
            
            try:
                await asyncio.gather(*tasks)
            finally:
                await utils.aio.gracefully_cancel(*tasks)
        finally:
            if ws is not None:
                await ws.close()

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        # 每次建立新 websocket 会话前，重置协议会话态，避免 sequence 跨会话延续。
        self._reset_protocol_session_state()

        try:
            ws = await asyncio.wait_for(
                self._session.ws_connect(
                    self._opts.endpoint_url,
                    headers=self._build_auth_headers(),
                ),
                self._conn_options.timeout,
            )
            return ws
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
            raise APIConnectionError("Failed to connect to Volcengine STT WebSocket") from e

    # def _process_stream_event(self, response: AsrResponse) -> None:
    #     if not response.payload_msg:
    #         return

    #     result = response.payload_msg.get("result")
    #     if not result:
    #         return

    #     utterances = result.get("utterances", [])
        
    #     # Detect start of speech
    #     if not self._speaking and utterances:
    #         self._speaking = True
    #         self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

    #     for i, utt in enumerate(utterances):
    #         # Skip previously finalized utterances if 'result_type'='full' returns them repeatedly
    #         if i < self._last_finalized_index:
    #             continue

    #         words = []
    #         for w in utt.get("words", []):
    #             words.append(
    #                 TimedString(
    #                     text=w.get("text", ""),
    #                     start_time=(w.get("start_time", 0) / 1000.0) + self.start_time_offset,
    #                     end_time=(w.get("end_time", 0) / 1000.0) + self.start_time_offset,
    #                 )
    #             )

    #         speech_data = stt.SpeechData(
    #             language=self._opts.language,
    #             text=utt.get("text", ""),
    #             start_time=(utt.get("start_time", 0) / 1000.0) + self.start_time_offset,
    #             end_time=(utt.get("end_time", 0) / 1000.0) + self.start_time_offset,
    #             confidence=1.0, 
    #             words=words,
    #         )

    #         is_definite = utt.get("definite", False)
            
    #         if is_definite:
    #             self._event_ch.send_nowait(stt.SpeechEvent(
    #                 type=stt.SpeechEventType.FINAL_TRANSCRIPT,
    #                 alternatives=[speech_data]
    #             ))
    #             self._last_finalized_index += 1
    #         else:
    #             self._event_ch.send_nowait(stt.SpeechEvent(
    #                 type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
    #                 alternatives=[speech_data]
    #             ))

    def _process_stream_event(self, response: AsrResponse) -> None:
        if not response.payload_msg:
            return

        result = response.payload_msg.get("result")
        if not result:
            return

        utterances = result.get("utterances", [])
        
        # Detect start of speech
        if not self._speaking and utterances:
            self._speaking = True
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

        for i, utt in enumerate(utterances):
            # 1. 跳过已经 final 过的历史语段
            if i < self._last_finalized_index:
                continue

            # 2. 提取文本并去除首尾空格
            text = utt.get("text", "").strip()
            
            # 【核心修复】：如果当前语段还没识别出任何字（空字符串），直接跳过，不要发给 LiveKit
            if not text:
                continue

            words = []
            for w in utt.get("words", []):
                words.append(
                    TimedString(
                        text=w.get("text", ""),
                        start_time=(w.get("start_time", 0) / 1000.0) + self.start_time_offset,
                        end_time=(w.get("end_time", 0) / 1000.0) + self.start_time_offset,
                    )
                )

            speech_data = stt.SpeechData(
                language=self._opts.language,
                text=text,
                start_time=(utt.get("start_time", 0) / 1000.0) + self.start_time_offset,
                end_time=(utt.get("end_time", 0) / 1000.0) + self.start_time_offset,
                confidence=1.0, 
                words=words,
            )

            is_definite = utt.get("definite", False)
            
            if is_definite:
                self._event_ch.send_nowait(stt.SpeechEvent(
                    type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                    alternatives=[speech_data]
                ))
                self._last_finalized_index += 1
            else:
                self._event_ch.send_nowait(stt.SpeechEvent(
                    type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                    alternatives=[speech_data]
                ))