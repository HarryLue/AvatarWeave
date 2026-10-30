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
    room_io,
)

from .avatars import get_avatar
from .config import settings
from .db import Practice, Session, get_messages, init_db, save_message
from .pipeline import create_session, load_pipeline, prewarm
from .practice import (
    GOALS,
    HELP,
    INSTRUCTIONS,
    aware,
    complete_goal,
    end_practice,
    join_practice,
    now,
)
from .practice_voice import practice_text_input
from .protocol import COMMAND_TOPIC, make_command
from .session_options import read_metadata, read_options
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
    port=settings.agent_health_port,
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
    def __init__(self, ctx: JobContext, chat_ctx: llm.ChatContext, avatar, instructions=None):
        actions = (
            ", ".join(f"{key}: {value.label}" for key, value in avatar.actions.items()) or "无"
        )
        super().__init__(
            instructions=(instructions or settings.agent_instructions)
            + "\n可用数字人动作："
            + actions,
            chat_ctx=chat_ctx,
        )
        self.avatar = avatar
        self.ctx = ctx

    @function_tool
    async def animate(self, context: RunContext, action: str | None = None):
        """播放已配置的数字人动作。action 使用可用动作列表中的标识，留空播放默认动作。"""
        return await publish_action(self.ctx, self.avatar, action)


class PracticeAssistant(Assistant):
    def __init__(self, ctx, chat_ctx, avatar, practice_id, flush):
        super().__init__(ctx, chat_ctx, avatar, INSTRUCTIONS)
        self.practice_id = practice_id
        self.flush = flush

    @function_tool
    async def complete_goal(self, context: RunContext, goal_id: str, evidence: str):
        """Mark one completed hotel task using exact learner evidence.

        goal_id is reservation, stay, or information, in that order.
        evidence must quote the learner's English, not your own reply.
        """
        await self.flush()
        return await complete_goal(self.practice_id, goal_id, evidence)


def watch_practice(ctx, practice_id, stop):
    """The worker enforces the deadline and acknowledges persisted final messages."""

    async def monitor():
        try:
            while True:
                async with Session() as db:
                    item = await db.get(Practice, practice_id)
                if not item or item.status != "active" or aware(item.expires_at) <= now():
                    await stop()
                    await end_practice(practice_id)
                    await ctx.room.disconnect()
                    return
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Practice watchdog failed")
            # Fail closed: a database outage must not leave a paid session running.
            await stop()
            await ctx.room.disconnect()

    task = asyncio.create_task(monitor())

    async def shutdown():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stop()
        await end_practice(practice_id)

    ctx.add_shutdown_callback(shutdown)


