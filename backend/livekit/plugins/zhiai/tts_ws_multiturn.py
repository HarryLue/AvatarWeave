from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace

import aiohttp

from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    tts,
    utils,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    NotGivenOr,
)
from livekit.agents.utils import is_given
from .log import logger

# 默认配置，请根据实际情况修改
DEFAULT_WS_URL = "ws://127.0.0.1:11992/ws_multiturn/tts_stream"

@dataclass
class _TTSOptions:
    url: str
    speaker: str
    role: str
    speed: float

class TTS(tts.TTS):
    def __init__(
        self,
        *,
        url: str = DEFAULT_WS_URL,
        speaker: str = "liubei",
        role: str = "system",
        speed: float = 1.0,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=24000,
            num_channels=1,
        )

        self._opts = _TTSOptions(
            url=url,
            speaker=speaker,
            role=role,
            speed=speed,
        )
        self._session = http_session

        # 使用 LiveKit 的连接池来管理持久的 WS 连接
        self._pool = utils.ConnectionPool[aiohttp.ClientWebSocketResponse](
            connect_cb=self._connect_ws,
            close_cb=self._close_ws,
            max_session_duration=3600, # 连接最长保持1小时
        )

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _connect_ws(self, timeout: float) -> aiohttp.ClientWebSocketResponse:
        """建立 WebSocket 连接"""
        session = self._ensure_session()
        logger.debug(f"Connecting to Custom TTS WS: {self._opts.url}")
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(self._opts.url),
                timeout,
            )
            logger.info("Custom TTS WebSocket connected successfully.")
            return ws
        except Exception as e:
            logger.error(f"Failed to connect to Custom TTS WS: {e}")
            raise APIConnectionError(f"Connection failed: {e}") from e

    async def _close_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """优雅地关闭 WebSocket 连接"""
        try:
            # 根据你的协议，发送关闭命令
            if not ws.closed:
                logger.debug("Sending close command to TTS server.")
                await ws.send_str(json.dumps({"command": "close"}))
                # 可选：等待服务器响应关闭，避免立即断开
                try:
                     await asyncio.wait_for(ws.receive(), timeout=0.5)
                except (asyncio.TimeoutError, Exception):
                    pass
        except Exception as e:
            logger.warning(f"Error sending close command during WS cleanup: {e}")
        finally:
            await ws.close()

    def update_options(
        self,
        *,
        speaker: NotGivenOr[str] = NOT_GIVEN,
        role: NotGivenOr[str] = NOT_GIVEN,
        speed: NotGivenOr[float] = NOT_GIVEN,
    ) -> None:
        if is_given(speaker):
            self._opts.speaker = speaker
        if is_given(role):
            self._opts.role = role
        if is_given(speed):
            self._opts.speed = speed

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return ChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.SynthesizeStream:
        return SynthesizeStream(tts=self, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._pool.aclose()


class ChunkedStream(tts.ChunkedStream):
    """辅助适配器，用于非流式输入"""
    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._internal_stream = SynthesizeStream(tts=tts, conn_options=conn_options)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        self._internal_stream.push_text(self._input_text)
        self._internal_stream.flush()
        await self._internal_stream.aclose()
        await self._internal_stream._run(output_emitter)


class SynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions):
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        request_id = utils.shortuuid()
        
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._tts.sample_rate,
            num_channels=self._tts.num_channels,
            mime_type="audio/pcm",
            stream=True,
        )

        try:
            # 【修复关键点】：使用 async with 从连接池获取连接
            # 这样可以确保连接在使用后自动归还给池
            async with self._tts._pool.connection(timeout=self._conn_options.timeout) as ws:
                
                # 用于累积文本块的缓冲区
                text_buffer = ""
                
                # 处理来自 LiveKit 的输入文本流
                async for input_data in self._input_ch:
                    if isinstance(input_data, str):
                        text_buffer += input_data
                        # 这里可以添加断句逻辑，如果需要提前发送部分文本
                        continue

                    # 如果收到 Flush 信号且缓冲区有文本，则处理这一轮对话
                    if isinstance(input_data, self._FlushSentinel) and text_buffer:
                        # ws 在这里是可用的，因为它在 async with 块内
                        await self._process_turn(ws, output_emitter, text_buffer)
                        text_buffer = "" # 重置缓冲区准备下一轮

        except asyncio.TimeoutError:
            # 捕获获取连接时的超时
            raise APITimeoutError("Timeout getting connection to TTS server") from None
        except Exception as e:
             # 不要包装已经是 LiveKit API 的异常
            if isinstance(e, (APIConnectionError, APIStatusError, APITimeoutError)):
                raise
            logger.exception("Unexpected error in TTS stream")
            raise APIConnectionError(f"Unexpected error: {e}") from e
        # finally 块不再需要手动释放连接，async with 会自动处理

    async def _process_turn(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        output_emitter: tts.AudioEmitter,
        text: str
    ) -> None:
        """
        处理单轮对话：发送文本请求并等待音频块，直到收到 'finished'。
        这反映了原始客户端示例中的内部循环逻辑。
        """
        turn_id = utils.shortuuid()
        
        # 在 LiveKit 流中标记新音频段的开始
        output_emitter.start_segment(segment_id=turn_id)

        # 1. 构造并发送 JSON 请求
        request_data = {
            "text": text,
            "speaker": self._opts.speaker,
            "role": self._opts.role,
            "speed": self._opts.speed,
            "request_id": turn_id
        }
        logger.debug(f"Sending TTS turn request: {turn_id}, text len: {len(text)}")
        await ws.send_str(json.dumps(request_data))

        # 2. 接收当前轮次的循环
        received_bytes = 0
        while True:
            msg = await ws.receive()

            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            ):
                # 如果在对话过程中连接断开，抛出异常
                raise APIConnectionError("TTS WebSocket closed unexpectedly during turn")

            if msg.type == aiohttp.WSMsgType.BINARY:
                # --- 处理音频数据 ---
                data = msg.data
                if data:
                    output_emitter.push(data)
                    received_bytes += len(data)

            elif msg.type == aiohttp.WSMsgType.TEXT:
                # --- 处理控制信号 (JSON) ---
                try:
                    response = json.loads(msg.data)
                    status = response.get("status")

                    if status == "finished":
                        # 【关键点】收到本句结束信号
                        logger.debug(f"Turn {turn_id} finished. Total bytes: {received_bytes}")
                        # 告诉 LiveKit 这个音频段结束了
                        output_emitter.end_segment()
                        # 跳出内部循环，返回到外层循环等待下一个文本输入
                        break
                    
                    elif status == "error":
                        error_msg = response.get("error", "Unknown server error")
                        logger.error(f"TTS Server returned error: {error_msg}")
                        # 这里抛出状态错误，外层会捕获并可能重试或终止
                        raise APIStatusError(f"TTS Server Error: {error_msg}")

                except json.JSONDecodeError:
                    logger.warning(f"Received non-JSON text message from TTS server: {msg.data}")