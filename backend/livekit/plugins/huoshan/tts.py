import asyncio
import copy
import io
import json
import logging
import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, List
import os

import websockets
from livekit.agents import tts
from livekit.agents import (
    APIError,
    APIConnectOptions
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

from .protocols import *

logger = logging.getLogger(__name__)

# ==============================================================================
# 1. 协议定义 (基于提供的 protocols.py 稍作精简与整合)
# ==============================================================================

# class MsgType(IntEnum):
#     Invalid = 0
#     FullClientRequest = 0b1
#     AudioOnlyClient = 0b10
#     FullServerResponse = 0b1001
#     AudioOnlyServer = 0b1011
#     FrontEndResultServer = 0b1100
#     Error = 0b1111
#     ServerACK = 0b1011

# class MsgTypeFlagBits(IntEnum):
#     NoSeq = 0
#     PositiveSeq = 0b1
#     LastNoSeq = 0b10
#     NegativeSeq = 0b11
#     WithEvent = 0b100

# class VersionBits(IntEnum):
#     Version1 = 1

# class HeaderSizeBits(IntEnum):
#     HeaderSize4 = 1

# class SerializationBits(IntEnum):
#     JSON = 0b1

# class CompressionBits(IntEnum):
#     None_ = 0

# class EventType(IntEnum):
#     """Event type enumeration"""

#     None_ = 0  # Default event

#     # 1 ~ 49 Upstream Connection events
#     StartConnection = 1
#     StartTask = 1  # Alias of StartConnection
#     FinishConnection = 2
#     FinishTask = 2  # Alias of FinishConnection

#     # 50 ~ 99 Downstream Connection events
#     ConnectionStarted = 50  # Connection established successfully
#     TaskStarted = 50  # Alias of ConnectionStarted
#     ConnectionFailed = 51  # Connection failed (possibly due to authentication failure)
#     TaskFailed = 51  # Alias of ConnectionFailed
#     ConnectionFinished = 52  # Connection ended
#     TaskFinished = 52  # Alias of ConnectionFinished

#     # 100 ~ 149 Upstream Session events
#     StartSession = 100
#     CancelSession = 101
#     FinishSession = 102

#     # 150 ~ 199 Downstream Session events
#     SessionStarted = 150
#     SessionCanceled = 151
#     SessionFinished = 152
#     SessionFailed = 153
#     UsageResponse = 154  # Usage response
#     ChargeData = 154  # Alias of UsageResponse

#     # 200 ~ 249 Upstream general events
#     TaskRequest = 200
#     UpdateConfig = 201

#     # 250 ~ 299 Downstream general events
#     AudioMuted = 250

#     # 300 ~ 349 Upstream TTS events
#     SayHello = 300

#     # 350 ~ 399 Downstream TTS events
#     TTSSentenceStart = 350
#     TTSSentenceEnd = 351
#     TTSResponse = 352
#     TTSEnded = 359
#     PodcastRoundStart = 360
#     PodcastRoundResponse = 361
#     PodcastRoundEnd = 362

#     # 450 ~ 499 Downstream ASR events
#     ASRInfo = 450
#     ASRResponse = 451
#     ASREnded = 459

#     # 500 ~ 549 Upstream dialogue events
#     ChatTTSText = 500  # (Ground-Truth-Alignment) text for speech synthesis

#     # 550 ~ 599 Downstream dialogue events
#     ChatResponse = 550
#     ChatEnded = 559

#     # 650 ~ 699 Downstream dialogue events
#     # Events for source (original) language subtitle
#     SourceSubtitleStart = 650
#     SourceSubtitleResponse = 651
#     SourceSubtitleEnd = 652
#     # Events for target (translation) language subtitle
#     TranslationSubtitleStart = 653
#     TranslationSubtitleResponse = 654
#     TranslationSubtitleEnd = 655

#     def __str__(self) -> str:
#         return self.name if self.name else f"EventType({self.value})"


# @dataclass
# class Message:
#     version: VersionBits = VersionBits.Version1
#     header_size: HeaderSizeBits = HeaderSizeBits.HeaderSize4
#     type: MsgType = MsgType.Invalid
#     flag: MsgTypeFlagBits = MsgTypeFlagBits.NoSeq
#     serialization: SerializationBits = SerializationBits.JSON
#     compression: CompressionBits = CompressionBits.None_

#     event: EventType = EventType.None_
#     session_id: str = ""
#     connect_id: str = ""
#     sequence: int = 0
#     error_code: int = 0
#     payload: bytes = b""

#     @classmethod
#     def from_bytes(cls, data: bytes) -> "Message":
#         if len(data) < 3:
#             raise ValueError(f"Data too short: expected at least 3 bytes, got {len(data)}")
#         type_and_flag = data[1]
#         msg = cls(type=MsgType(type_and_flag >> 4), flag=MsgTypeFlagBits(type_and_flag & 0b00001111))
#         msg.unmarshal(data)
#         return msg

#     def marshal(self) -> bytes:
#         buffer = io.BytesIO()
#         header = [
#             (self.version << 4) | self.header_size,
#             (self.type << 4) | self.flag,
#             (self.serialization << 4) | self.compression,
#         ]
#         header_size = 4 * self.header_size
#         header.extend([0] * (header_size - len(header)))
#         buffer.write(bytes(header))

#         for writer in self._get_writers():
#             writer(buffer)
#         return buffer.getvalue()

#     def unmarshal(self, data: bytes) -> None:
#         buffer = io.BytesIO(data)
#         buffer.read(3) # skip version/header/serialization logic
#         header_size = 4 * self.header_size
#         buffer.read(header_size - 3)

#         for reader in self._get_readers():
#             reader(buffer)

#     def _get_writers(self) -> List[Callable[[io.BytesIO], None]]:
#         writers = []
#         if self.flag == MsgTypeFlagBits.WithEvent:
#             writers.extend([self._write_event, self._write_session_id])
#         if self.type in [MsgType.FullClientRequest, MsgType.FullServerResponse, MsgType.AudioOnlyClient, MsgType.AudioOnlyServer]:
#             if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
#                 writers.append(self._write_sequence)
#         elif self.type == MsgType.Error:
#             writers.append(self._write_error_code)
#         writers.append(self._write_payload)
#         return writers

#     def _get_readers(self) -> List[Callable[[io.BytesIO], None]]:
#         readers = []
#         if self.type in [MsgType.FullClientRequest, MsgType.FullServerResponse, MsgType.AudioOnlyClient, MsgType.AudioOnlyServer]:
#             if self.flag in [MsgTypeFlagBits.PositiveSeq, MsgTypeFlagBits.NegativeSeq]:
#                 readers.append(self._read_sequence)
#         elif self.type == MsgType.Error:
#             readers.append(self._read_error_code)
        
#         if self.flag == MsgTypeFlagBits.WithEvent:
#             readers.extend([self._read_event, self._read_session_id, self._read_connect_id])
#         readers.append(self._read_payload)
#         return readers

#     def _write_event(self, buffer: io.BytesIO): buffer.write(struct.pack(">i", self.event))
#     def _write_session_id(self, buffer: io.BytesIO):
#         if self.event not in [EventType.StartConnection, EventType.FinishConnection, EventType.ConnectionStarted, EventType.ConnectionFailed]:
#             s_bytes = self.session_id.encode("utf-8")
#             buffer.write(struct.pack(">I", len(s_bytes)))
#             if s_bytes: buffer.write(s_bytes)
#     def _write_sequence(self, buffer: io.BytesIO): buffer.write(struct.pack(">i", self.sequence))
#     def _write_error_code(self, buffer: io.BytesIO): buffer.write(struct.pack(">I", self.error_code))
#     def _write_payload(self, buffer: io.BytesIO):
#         buffer.write(struct.pack(">I", len(self.payload)))
#         buffer.write(self.payload)

#     def _read_event(self, buffer: io.BytesIO):
#         data = buffer.read(4)
#         if data: self.event = EventType(struct.unpack(">i", data)[0])
#     def _read_session_id(self, buffer: io.BytesIO):
#         if self.event not in [EventType.StartConnection, EventType.FinishConnection, EventType.ConnectionStarted, EventType.ConnectionFailed, EventType.ConnectionFinished]:
#             size = buffer.read(4)
#             if size and (s := struct.unpack(">I", size)[0]) > 0:
#                 self.session_id = buffer.read(s).decode("utf-8")
#     def _read_connect_id(self, buffer: io.BytesIO):
#         if self.event in [EventType.ConnectionStarted, EventType.ConnectionFailed, EventType.ConnectionFinished]:
#             size = buffer.read(4)
#             if size and (s := struct.unpack(">I", size)[0]) > 0:
#                 self.connect_id = buffer.read(s).decode("utf-8")
#     def _read_sequence(self, buffer: io.BytesIO):
#         data = buffer.read(4)
#         if data: self.sequence = struct.unpack(">i", data)[0]
#     def _read_error_code(self, buffer: io.BytesIO):
#         data = buffer.read(4)
#         if data: self.error_code = struct.unpack(">I", data)[0]
#     def _read_payload(self, buffer: io.BytesIO):
#         size = buffer.read(4)
#         if size and (s := struct.unpack(">I", size)[0]) > 0:
#             self.payload = buffer.read(s)

# # Helper functions for the protocol
# async def receive_message(websocket) -> Message:
#     data = await websocket.recv()
#     if isinstance(data, bytes):
#         return Message.from_bytes(data)
#     raise ValueError(f"Unexpected text message: {data}")

# async def wait_for_event(websocket, msg_type: MsgType, event_type: EventType) -> Message:
#     while True:
#         msg = await receive_message(websocket)
#         if msg.type == msg_type and msg.event == event_type:
#             return msg
#         if msg.type == MsgType.Error:
#             raise Exception(f"Received error from server: Code {msg.error_code}")

async def send_msg_with_event(websocket, event: EventType, payload: bytes = b"{}", session_id: str = ""):
    msg = Message(type=MsgType.FullClientRequest, flag=MsgTypeFlagBits.WithEvent)
    msg.event = event
    msg.payload = payload
    msg.session_id = session_id
    await websocket.send(msg.marshal())


# ==============================================================================
# 2. LiveKit Agent 核心集成
# ==============================================================================

class TTS(tts.TTS):
    # def __init__(
    #     self,
    #     app_id: str,
    #     access_token: str,
    #     voice: str,
    #     sample_rate: int = 24000,
    # ) -> None:
    #     """
    #     初始化火山引擎的 TTS
    #     """
    #     super().__init__(
    #         capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
    #         sample_rate=sample_rate,
    #         num_channels=1,
    #     )
    #     self.app_id = app_id
    #     self.access_token = access_token
    #     self.voice = voice

    #     # 区分 S_ 开头的一般为 default 组资源，或者固定使用 10029
    #     self.resource_id = "volc.megatts.default" if voice.startswith("S_") else "volc.service_type.10029"
    
    def __init__(
        self,
        app_id: str | None = None,
        access_token: str | None = None,
        voice: str = "zh_female_qingxinnvsheng_mars_bigtts",
        sample_rate: int = 24000,
    ) -> None:
        """
        初始化火山引擎的 TTS
        
        Args:
            app_id: 应用ID，默认从环境变量 VOLC_APP_ID 或 VOLC_APP_KEY 读取
            access_token: 访问令牌，默认从环境变量 VOLC_ACCESS_TOKEN 或 VOLC_ACCESS_KEY 读取
            voice: 声音ID，默认使用清新女声
            sample_rate: 采样率，默认 24000
        """
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        
        # 从环境变量或参数获取认证信息
        self.app_id = app_id or os.environ.get("VOLC_APP_ID") or os.environ.get("VOLC_APP_KEY", "")
        self.access_token = access_token or os.environ.get("VOLC_ACCESS_TOKEN") or os.environ.get("VOLC_ACCESS_KEY", "")
        self.voice = voice
        
        # 验证认证信息
        if not self.app_id:
            raise ValueError("app_id 不能为空，请通过参数传入或设置环境变量 VOLC_APP_ID / VOLC_APP_KEY")
        if not self.access_token:
            raise ValueError("access_token 不能为空，请通过参数传入或设置环境变量 VOLC_ACCESS_TOKEN / VOLC_ACCESS_KEY")
        
        # 区分 S_ 开头的一般为 default 组资源，或者固定使用 10029
        self.resource_id = "volc.megatts.default" if voice.startswith("S_") else "volc.service_type.10029"

    @property
    def model(self) -> str:
        return "volcengine-bidirection"

    @property
    def provider(self) -> str:
        return "volcengine"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        """不支持 HTTP 块式调用，直接通过内部转接流式方法"""
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        """为当前段落生成一个新的流式会话实例"""
        return VolcengineSynthesizeStream(self, conn_options)


class VolcengineSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, volc_tts: TTS, conn_options: APIConnectOptions):
        super().__init__(tts=volc_tts, conn_options=conn_options)
        self._volc_tts = volc_tts

    # async def _run(self, output_emitter: tts.AudioEmitter) -> None:
    #     """接管底层输入输出，管理 websocket 生命周期"""
    #     endpoint = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
    #     request_id = str(uuid.uuid4())
    #     session_id = str(uuid.uuid4())
        
    #     headers = {
    #         "X-Api-App-Key": self._volc_tts.app_id,
    #         "X-Api-Access-Key": self._volc_tts.access_token,
    #         "X-Api-Resource-Id": self._volc_tts.resource_id,
    #         "X-Api-Connect-Id": str(uuid.uuid4()),
    #     }

    #     base_request = {
    #         "user": {"uid": str(uuid.uuid4())},
    #         "namespace": "BidirectionalTTS",
    #         "req_params": {
    #             "speaker": self._volc_tts.voice,
    #             "audio_params": {
    #                 "format": "pcm",           # 使用 PCM 无需额外解码，最低延迟
    #                 "sample_rate": self._volc_tts.sample_rate,
    #                 "enable_timestamp": True,
    #             },
    #             "additions": json.dumps({"disable_markdown_filter": False}),
    #         },
    #     }

    #     # 1. 建立 Websocket 握手
    #     try:
    #         ws = await websockets.connect(endpoint, additional_headers=headers, max_size=10 * 1024 * 1024)
    #     except Exception as e:
    #         raise APIError(f"Failed to connect to Volcengine WS: {e}")

    #     try:
    #         # 开始 Connection
    #         await send_msg_with_event(ws, EventType.StartConnection)
    #         await wait_for_event(ws, MsgType.FullServerResponse, EventType.ConnectionStarted)

    #         # 初始化 Livekit Audio Emitter
    #         output_emitter.initialize(
    #             request_id=request_id,
    #             sample_rate=self._volc_tts.sample_rate,
    #             num_channels=self._volc_tts.num_channels,
    #             mime_type="audio/pcm", # 匹配我们请求头中的 "format": "pcm"
    #             stream=True,
    #         )
    #         output_emitter.start_segment(segment_id=session_id)

    #         # 开始 Session
    #         start_req = copy.deepcopy(base_request)
    #         start_req["event"] = EventType.StartSession
    #         await send_msg_with_event(ws, EventType.StartSession, json.dumps(start_req).encode(), session_id)
    #         await wait_for_event(ws, MsgType.FullServerResponse, EventType.SessionStarted)

    #         # 2. 异步协程：将 Livekit Channel 的文字打成请求发往火山
    #         async def send_task():
    #             async for data in self._input_ch:
    #                 if isinstance(data, self._FlushSentinel):
    #                     # 收到结束信号，完成 Session
    #                     await send_msg_with_event(ws, EventType.FinishSession, b"{}", session_id)
    #                     break
    #                 elif isinstance(data, str):
    #                     # 支持逐字发送以最大化流式效率，无缝连接大模型的流式生成
    #                     for char in data:
    #                         if char == "*":
    #                             continue#跳过星号
    #                         if not char.strip():
    #                             continue#跳过空格
    #                         req = copy.deepcopy(base_request)
    #                         req["event"] = EventType.TaskRequest
    #                         req["req_params"]["text"] = char
    #                         await send_msg_with_event(ws, EventType.TaskRequest, json.dumps(req).encode(), session_id)
    #                         # 为保证长文本处理的健壮性，保留微小的调度交出时间
    #                         await asyncio.sleep(0.001)

    #         # 3. 异步协程：监听火山的下发音频将其推送给 Emitter
    #         async def recv_task():
    #             while True:
    #                 msg = await receive_message(ws)
    #                 if msg.type == MsgType.FullServerResponse and msg.event == EventType.SessionFinished:
    #                     break
    #                 elif msg.type == MsgType.AudioOnlyServer:
    #                     # 收到音频流数据，推给 Livekit 底层的 WebRTC 管道
    #                     output_emitter.push(msg.payload)
    #                 elif msg.type == MsgType.Error:
    #                     logger.error(f"TTS Error code {msg.error_code}")
    #                     raise APIError(f"Volcengine TTS Error: {msg.error_code}")

    #         # 4. 并发执行收发队列
    #         sender = asyncio.create_task(send_task())
    #         receiver = asyncio.create_task(recv_task())
            
    #         await asyncio.gather(sender, receiver)

    #     finally:
    #         # 5. Emitter 关闭及连接收尾
    #         output_emitter.end_segment()
            
    #         try:
    #             await send_msg_with_event(ws, EventType.FinishConnection)
    #             await wait_for_event(ws, MsgType.FullServerResponse, EventType.ConnectionFinished)
    #         except Exception as e:
    #             logger.debug(f"Volcengine connection shutdown incomplete: {e}")
    #         finally:
    #             await ws.close()

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        """接管底层输入输出，管理 websocket 生命周期"""
        endpoint = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
        request_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        
        headers = {
            "X-Api-App-Key": self._volc_tts.app_id,
            "X-Api-Access-Key": self._volc_tts.access_token,
            "X-Api-Resource-Id": self._volc_tts.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        base_request = {
            "user": {"uid": str(uuid.uuid4())},
            "namespace": "BidirectionalTTS",
            "req_params": {
                "speaker": self._volc_tts.voice,
                "audio_params": {
                    "format": "pcm",           # 使用 PCM 无需额外解码，最低延迟
                    "sample_rate": self._volc_tts.sample_rate,
                    "enable_timestamp": True,
                },
                "additions": json.dumps({"disable_markdown_filter": False}),
            },
        }

        # 1. 建立 Websocket 握手
        try:
            ws = await websockets.connect(endpoint, additional_headers=headers, max_size=10 * 1024 * 1024)
        except Exception as e:
            raise APIError(f"Failed to connect to Volcengine WS: {e}")

        sender = None
        receiver = None
        session_started = False
        
        try:
            # 开始 Connection
            await send_msg_with_event(ws, EventType.StartConnection)
            await wait_for_event(ws, MsgType.FullServerResponse, EventType.ConnectionStarted)

            # 初始化 Livekit Audio Emitter
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._volc_tts.sample_rate,
                num_channels=self._volc_tts.num_channels,
                mime_type="audio/pcm", # 匹配我们请求头中的 "format": "pcm"
                stream=True,
            )
            output_emitter.start_segment(segment_id=session_id)

            # 开始 Session
            start_req = copy.deepcopy(base_request)
            start_req["event"] = EventType.StartSession
            await send_msg_with_event(ws, EventType.StartSession, json.dumps(start_req).encode(), session_id)
            await wait_for_event(ws, MsgType.FullServerResponse, EventType.SessionStarted)
            session_started = True

            # 2. 异步协程：将 Livekit Channel 的文字打成请求发往火山
            async def send_task():
                async for data in self._input_ch:
                    if isinstance(data, self._FlushSentinel):
                        # 收到结束信号，完成 Session
                        await send_msg_with_event(ws, EventType.FinishSession, b"{}", session_id)
                        break
                    elif isinstance(data, str):
                        # 支持逐字发送以最大化流式效率，无缝连接大模型的流式生成
                        for char in data:
                            if char == "*":
                                continue#跳过星号
                            # if not char.strip():
                            #     continue#跳过空格
                            req = copy.deepcopy(base_request)
                            req["event"] = EventType.TaskRequest
                            req["req_params"]["text"] = char
                            await send_msg_with_event(ws, EventType.TaskRequest, json.dumps(req).encode(), session_id)
                            # 为保证长文本处理的健壮性，保留微小的调度交出时间
                            await asyncio.sleep(0.001)

            # 3. 异步协程：监听火山的下发音频将其推送给 Emitter
            async def recv_task():
                while True:
                    try:
                        msg = await receive_message(ws)
                    except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                        # 连接关闭或被取消
                        break
                        
                    if msg.type == MsgType.FullServerResponse and msg.event == EventType.SessionFinished:
                        break
                    elif msg.type == MsgType.AudioOnlyServer:
                        # 收到音频流数据，推给 Livekit 底层的 WebRTC 管道
                        output_emitter.push(msg.payload)
                    elif msg.type == MsgType.Error:
                        logger.error(f"TTS Error code {msg.error_code}")
                        raise APIError(f"Volcengine TTS Error: {msg.error_code}")

            # 4. 并发执行收发队列
            sender = asyncio.create_task(send_task())
            receiver = asyncio.create_task(recv_task())
            
            await asyncio.gather(sender, receiver, return_exceptions=True)

        except asyncio.CancelledError:
            # 任务被取消
            logger.debug("TTS task was cancelled")
            raise
            
        except Exception as e:
            logger.error(f"TTS task failed: {e}")
            raise
            
        finally:
            # 5. 清理任务
            if sender and not sender.done():
                sender.cancel()
            if receiver and not receiver.done():
                receiver.cancel()
                
            # 安全地结束音频段
            try:
                # 检查 output_emitter 是否有 end_segment 方法
                if hasattr(output_emitter, 'end_segment'):
                    # 检查 _streaming 属性是否存在
                    if hasattr(output_emitter, '_streaming'):
                        output_emitter.end_segment()
                    else:
                        # 如果没有 _streaming 属性，尝试其他清理方式
                        logger.debug("AudioEmitter has no _streaming attribute, skipping end_segment")
            except AttributeError as e:
                logger.debug(f"Error in end_segment: {e}")
            except Exception as e:
                logger.debug(f"Unexpected error in cleanup: {e}")
            
            # 清理 websocket 连接
            try:
                if session_started:
                    await send_msg_with_event(ws, EventType.FinishSession, b"{}", session_id)
            except Exception as e:
                logger.debug(f"Failed to send FinishSession: {e}")
                
            try:
                await send_msg_with_event(ws, EventType.FinishConnection)
            except Exception as e:
                logger.debug(f"Failed to send FinishConnection: {e}")
                
            try:
                if not ws.closed:
                    await ws.close()
            except Exception as e:
                logger.debug(f"Failed to close websocket: {e}")