async def run_mock(ctx: JobContext, practice_id=None):
    """Real LiveKit room and data streams; deterministic text, no paid model or fake audio."""
    _, avatar = get_avatar(read_options(ctx.job.metadata).avatar_id)
    done = asyncio.Event()
    tasks: set[asyncio.Task] = set()

    async def respond(reader, participant_identity):
        if participant_identity == ctx.room.local_participant.identity:
            return
        text = await reader.read_all()
        if len(text) > 4000:
            return
        await save_message(ctx.room.name, reader.info.stream_id, "user", text)
        await ctx.room.local_participant.set_attributes({"lk.agent.state": "thinking"})
        reply = (
            "这是本地演示模式，已收到你的消息："
            + text[:500]
            + "。配置模型服务后可启用真实语音对话。"
        )
        if practice_id:
            async with Session() as db:
                item = await db.get(Practice, practice_id)
            if not item or item.status != "active" or aware(item.expires_at) <= now():
                return
            if text == HELP["meaning"]:
                reply = "我在问你是否要办理入住。Are you checking in?"
            elif text in (HELP["hint"], HELP["hint_full"]):
                import json

                index = min(len(json.loads(item.goals_json)), 2)
                reply = (
                    "你可以试着说："
                    + (
                        GOALS[index]["hint"]
                        if text == HELP["hint_full"]
                        else ["I'd like to…", "I'm staying for…", "What time is…"][index]
                    )
                    + " 现在试着自己说一遍。"
                )
            elif text == HELP["replay"]:
                history = await get_messages(ctx.room.name)
                reply = next(
                    (
                        message["text"]
                        for message in reversed(history)
                        if message["role"] == "assistant"
                    ),
                    "Welcome to Willow Hotel! Are you checking in?",
                )
            else:
                reply = "Welcome to Willow Hotel! This is a text demo. Try: I'd like to check in. My reservation is under Jamie Chen."
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
        if not practice_id:
            for task in list(tasks):
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        done.set()

    ctx.add_shutdown_callback(shutdown)
    if practice_id:
        watch_practice(ctx, practice_id, shutdown)
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
    practice_id = read_metadata(ctx.job.metadata).context.get("practice_id")
    if practice_id is not None:
        if not isinstance(practice_id, str):
            raise ValueError("Invalid practice id")
        await join_practice(practice_id, ctx.room.name)
        lifecycle = {"watching": False}

        async def failed_startup():
            if not lifecycle["watching"]:
                await end_practice(practice_id)

        ctx.add_shutdown_callback(failed_startup)
    config = load_pipeline()
    logger.info("pipeline_configuration", extra=config.summary())
    if config.mode == "mock":
        log_startup("mock_config_loaded")
        await run_mock(ctx, practice_id)
        if practice_id:
            lifecycle["watching"] = True
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
    if practice_id:
        config = config.model_copy(deep=True)
        # The default ASR profile may force Chinese. Use English for this product.
        if config.stt.provider in {"openai", "cartesia"}:
            config.stt.options["language"] = "en"
        elif config.stt.provider == "huoshan":
            config.stt.options["language"] = "en-US"
        config.session.min_endpointing_delay = max(config.session.min_endpointing_delay, 0.8)
        config.session.max_endpointing_delay = max(config.session.max_endpointing_delay, 2.5)
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

    @session.on("error")
    def on_service_error(event):
        if not practice_id:
            return
        if getattr(event.error, "recoverable", False):
            return  # Let SDK retries finish before showing a failure to the learner.
        kind = getattr(event.error, "type", "")
        messages = {
            "tts_error": "语音服务暂时不可用，本轮回复无法播放，请稍后重试。",
            "llm_error": "伙伴暂时无法生成回复，请稍后重试；未收到回复的练习不扣次数。",
            "stt_error": "暂时无法识别语音，你可以先用文字输入，稍后再开启麦克风。",
        }
        if kind not in messages:
            return
        # Send only user-facing messages; provider exceptions can contain sensitive details.
        task = asyncio.create_task(
            ctx.room.local_participant.set_attributes({"avatarweave.service_error": messages[kind]})
        )
        tasks.add(task)

        def complete(t):
            tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.warning("Could not publish service status", exc_info=t.exception())

        task.add_done_callback(complete)

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
    assistant = (
        PracticeAssistant(ctx, chat_ctx, avatar, practice_id, flush)
        if practice_id
        else Assistant(ctx, chat_ctx, avatar)
    )
    await session.start(
        room=ctx.room,
        agent=assistant,
        room_options=room_io.RoomOptions(
            text_output=room_io.TextOutputOptions(sync_transcription=True),
            text_input=room_io.TextInputOptions(text_input_cb=practice_text_input(ctx.room.name)),
        )
        if practice_id
        else room_io.RoomOptions(),
    )
    log_startup("session_started")
    await ctx.connect()
    log_startup("room_connected")
    if practice_id:

        async def stop_practice():
            await session.aclose()
            await flush()

        watch_practice(ctx, practice_id, stop_practice)
        lifecycle["watching"] = True
    await session.generate_reply(
        instructions=(
            "Say exactly: Welcome to Willow Hotel! Are you checking in?"
            if practice_id
            else "用中文简短问候用户。"
        )
    )


if __name__ == "__main__":
    cli.run_app(server)
