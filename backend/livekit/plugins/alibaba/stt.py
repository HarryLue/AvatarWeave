import asyncio
import dataclasses
import json
import os
import uuid
import weakref
import numpy as np  # 新增：用于生成测试音频波形
from dataclasses import dataclass
from typing import Any

import aiohttp
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    LanguageCode,
    stt,
    utils,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given
from livekit.agents.log import logger

@dataclass
class STTOptions:
    """DashScope STT配置选项"""
    model: str           # 语音识别模型名称
    sample_rate: int     # 音频采样率，默认为16kHz
    num_channels: int    # 声道数，通常为1（单声道）
    format: str          # 音频格式，LiveKit默认使用PCM
    endpoint_url: str    # WebSocket API端点URL

class STT(stt.STT):
    """DashScope STT主类，继承LiveKit STT基类"""
    
    def __init__(
        self,
        *,
        model: str = "paraformer-realtime-v2",  # 默认使用实时语音识别模型
        sample_rate: int = 16000,
        api_key: NotGivenOr[str] = NOT_GIVEN,    # API密钥，支持环境变量
        http_session: aiohttp.ClientSession | None = None,
        base_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/",  # DashScope WebSocket地址
    ) -> None:
        """
        创建DashScope STT实例
        
        Args:
            model: DashScope模型名称，默认为"paraformer-realtime-v2"
            sample_rate: 音频采样率Hz，默认为16000
            api_key: DashScope API密钥，未提供时读取DASHSCOPE_API_KEY环境变量
            http_session: 可选的aiohttp会话
            base_url: WebSocket基础URL
        """
        # 配置LiveKit STT能力：支持流式识别、中间结果、不支持说话人分割
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,          # 支持流式识别
                interim_results=True,    # 支持中间识别结果
                diarization=False,       # 不支持说话人分割
            )
        )

        # 获取API密钥，支持参数和环境变量
        dashscope_api_key = api_key if is_given(api_key) else os.environ.get("DASHSCOPE_API_KEY")
        if not dashscope_api_key:
            raise ValueError(
                "DashScope API key is required, either as argument or set "
                "DASHSCOPE_API_KEY environment variable"
            )
        self._api_key = dashscope_api_key

        # 创建配置对象
        self._opts = STTOptions(
            model=model,
            sample_rate=sample_rate,
            num_channels=1,                    # LiveKit默认单声道
            format="pcm",                      # LiveKit默认PCM格式
            endpoint_url=base_url,
        )
        self._session = http_session
        self._streams = weakref.WeakSet[SpeechStream]()  # 弱引用存储所有流

    @property
    def model(self) -> str:
        """获取当前使用的模型名称"""
        return self._opts.model

    @property
    def provider(self) -> str:
        """返回STT提供商名称"""
        return "DashScope"

    def _ensure_session(self) -> aiohttp.ClientSession:
        """确保HTTP会话存在"""
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _recognize_impl(
        self,
        buffer: rtc.AudioFilter,
        *,
        language: NotGivenOr[LanguageCode | str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        """非流式识别（未实现，要求使用stream方法）"""
        raise NotImplementedError("Non-streaming recognition is not implemented for this DashScope provider. Use stream().")

    def stream(
        self,
        *,
        language: NotGivenOr[LanguageCode | str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "SpeechStream":
        """创建新的SpeechStream实例用于流式识别"""
        stream = SpeechStream(
            stt=self,
            conn_options=conn_options,
            opts=self._opts,
            api_key=self._api_key,
            http_session=self._ensure_session(),
        )
        self._streams.add(stream)
        return stream

class SpeechStream(stt.SpeechStream):
    """流式语音识别核心实现类"""
    
    def __init__(
        self,
        *,
        stt: STT,
        opts: STTOptions,
        conn_options: APIConnectOptions,
        api_key: str,
        http_session: aiohttp.ClientSession,
    ) -> None:
        """初始化SpeechStream"""
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=opts.sample_rate)
        self._opts = opts                    # STT配置
        self._api_key = api_key              # API密钥
        self._session = http_session         # HTTP会话
        self._speaking = False               # 说话状态标记
        
        # DashScope任务ID（32位十六进制）
        self._task_id = uuid.uuid4().hex[:32]
        self._request_id = self._task_id
        
        # 状态管理事件
        self._reconnect_event = asyncio.Event()
        self._task_started_event = asyncio.Event()  # 等待服务器确认任务开始

    async def _run(self) -> None:
        """主运行循环：管理WebSocket连接和双向数据流"""
        closing_ws = False  # WebSocket关闭标志

        @utils.log_exceptions(logger=logger)
        async def send_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            """
            发送任务协程：
            1. 等待task-started确认
            2. 处理LiveKit音频输入流（_input_ch）
            3. 使用AudioByteStream分片发送PCM数据
            """
            nonlocal closing_ws

            # 🔒 关键阻塞点：等待服务器确认task-started
            await self._task_started_event.wait()
            # print("✅ task-started确认收到，开始处理音频流")

            # 创建音频字节流处理器（50ms分片）
            audio_bstream = utils.audio.AudioByteStream(
                sample_rate=self._opts.sample_rate,
                num_channels=self._opts.num_channels,
                samples_per_channel=self._opts.sample_rate // 20,  # ~50ms分片
            )

            # 异步迭代LiveKit音频输入通道
            async for data in self._input_ch:
                frames: list[rtc.AudioFrame] = []
                
                # 处理音频帧或结束哨兵
                if isinstance(data, rtc.AudioFrame):
                    # 将原始PCM数据写入字节流，获取分片帧
                    frames.extend(audio_bstream.write(data.data.tobytes()))
                elif isinstance(data, self._FlushSentinel):
                    # 刷新缓冲区并发送结束指令
                    frames.extend(audio_bstream.flush())
                    
                    # 发送finish-task指令
                    finish_task_message = {
                        "header": {
                            "action": "finish-task",
                            "task_id": self._task_id,
                            "streaming": "duplex"
                        },
                        "payload": {"input": {}}
                    }
                    await ws.send_str(json.dumps(finish_task_message))
                    # print("📤 发送finish-task指令")

                # 发送所有分片音频帧（二进制数据）
                for frame in frames:
                    await ws.send_bytes(frame.data.tobytes())

            closing_ws = True
            # print("🔚 send_task完成")

        @utils.log_exceptions(logger=logger)
        async def recv_task(ws: aiohttp.ClientWebSocketResponse) -> None:
            """接收任务协程：处理DashScope服务器响应"""
            nonlocal closing_ws
            while True:
                # print(f"WS状态: {ws.closed}, closing_ws: {closing_ws}")
                
                # 接收WebSocket消息
                msg = await ws.receive()
                # print(f"收到消息类型: {msg.type}, 数据: {msg.data}")

                # 处理连接关闭消息
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    if closing_ws or self._session.closed:
                        # print("正常WebSocket关闭")
                        return
                    raise APIStatusError(
                        message="DashScope connection closed unexpectedly",
                        status_code=ws.close_code or -1,
                        body=f"{msg.data=} {msg.extra=}",
                    )

                # 忽略非文本消息
                if msg.type != aiohttp.WSMsgType.TEXT:
                    logger.warning("unexpected DashScope message type %s", msg.type)
                    continue

                # 处理服务器事件（核心逻辑）
                try:
                    self._process_stream_event(json.loads(msg.data), ws)
                except Exception:
                    logger.exception("failed to process DashScope message")

        # === 主连接逻辑 ===
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            # 1. 建立WebSocket连接
            ws = await self._connect_ws()
            
            # 2. 发送run-task配置
            await self._send_run_task(ws)
            # print("📤 run-task已发送，等待task-started确认")

            # 3. 启动发送/接收协程
            tasks = [
                asyncio.create_task(send_task(ws)),
                asyncio.create_task(recv_task(ws)),
            ]
            # print("🚀 启动 send/recv 任务")
            tasks_group = asyncio.gather(*tasks)
            
            try:
                await tasks_group  # 阻塞直到任一协程异常或完成
            except Exception as e:
                # print(f"❌ tasks_group异常: {e}")
                raise
            finally:
                # 优雅取消任务
                await utils.aio.gracefully_cancel(*tasks)
                tasks_group.cancel()
        finally:
            # 确保WebSocket关闭
            if ws is not None:
                await ws.close()

    async def _connect_ws(self) -> aiohttp.ClientWebSocketResponse:
        """建立安全的WebSocket连接到DashScope"""
        try:
            ws = await asyncio.wait_for(
                self._session.ws_connect(
                    self._opts.endpoint_url,
                    headers={"Authorization": f"bearer {self._api_key}"},  # Bearer令牌认证
                ),
                self._conn_options.timeout,  # 连接超时
            )
            logger.debug(f"Established new DashScope STT WebSocket connection (Task ID: {self._task_id})")
            return ws
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
            raise APIConnectionError("failed to connect to DashScope") from e

    async def _send_run_task(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """发送run-task初始化消息，配置ASR参数"""
        run_task_message = {
            "header": {
                "action": "run-task",      # 启动任务动作
                "task_id": self._task_id,  # 唯一任务ID
                "streaming": "duplex"      # 双向流式模式
            },
            "payload": {
                "task_group": "audio",     # 音频任务组
                "task": "asr",             # 自动语音识别任务
                "function": "recognition", # 识别功能
                "model": self._opts.model, # 模型名称
                "parameters": {
                    "sample_rate": self._opts.sample_rate,  # 16kHz
                    "format": self._opts.format             # PCM格式
                },
                "input": {}                # 空输入等待音频流
            }
        }
        await ws.send_str(json.dumps(run_task_message))
        # print(f"📤 run-task发送成功，TaskID: {self._task_id}")

    def _process_stream_event(self, data: dict, ws: aiohttp.ClientWebSocketResponse) -> None:
        """
        处理DashScope服务器事件（核心协议解析）
        
        事件类型：
        - task-started: 任务开始确认
        - result-generated: 识别结果（实时/最终）
        - task-finished: 任务完成
        - task-failed: 任务失败
        """
        # print(f"🔥 收到服务器事件: {data}")
        # print(f"📡 事件类型: {data.get('header', {}).get('event')}")
        header = data.get("header", {})
        event_type = header.get("event")

        if event_type == "task-started":
            """服务器确认任务已启动，解除send_task阻塞"""
            # print("✅ task-started确认，send_task可以开始发送音频")
            self._task_started_event.set()
            
        elif event_type == "result-generated":
            """实时识别结果处理"""
            payload = data.get("payload", {})
            output = payload.get("output", {})
            sentence = output.get("sentence", {})
            text = sentence.get("text", "")
            
            if not text:  # 空结果直接返回
                return

            # 发送开始说话事件（首次有识别结果）
            if not self._speaking:
                self._speaking = True
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

            # 判断是否最终结果
            is_final = sentence.get("sentence_end", False) 

            # 创建SpeechData对象
            speech_data = stt.SpeechData(
                language=LanguageCode("zh"),  # DashScope主要支持中文
                start_time=sentence.get("begin_time", 0) / 1000.0,  # 转换为秒
                end_time=(sentence.get("end_time") or 0) / 1000.0,
                confidence=1.0,              # DashScope未提供置信度，固定为1.0
                text=text,
                words=[]                     # 未实现单词级时间戳
            )

            # 根据is_end字段选择事件类型
            event_type_enum = (
                stt.SpeechEventType.FINAL_TRANSCRIPT 
                if is_final 
                else stt.SpeechEventType.INTERIM_TRANSCRIPT
            )
            
            # 发送识别事件到LiveKit管道
            event = stt.SpeechEvent(
                type=event_type_enum,
                request_id=self._request_id,
                alternatives=[speech_data],
            )
            self._event_ch.send_nowait(event)
            # print(f"🎤 识别结果: '{text}' (最终: {is_final})")

            # 最终结果时发送结束说话事件
            if is_final and self._speaking:
                self._speaking = False
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))

        elif event_type == "task-finished":
            """任务正常完成"""
            if self._speaking:
                self._speaking = False
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH))
            
            # 记录计费时长
            usage = data.get("payload", {}).get("usage", {}).get("duration")
            if usage:
                logger.debug(f"DashScope Task Finished. Billed duration: {usage}s")

        elif event_type == "task-failed":
            """任务失败处理"""
            error_message = header.get("error_message", "Unknown error")
            logger.error(f"DashScope task failed: {error_message}")
            raise APIStatusError(
                message=error_message,
                status_code=-1,
                body=json.dumps(data)
            )