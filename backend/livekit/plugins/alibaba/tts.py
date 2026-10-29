from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, cast

import aiohttp
import websockets

from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    APIError,
    APIStatusError,
    APITimeoutError,
    LanguageCode,
    tokenize,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given
from livekit.agents.voice.io import TimedString

from .constants import (
    USER_AGENT,
    REQUEST_ID_HEADER,
)
from .log import logger


@dataclass
class _TTSOptions:
    model: str
    voice: str
    format: str
    sample_rate: int
    volume: int
    rate: float
    pitch: float
    enable_ssml: bool
    api_key: str
    base_url: str
    word_timestamps: bool
    text_type: str


class TTS(tts.TTS):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "cosyvoice-v3-flash",
        voice: str = "longanyang",
        format: str = "mp3",
        sample_rate: int = 22050,
        volume: int = 50,   # 音量范围0-100
        rate: float = 1.0, # 语速范围0.5-2.0
        pitch: float = 1.0,# 音高范围0.5-2.0
        enable_ssml: bool = False,   # 是否启用SSML
        text_type: str = "PlainText",  # 文本类型，默认为PlainText，可选SSML
        http_session: aiohttp.ClientSession | None = None,  # 可选的HTTP会话
        tokenizer: NotGivenOr[tokenize.SentenceTokenizer] = NOT_GIVEN,   # 可选的分词器
        text_pacing: tts.SentenceStreamPacer | bool = False,   # 文本流控速选项，True表示启用默认的SentenceStreamPacer
        base_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference/", 
    ) -> None:
        """
        创建阿里语音合成实例

        参数:
            model (str, optional): 语音合成模型。默认为 "cosyvoice-v3-flash"
            voice (str, optional): 发音人声音。默认为 "longanyang"
            format (str, optional): 音频格式。默认为 "mp3"
            sample_rate (int, optional): 音频采样率(Hz)。默认为 22050
            volume (int, optional): 音量大小，范围0-100。默认为 50
            rate (float, optional): 语速，范围0.5-2.0。默认为 1.0
            pitch (float, optional): 音高，范围0.5-2.0。默认为 1.0
            enable_ssml (bool, optional): 是否启用SSML。默认为 False
            text_type (str, optional): 文本类型。默认为 "PlainText"
            api_key (str, optional): API密钥。如果未提供，从DASHSCOPE_API_KEY环境变量读取
            http_session (aiohttp.ClientSession, optional): 可选的HTTP会话
            tokenizer (tokenize.SentenceTokenizer, optional): 分词器
            text_pacing (tts.SentenceStreamPacer | bool, optional): 文本流控速
            base_url (str, optional): API基础URL。默认为阿里云WebSocket地址
        """

        super().__init__(
            capabilities=tts.TTSCapabilities(
                streaming=True,
                aligned_transcript=False,  # 阿里不支持词级时间戳
            ),
            sample_rate=sample_rate,
            num_channels=1,
        )

        dashscope_api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
        if not dashscope_api_key:
            raise ValueError(
                "阿里云API密钥是必需的，请通过参数传递或设置DASHSCOPE_API_KEY环境变量"
            )

        self._opts = _TTSOptions(
            model=model,
            voice=voice,
            format=format,
            sample_rate=sample_rate,
            volume=volume,
            rate=rate,
            pitch=pitch,
            enable_ssml=enable_ssml,
            text_type=text_type,
            api_key=dashscope_api_key,
            base_url=base_url,
            word_timestamps=False,  # 阿里不支持词级时间戳
        )

        self._session = http_session
        self._ws_connections = {}
        self._streams = weakref.WeakSet[AliSynthesizeStream]()
        self._sentence_tokenizer = (
            tokenizer if is_given(tokenizer) else tokenize.blingfire.SentenceTokenizer()
        )
        self._stream_pacer: tts.SentenceStreamPacer | None = None
        if text_pacing is True:
            self._stream_pacer = tts.SentenceStreamPacer()
        elif isinstance(text_pacing, tts.SentenceStreamPacer):
            self._stream_pacer = text_pacing

    @property
    def model(self) -> str:
        return self._opts.model

    @property
    def provider(self) -> str:
        return "Aliyun"

    def _ensure_session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = utils.http_context.http_session()
        return self._session

    async def _connect_ws(self, task_id: str) -> websockets.WebSocketClientProtocol:
        """创建WebSocket连接"""
        headers = {
            "Authorization": f"bearer {self._opts.api_key}",
            "User-Agent": USER_AGENT,
        }

        try:
            ws = await websockets.connect(
                self._opts.base_url,
                additional_headers=headers,
                ping_interval=None,
            )
            self._ws_connections[task_id] = ws
            logger.debug(f"建立阿里TTS WebSocket连接，任务ID: {task_id}")
            return ws
        except Exception as e:
            logger.error(f"连接阿里WebSocket失败: {e}")
            raise APIConnectionError() from e

    async def _close_ws(self, task_id: str) -> None:
        """关闭WebSocket连接"""
        if task_id in self._ws_connections:
            ws = self._ws_connections[task_id]
            await ws.close()
            del self._ws_connections[task_id]

    def prewarm(self) -> None:
        """预热连接池（阿里云不支持连接池，留空）"""
        pass

    def update_options(
        self,
        *,
        model: NotGivenOr[str] = NOT_GIVEN,
        voice: NotGivenOr[str] = NOT_GIVEN,
        format: NotGivenOr[str] = NOT_GIVEN,
        sample_rate: NotGivenOr[int] = NOT_GIVEN,
        volume: NotGivenOr[int] = NOT_GIVEN,
        rate: NotGivenOr[float] = NOT_GIVEN,
        pitch: NotGivenOr[float] = NOT_GIVEN,
        enable_ssml: NotGivenOr[bool] = NOT_GIVEN,
        text_type: NotGivenOr[str] = NOT_GIVEN,
    ) -> None:
        """
        更新语音合成配置

        参数:
            model (str, optional): 语音合成模型
            voice (str, optional): 发音人声音
            format (str, optional): 音频格式
            sample_rate (int, optional): 音频采样率
            volume (int, optional): 音量大小
            rate (float, optional): 语速
            pitch (float, optional): 音高
            enable_ssml (bool, optional): 是否启用SSML
            text_type (str, optional): 文本类型
        """
        if is_given(model):
            self._opts.model = model
        if is_given(voice):
            self._opts.voice = voice
        if is_given(format):
            self._opts.format = format
        if is_given(sample_rate):
            self._opts.sample_rate = sample_rate
        if is_given(volume):
            self._opts.volume = volume
        if is_given(rate):
            self._opts.rate = rate
        if is_given(pitch):
            self._opts.pitch = pitch
        if is_given(enable_ssml):
            self._opts.enable_ssml = enable_ssml
        if is_given(text_type):
            self._opts.text_type = text_type

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> AliChunkedStream:
        return AliChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> AliSynthesizeStream:
        stream = AliSynthesizeStream(tts=self, conn_options=conn_options)
        self._streams.add(stream)
        return stream

    async def aclose(self) -> None:
        """关闭所有连接"""
        for stream in list(self._streams):
            await stream.aclose()
        self._streams.clear()

        # 关闭所有WebSocket连接
        close_tasks = [self._close_ws(task_id) for task_id in list(self._ws_connections.keys())]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)


