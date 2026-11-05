"""Participant-bound startup, early hellos and idempotent handshake retries."""
# ruff: noqa: F811 -- pytest injects imported fixtures

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_memory_foundation import account, foundation, session  # noqa: F401

from avatarweave_agent.memory.action_protocol import BOOTSTRAP_TOPIC, HELLO_TOPIC, ClientHello
from avatarweave_agent.memory.actions import PrivateActions
from avatarweave_agent.memory.rtc_session import start_private_session
from avatarweave_agent.memory.storage import MemorySession


@pytest.mark.asyncio
async def test_room_connects_before_bound_outputs_and_hello_waits_for_voice_ready(foundation):
    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    async with service.database.sessions() as db:
        saved = await db.get(MemorySession, view.session_id)
    conversation = SimpleNamespace(
        service=service,
        scope=scope,
        session=saved,
        owner_epoch=owner,
        generation_id=None,
        cancel=AsyncMock(),
    )
    packets, order = [], []

    class Room:
        connected = False
        handler = None

        @property
        def local_participant(self):
            if not self.connected:
                raise RuntimeError("Room is not connected")
            return self

        async def publish_data(self, payload, **options):
            packets.append((options["topic"], json.loads(payload)))

        def on(self, name, handler):
            self.handler = handler

        def off(self, *args):
            self.handler = None

    room = Room()

    async def connect():
        room.connected = True
        order.append("connected")
        room.handler(
            SimpleNamespace(
                topic=HELLO_TOPIC,
                participant=SimpleNamespace(identity=saved.participant),
                data=ClientHello(version=1, session_id=saved.id, nonce="early-client")
                .model_dump_json()
                .encode(),
            )
        )
        await asyncio.sleep(0)

    async def start(**options):
        assert options["room"].local_participant is room
        assert options["room_options"].participant_identity == saved.participant
        assert options["room_options"].close_on_disconnect is False
        assert options["record"] == {
            "audio": False,
            "traces": False,
            "logs": False,
            "transcript": False,
            "input_delta": False,
            "redaction": True,
        }
        await asyncio.sleep(0.01)
        assert not packets  # RTC join is not enough to accept a conversation.
        order.append("voice_ready")

    ctx = SimpleNamespace(room=room, connect=connect)
    greeted = []
    bridge = PrivateActions(
        ctx,
        conversation,
        None,
        ready=False,
        on_connection_ready=lambda: greeted.append(len(packets)),
    )
    assistant = SimpleNamespace(private=conversation, private_actions=bridge)
    try:
        await start_private_session(ctx, SimpleNamespace(start=start), assistant)
        await asyncio.wait_for(bridge.queue.join(), 2)
        assert order == ["connected", "voice_ready"]
        assert packets[0][0] == BOOTSTRAP_TOPIC
        assert packets[0][1]["nonce"] == "early-client"
        assert greeted == [1]  # Only after the acknowledgement has been sent.
        bridge.sequence = 5
        bridge.plans["in-flight"] = (
            SimpleNamespace(expires_at=service.clock() + timedelta(seconds=30)),
            None,
        )
        await bridge.bootstrap(ClientHello(version=1, session_id=saved.id, nonce="early-client"))
        assert packets[-1][1]["sequence_floor"] == 0
        assert "in-flight" in bridge.plans
        assert greeted == [1]  # Retry of the same handshake never repeats greeting.
        await bridge.bootstrap(ClientHello(version=1, session_id=saved.id, nonce="reloaded-client"))
        assert greeted == [1, len(packets)]
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_failed_private_start_does_not_acknowledge_readiness():
    bridge = SimpleNamespace(mark_ready=Mock())
    assistant = SimpleNamespace(
        private=SimpleNamespace(session=SimpleNamespace(participant="bound-user")),
        private_actions=bridge,
    )
    ctx = SimpleNamespace(connect=AsyncMock(), room=object())
    voice = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("startup failed")))
    with pytest.raises(RuntimeError, match="startup failed"):
        await start_private_session(ctx, voice, assistant)
    ctx.connect.assert_awaited_once()
    bridge.mark_ready.assert_not_called()


@pytest.mark.asyncio
async def test_absent_participant_has_a_bounded_reconnect_grace_and_foreign_peers_do_not_keep_it_alive():
    from avatarweave_agent.memory.rtc_session import maintain_private_owner

    elapsed = 0
    room = SimpleNamespace(remote_participants={})
    ctx = SimpleNamespace(room=room, job=SimpleNamespace(id="job"), shutdown=Mock())
    voice = SimpleNamespace(interrupt=Mock())
    private = SimpleNamespace(
        session=SimpleNamespace(id="session", participant="bound"),
        service=SimpleNamespace(renew_owner=AsyncMock()),
        scope=object(),
        owner_epoch=1,
        cancel=AsyncMock(),
    )

    async def tick(seconds):
        nonlocal elapsed
        elapsed += seconds
        # Returning during the grace period resets the timer.
        name = "bound" if elapsed == 30 else "foreign"
        room.remote_participants = {name: SimpleNamespace(identity=name)}

    await maintain_private_owner(ctx, voice, private, sleep=tick, clock=lambda: elapsed)
    assert elapsed == 100
    ctx.shutdown.assert_called_once_with(reason="private_participant_timeout")
    private.cancel.assert_awaited_once()
    voice.interrupt.assert_called_once()


