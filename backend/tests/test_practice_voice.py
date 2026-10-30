from types import SimpleNamespace
from unittest.mock import AsyncMock

from avatarweave_agent import practice_voice
from avatarweave_agent.practice import HELP


def test_replay_uses_actual_assistant_text_without_another_model_turn(monkeypatch):
    import asyncio

    saved = AsyncMock()
    monkeypatch.setattr(practice_voice, "save_message", saved)
    session = SimpleNamespace(
        interrupt=AsyncMock(),
        say=AsyncMock(),
        history=SimpleNamespace(
            items=[
                SimpleNamespace(role="assistant", text_content="Welcome!"),
                SimpleNamespace(role="assistant", text_content="How many nights?"),
                SimpleNamespace(role="user", text_content=HELP["replay"]),
            ]
        ),
    )
    event = SimpleNamespace(text=HELP["replay"], info=SimpleNamespace(stream_id="help-1"))
    asyncio.run(practice_voice.practice_text_input("practice-test")(session, event))
    session.say.assert_awaited_once_with("How many nights?", add_to_chat_ctx=False)
    saved.assert_awaited_once_with("practice-test", "help-1", "user", HELP["replay"])


def test_other_inputs_keep_the_default_conversation_flow(monkeypatch):
    import asyncio

    default = AsyncMock()
    monkeypatch.setattr(
        practice_voice.room_io,
        "TextInputOptions",
        lambda: SimpleNamespace(text_input_cb=default),
    )
    session = SimpleNamespace(say=AsyncMock())
    event = SimpleNamespace(text=HELP["hint"])
    asyncio.run(practice_voice.practice_text_input("practice-test")(session, event))
    default.assert_awaited_once_with(session, event)
    session.say.assert_not_called()