class AliChunkedStream(tts.ChunkedStream):
    """阿里语音合成流（非流式版本）"""

    def __init__(self, *, tts: TTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        """运行语音合成（非流式）"""
        # 阿里云暂未提供HTTP API，这里实现WebSocket单次请求
        task_id = str(uuid.uuid4())
        
        try:
            # 创建WebSocket连接
            ws = await self._tts._connect_ws(task_id)
            
            # 发送run-task指令
            run_task_cmd = {
                "header": {
                    "action": "run-task",
                    "task_id": task_id,
                    "streaming": "duplex"
                },
                "payload": {
                    "task_group": "audio",
                    "task": "tts",
                    "function": "SpeechSynthesizer",
                    "model": self._opts.model,
                    "parameters": {
                        "text_type": self._opts.text_type,
                        "voice": self._opts.voice,
                        "format": self._opts.format,
                        "sample_rate": self._opts.sample_rate,
                        "volume": self._opts.volume,
                        "rate": self._opts.rate,
                        "pitch": self._opts.pitch,
                        "enable_ssml": self._opts.enable_ssml
                    },
                    "input": {}
                }
            }
            
            await ws.send(json.dumps(run_task_cmd))
            
            # 发送continue-task指令
            continue_task_cmd = {
                "header": {
                    "action": "continue-task",
                    "task_id": task_id,
                    "streaming": "duplex"
                },
                "payload": {
                    "input": {
                        "text": self._input_text
                    }
                }
            }
            
            await ws.send(json.dumps(continue_task_cmd))
            
            # 发送finish-task指令
            finish_task_cmd = {
                "header": {
                    "action": "finish-task",
                    "task_id": task_id,
                    "streaming": "duplex"
                },
                "payload": {
                    "input": {}
                }
            }
            
            await ws.send(json.dumps(finish_task_cmd))
            
            # 接收音频数据
            output_emitter.initialize(
                request_id=task_id,
                sample_rate=self._opts.sample_rate,
                num_channels=1,
                mime_type=f"audio/{self._opts.format}",
            )
            
            audio_data = b""
            task_finished = False
            
            while not task_finished:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=self._conn_options.timeout)
                    
                    if isinstance(message, str):
                        msg_json = json.loads(message)
                        
                        if "header" in msg_json:
                            header = msg_json["header"]
                            
                            if "event" in header:
                                event = header["event"]
                                
                                if event == "task-finished":
                                    task_finished = True
                                elif event == "task-failed":
                                    error_msg = msg_json.get("error_message", "未知错误")
                                    raise APIError(f"阿里语音合成失败: {error_msg}")
                    else:
                        # 二进制消息（音频数据）
                        audio_data += message
                        
                except asyncio.TimeoutError:
                    raise APITimeoutError() from None
            
            # 发送音频数据
            if audio_data:
                output_emitter.push(audio_data)
            
            output_emitter.flush()
            
        except websockets.exceptions.ConnectionClosed as e:
            raise APIConnectionError() from e
        except Exception as e:
            if not isinstance(e, (APITimeoutError, APIError)):
                raise APIConnectionError() from e
            else:
                raise
        finally:
            await self._tts._close_ws(task_id)


