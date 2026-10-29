"""Official AgentServer/AgentSession pipeline plus a credential-free transport demo."""

import asyncio
import logging
import os
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from livekit.agents import (
    Agent,
    AgentServer,
    JobContext,
    RunContext,
    cli,
    function_tool,
    llm,
)

from .avatars import get_avatar
from .config import settings
from .db import get_messages, init_db, save_message
from .pipeline import create_session, load_pipeline, prewarm
from .protocol import COMMAND_TOPIC, make_command
from .session_options import read_options
from .startup_tracing import install_startup_tracing
from .voices import voice_service

logger = logging.getLogger("avatarweave.agent")
# The CLI resolves these variables before creating JobContext.
os.environ.setdefault("LIVEKIT_URL", settings.livekit_url)
os.environ.setdefault("LIVEKIT_API_KEY", settings.livekit_api_key)
os.environ.setdefault("LIVEKIT_API_SECRET", settings.livekit_api_secret)
http_proxy = (
    settings.agent_http_proxy
    or os.getenv("HTTPS_PROXY")
    or os.getenv("https_proxy")
    or os.getenv("HTTP_PROXY")
    or os.getenv("http_proxy")
)
server = AgentServer(
    http_proxy=http_proxy,
    num_idle_processes=1,
    port=8091,
    log_level="INFO",
    initialize_process_timeout=60,
    drain_timeout=10,
)
server.setup_fnc = prewarm


async def publish_action(ctx: JobContext, avatar, action_id: str | None = None):
    action_id = action_id or avatar.default_action
    if action_id not in avatar.actions:
        return "该数字人没有配置这个动作。"
    command = make_command(avatar.actions[action_id].command())
    await ctx.room.local_participant.publish_data(
        command.model_dump_json(exclude_none=True).encode(),
        reliable=True,
        topic=COMMAND_TOPIC,
    )
    return "已播放动作。"


class Assistant(Agent):
    def __init__(self, ctx: JobContext, chat_ctx: llm.ChatContext, avatar):
        actions = (
            ", ".join(f"{key}: {value.label}" for key, value in avatar.actions.items())
            or "无"
        )
        super().__init__(
            instructions=settings.agent_instructions + "\n可用数字人动作：" + actions,
            chat_ctx=chat_ctx,
        )
        self.avatar = avatar
        self.ctx = ctx

    @function_tool
    async def animate(self, context: RunContext, action: str | None = None):
        """播放已配置的数字人动作。action 使用可用动作列表中的标识，留空播放默认动作。"""
        return await publish_action(self.ctx, self.avatar, action)


async def run_mock(ctx: JobContext):
    """Real LiveKit room and data streams; deterministic text, no paid model or fake audio."""
    _, avatar = get_avatar(read_options(ctx.job.metadata).avatar_id)
    done = asyncio.Event()
    tasks: set[asyncio.Task] = set()

    async def respond(reader, participant_identity):
        if participant_identity == ctx.room.local_participant.identity:
            return
        text = await reader.read_all()
        await save_message(ctx.room.name, reader.info.stream_id, "user", text)
        await ctx.room.local_participant.set_attributes({"lk.agent.state": "thinking"})
        reply = (
            "这是本地演示模式，已收到你的消息："
            + text[:500]
            + "。配置模型服务后可启用真实语音对话。"
        )
        if any(word in text for word in ("动作", "表演", "跳舞")):
            await publish_action(ctx, avatar)
        writer = await ctx.room.local_participant.stream_text(
            topic="lk.transcription", attributes={"lk.segment_id": uuid4().hex}
        )
        for i in range(0, len(reply), 8):
            await writer.write(reply[i : i + 8])
            await asyncio.sleep(0.025)
        await writer.aclose()
        await save_message(ctx.room.name, uuid4().hex, "assistant", reply)
        await ctx.room.local_participant.set_attributes({"lk.agent.state": "listening"})

    def on_text(reader, identity):
        task = asyncio.create_task(respond(reader, identity))
        tasks.add(task)

        def complete(t):
            tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error("Mock response failed", exc_info=t.exception())

        task.add_done_callback(complete)

    ctx.room.register_text_stream_handler("lk.chat", on_text)
    await ctx.connect()
    await ctx.room.local_participant.set_attributes({"lk.agent.state": "listening"})

    async def shutdown():
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        done.set()

    ctx.add_shutdown_callback(shutdown)
    # JobContext keeps the job alive after entrypoint returns.


