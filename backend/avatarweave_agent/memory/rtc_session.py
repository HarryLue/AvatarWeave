"""Start participant-bound RoomIO only after its room is connected."""

import asyncio
import logging
from time import monotonic

from livekit.agents import llm
from livekit.agents.voice.room_io import RoomOptions

logger = logging.getLogger(__name__)


class PrivateConnectionGreeting:
    """Greet a newly acknowledged connection without replaying private history."""

    def __init__(self, session, private):
        self.session, self.private = session, private
        self.task = None
        self.closed = False

    def connected(self):
        if self.closed:
            return
        if self.task:
            self.task.cancel()
        previous = (self.private.generation_id, self.private.current_event)
        self.task = asyncio.create_task(self._greet(previous))

    async def _greet(self, previous):
        private = self.private
        try:
            if (private.generation_id, private.current_event) != previous:
                return  # A user turn already started; do not interrupt it to greet.
            await self.session.interrupt(force=True)
            async with private._turn_lock:
                if (private.generation_id, private.current_event) != previous:
                    return
                private.current_context = None
                private.generation_id, private.fence = None, None
            handle = self.session.generate_reply(
                chat_ctx=llm.ChatContext(),
                instructions="用中文简短问候用户，不主动提起私人历史。",
                tools=[],
            )
            await handle
            if error := handle.exception():
                logger.warning(
                    "private_greeting_failed", extra={"exception_type": type(error).__name__}
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- report type only, never private/provider content
            logger.warning(
                "private_greeting_failed", extra={"exception_type": type(error).__name__}
            )

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def watch_private_generation(session, private, actions, *, sleep=asyncio.sleep):
    last_invalid = None
    while True:
        await sleep(0.1)
        generation, fence = private.generation_id, private.fence
        if not generation:
            continue
        try:
            await private.check(generation, fence)
        except Exception:  # noqa: BLE001 -- cancellation remains fail-closed
            if private.generation_id != generation or last_invalid == generation:
                continue
            last_invalid = generation
            session.interrupt()
            await actions.cancel()


async def start_private_session(ctx, session, assistant):
    # Explicit participant binding initializes the transcription output eagerly;
    # LiveKit reads room.local_participant here, which requires a connected room.
    await ctx.connect()
    await session.start(
        room=ctx.room,
        agent=assistant,
        room_options=RoomOptions(
            participant_identity=assistant.private.session.participant, close_on_disconnect=False
        ),
        record={
            "audio": False,
            "traces": False,
            "logs": False,
            "transcript": False,
            "input_delta": False,
            "redaction": True,
        },
    )
    assistant.private_actions.mark_ready()


async def maintain_private_owner(ctx, session, private, *, sleep=asyncio.sleep, clock=monotonic):
    absent_since = None
    reason = "private_participant_timeout"
    try:
        while True:
            await sleep(10)
            present = any(
                p.identity == private.session.participant
                for p in ctx.room.remote_participants.values()
            )
            if present:
                absent_since = None
            elif absent_since is None:
                absent_since = clock()
            elif clock() - absent_since >= 60:
                break
            await private.service.renew_owner(
                private.scope, private.session.id, ctx.job.id, private.owner_epoch
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- shutdown on every failed ownership renewal
        reason = "private_owner_unavailable"
    try:
        await private.cancel()
    finally:
        try:
            session.interrupt()
        finally:
            ctx.shutdown(reason=reason)