class AliSynthesizeStream(tts.SynthesizeStream):
    def __init__(self, *, tts: TTS, conn_options: APIConnectOptions):
        super().__init__(tts=tts, conn_options=conn_options)
        self._tts: TTS = tts
        self._opts = replace(tts._opts)
        self._ws = None
        self._task_id = None
        self._input_sent_event = asyncio.Event()
        self._task_started = False
        self._task_finished = False
        self._sent_tokenizer_stream = None
        self._sent_tokens = deque[str]()

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        """运行流式语音合成"""
        self._task_id = str(uuid.uuid4())
        request_id = self._task_id
        
        # 初始化音频发射器
        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._opts.sample_rate,
            num_channels=1,
            mime_type=f"audio/{self._opts.format}",
            stream=True,
        )
        
        # 初始化分词器
        self._sent_tokenizer_stream = self._tts._sentence_tokenizer.stream()
        
        if self._tts._stream_pacer:
            self._sent_tokenizer_stream = self._tts._stream_pacer.wrap(
                sent_stream=self._sent_tokenizer_stream,
                audio_emitter=output_emitter,
            )
        
        # 创建WebSocket连接
        self._ws = await self._tts._connect_ws(self._task_id)
        
        # 启动任务
        tasks = [
            asyncio.create_task(self._input_task()),
            asyncio.create_task(self._sentence_stream_task(output_emitter)),
            asyncio.create_task(self._recv_task(output_emitter)),
        ]
        
        try:
            await asyncio.gather(*tasks)
        finally:
            self._input_sent_event.set()
            if self._sent_tokenizer_stream:
                await self._sent_tokenizer_stream.aclose()
            await utils.aio.gracefully_cancel(*tasks)
            if self._task_id:
                await self._tts._close_ws(self._task_id)

    async def _sentence_stream_task(self, output_emitter: tts.AudioEmitter) -> None:
        """处理句子流任务"""
        # 发送run-task指令
        run_task_cmd = {
            "header": {
                "action": "run-task",
                "task_id": self._task_id,
                "streaming": "duplex"
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self._opts.model,
                "parameters": {
                    "text_type": self._opts.text_type,
                    "voice": self._opts.voice,
                    "format": self._opts.format,
                    "sample_rate": self._opts.sample_rate,
                    "volume": self._opts.volume,
                    "rate": self._opts.rate,
                    "pitch": self._opts.pitch,
                    "enable_ssml": self._opts.enable_ssml
                },
                "input": {}
            }
        }
        
        await self._ws.send(json.dumps(run_task_cmd))
        
        # 等待任务启动
        await self._input_sent_event.wait()
        
        # 处理分词器流
        async for ev in self._sent_tokenizer_stream:
            continue_task_cmd = {
                "header": {
                    "action": "continue-task",
                    "task_id": self._task_id,
                    "streaming": "duplex"
                },
                "payload": {
                    "input": {
                        "text": ev.token
                    }
                }
            }
            
            self._sent_tokens.append(ev.token)
            await self._ws.send(json.dumps(continue_task_cmd))
        
        # 发送finish-task指令
        finish_task_cmd = {
            "header": {
                "action": "finish-task",
                "task_id": self._task_id,
                "streaming": "duplex"
            },
            "payload": {
                "input": {}
            }
        }
        
        await self._ws.send(json.dumps(finish_task_cmd))
        self._input_sent_event.set()

    async def _input_task(self) -> None:
        """处理输入任务"""
        async for data in self._input_ch:
            if isinstance(data, self._FlushSentinel):
                if self._sent_tokenizer_stream:
                    self._sent_tokenizer_stream.flush()
                continue
            
            if self._sent_tokenizer_stream:
                self._sent_tokenizer_stream.push_text(data)
        
        if self._sent_tokenizer_stream:
            self._sent_tokenizer_stream.end_input()

    async def _recv_task(self, output_emitter: tts.AudioEmitter) -> None:
        """接收任务"""
        segment_id = self._task_id
        output_emitter.start_segment(segment_id=segment_id)
        
        while not self._task_finished:
            try:
                message = await asyncio.wait_for(self._ws.recv(), timeout=self._conn_options.timeout)
                
                if isinstance(message, str):
                    msg_json = json.loads(message)
                    
                    if "header" in msg_json:
                        header = msg_json["header"]
                        
                        if "event" in header:
                            event = header["event"]
                            
                            if event == "task-started":
                                logger.debug("阿里TTS任务已启动")
                                self._task_started = True
                                self._input_sent_event.set()
                                
                            elif event == "task-finished":
                                logger.debug("阿里TTS任务已完成")
                                self._task_finished = True
                                output_emitter.end_input()
                                break
                                
                            elif event == "task-failed":
                                error_msg = msg_json.get("error_message", "未知错误")
                                logger.error(f"阿里TTS任务失败: {error_msg}")
                                self._task_finished = True
                                output_emitter.end_input()
                                raise APIError(f"阿里语音合成失败: {error_msg}")
                else:
                    # 二进制消息（音频数据）
                    output_emitter.push(message)
                    
            except asyncio.TimeoutError:
                raise APITimeoutError() from None
            except websockets.exceptions.ConnectionClosed as e:
                raise APIConnectionError() from e
            except Exception as e:
                if not isinstance(e, (APITimeoutError, APIError)):
                    raise APIConnectionError() from e
                else:
                    raise