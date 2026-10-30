"""Replay a practice partner's actual words through the configured TTS."""

from uuid import uuid4

from livekit.agents import room_io

from .db import save_message
from .practice import HELP


def practice_text_input(room_name):
    default_input = room_io.TextInputOptions().text_input_cb

    async def on_text(session, event):
        if event.text != HELP["replay"]:
            return await default_input(session, event)
        await session.interrupt()
        previous = next(
            (
                item.text_content
                for item in reversed(session.history.items)
                if getattr(item, "role", None) == "assistant"
                and getattr(item, "text_content", None)
            ),
            None,
        )
        if not previous:
            return await default_input(session, event)
        await save_message(
            room_name,
            event.info.stream_id if event.info else uuid4().hex,
            "user",
            event.text,
        )
        # Re-synthesize the actual reply, without another model turn or score evidence.
        # RoomIO still synchronizes this speech with its transcript as usual.
        await session.say(previous, add_to_chat_ctx=False)

    return on_text