@pytest.mark.asyncio
async def test_failed_bootstrap_send_can_retry_without_losing_or_repeating_greeting(foundation):
    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    owner = await service.claim_owner(scope, view.session_id, view.room_name, "worker")
    async with service.database.sessions() as db:
        saved = await db.get(MemorySession, view.session_id)
    room = SimpleNamespace(
        on=Mock(),
        off=Mock(),
        local_participant=SimpleNamespace(
            publish_data=AsyncMock(side_effect=[RuntimeError("transient send failure"), None, None])
        ),
    )
    callback = Mock()
    bridge = PrivateActions(
        SimpleNamespace(room=room),
        SimpleNamespace(
            service=service, scope=scope, session=saved, owner_epoch=owner, generation_id=None
        ),
        None,
        on_connection_ready=callback,
    )
    hello = ClientHello(version=1, session_id=saved.id, nonce="same-client")
    try:
        with pytest.raises(RuntimeError, match="transient"):
            await bridge.bootstrap(hello)
        callback.assert_not_called()
        await bridge.bootstrap(hello)
        await bridge.bootstrap(hello)
        callback.assert_called_once()
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_resumed_owner_greets_with_empty_history_and_no_tools():
    from avatarweave_agent.memory.rtc_session import PrivateConnectionGreeting

    handle = asyncio.get_running_loop().create_future()
    handle.set_result(None)
    voice = SimpleNamespace(interrupt=AsyncMock(), generate_reply=Mock(return_value=handle))
    private = SimpleNamespace(
        owner_epoch=3,
        generation_id="old-generation",
        fence=object(),
        current_event=object(),
        current_context=object(),
        _turn_lock=asyncio.Lock(),
    )
    greeting = PrivateConnectionGreeting(voice, private)
    try:
        greeting.connected()
        await greeting.task
        voice.interrupt.assert_awaited_once_with(force=True)
        options = voice.generate_reply.call_args.kwargs
        assert list(options["chat_ctx"].items) == [] and options["tools"] == []
        assert private.generation_id is None and private.current_context is None
        await greeting.close()
        greeting.connected()
        voice.generate_reply.assert_called_once()
    finally:
        await greeting.close()


@pytest.mark.asyncio
async def test_user_turn_wins_over_a_delayed_connection_greeting():
    from avatarweave_agent.memory.rtc_session import PrivateConnectionGreeting

    private = SimpleNamespace(
        generation_id="old",
        fence=object(),
        current_event=object(),
        current_context=object(),
        _turn_lock=asyncio.Lock(),
    )

    async def interrupt(**kwargs):
        private.generation_id, private.current_event = "new-user-turn", object()

    voice = SimpleNamespace(interrupt=interrupt, generate_reply=Mock())
    greeting = PrivateConnectionGreeting(voice, private)
    try:
        greeting.connected()
        await greeting.task
        voice.generate_reply.assert_not_called()
        assert private.generation_id == "new-user-turn"
    finally:
        await greeting.close()


@pytest.mark.asyncio
async def test_delayed_invalidation_check_cannot_interrupt_a_new_greeting():
    from avatarweave_agent.memory.execution import MemoryFault
    from avatarweave_agent.memory.rtc_session import watch_private_generation

    private = SimpleNamespace(generation_id="old", fence=object())

    async def check(generation, fence):
        assert generation == "old"
        private.generation_id, private.fence = "new-greeting", object()
        raise MemoryFault("stale_generation", "Old generation ended")

    private.check = check
    voice, actions = SimpleNamespace(interrupt=Mock()), SimpleNamespace(cancel=AsyncMock())
    ticks = 0

    async def tick(seconds):
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await watch_private_generation(voice, private, actions, sleep=tick)
    voice.interrupt.assert_not_called()
    actions.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_greetings_have_distinct_turns_and_do_not_replay_private_history(
    foundation, monkeypatch
):
    from livekit.agents import Agent, llm
    from livekit.agents.voice.agent_activity import _SpeechHandleContextVar

    from avatarweave_agent.memory.contracts import PersonaCard, RecentMessage
    from avatarweave_agent.memory.conversation import PrivateConversation
    from avatarweave_agent.memory.voice import MemoryAgentMixin

    service = foundation
    scope = await account(service)
    view, _ = await session(service, scope)
    private = await PrivateConversation.bind(
        service,
        view.session_id,
        view.room_name,
        "worker",
        persona=PersonaCard(
            persona_id="companion",
            version=1,
            authored_identity="中文助手",
            behavior_boundaries=[],
            preferred_style=[],
            lore_refs=[],
            approved_at=service.clock(),
        ),
    )
    private.recent_replies = [
        RecentMessage(
            message_id="private-history",
            role="assistant",
            text="合成私事不应该进入问候",
            output_evidence="generated",
            source_event_id="synthetic-user-event",
        )
    ]
    prompts = []

    async def provider(subject, chat_ctx, tools, model_settings):
        prompts.append("\n".join(getattr(item, "text_content", "") for item in chat_ctx.items))
        yield "你好"

    monkeypatch.setattr(Agent.default, "llm_node", provider)

    class Subject(MemoryAgentMixin):
        async def memory_notice(self, *args, **kwargs):
            raise AssertionError("Greeting unexpectedly failed")

    subject = Subject()
    subject.private, subject.private_actions = private, None
    for identifier in ("first-speech", "resumed-speech"):
        token = _SpeechHandleContextVar.set(
            SimpleNamespace(
                id=identifier, interrupted=False, input_details=SimpleNamespace(modality="text")
            )
        )
        try:
            assert [part async for part in subject.llm_node(llm.ChatContext(), [], None)] == [
                "你好"
            ]
            assert private.current_event.event_id == "welcome:" + identifier
        finally:
            _SpeechHandleContextVar.reset(token)
    assert len(prompts) == 2 and all("合成私事" not in prompt for prompt in prompts)