@server.rtc_session(agent_name=settings.agent_name)
async def entrypoint(ctx: JobContext):
    started = previous = perf_counter()
    previous_stage = None

    def log_startup(stage: str):
        nonlocal previous, previous_stage
        now = perf_counter()
        logger.info(
            "connection_startup",
            extra={
                "stage": stage,
                "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "job_id": ctx.job.id,
                "room": ctx.room.name,
                "elapsed_ms": round((now - started) * 1000, 1),
                "since_previous_ms": round((now - previous) * 1000, 1),
                "previous_stage": previous_stage,
            },
        )
        previous = now
        previous_stage = stage

    # The SDK logs "received job request" in the parent worker before dispatch.
    # This timestamp measures entry into the assigned job process separately.
    log_startup("job_entrypoint")
    await init_db()
    config = load_pipeline()
    logger.info("pipeline_configuration", extra=config.summary())
    if config.mode == "mock":
        log_startup("mock_config_loaded")
        await run_mock(ctx)
        log_startup("mock_ready")
        return
    startup_tracing = install_startup_tracing(ctx.job.id, ctx.room.name)

    async def stop_startup_tracing():
        if startup_tracing is not None:
            startup_tracing.active = False

    ctx.add_shutdown_callback(stop_startup_tracing)
    chat_ctx = llm.ChatContext()
    for item in await get_messages(ctx.room.name, limit=settings.agent_history_limit):
        chat_ctx.add_message(role=item["role"], content=item["text"])
    choices = read_options(ctx.job.metadata).validate_choices(config)
    _, avatar = get_avatar(choices.avatar_id)
    if choices.tts_voice:
        config = config.model_copy(deep=True)
        config.tts, _ = voice_service(config).select(choices.tts_voice)
    logger.info(
        "session_choices",
        extra={
            "avatar_id": choices.avatar_id,
            "tts_provider": config.tts.provider,
            "tts_voice": choices.tts_voice,
        },
    )
    log_startup("config_history_loaded")
    session = create_session(ctx, config)
    log_startup("session_created")
    tasks: set[asyncio.Task] = set()

    @session.on("metrics_collected")
    def on_metrics(event):
        metric = event.metrics
        timing = {
            key: getattr(metric, key)
            for key in (
                "ttft",
                "ttfb",
                "duration",
                "end_of_utterance_delay",
                "transcription_delay",
            )
            if isinstance(getattr(metric, key, None), (int, float))
        }
        logger.info(
            "pipeline_latency",
            extra={"metric_type": metric.type, "timing_seconds": timing},
        )

    @session.on("conversation_item_added")
    def on_item(event):
        item = event.item
        if getattr(item, "type", None) != "message":
            return
        if item.role not in {"user", "assistant"} or not item.text_content:
            return
        task = asyncio.create_task(
            save_message(ctx.room.name, item.id, item.role, item.text_content)
        )
        tasks.add(task)

        def complete(t):
            tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error("Message persistence failed", exc_info=t.exception())

        task.add_done_callback(complete)

    async def flush():
        await asyncio.gather(*tasks, return_exceptions=True)

    ctx.add_shutdown_callback(flush)
    await session.start(room=ctx.room, agent=Assistant(ctx, chat_ctx, avatar))
    log_startup("session_started")
    await ctx.connect()
    log_startup("room_connected")
    await session.generate_reply(instructions="用中文简短问候用户。")


if __name__ == "__main__":
    cli.run_app(server)
