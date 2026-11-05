"""Opt-in real extraction/embedding/voice-hook smoke against the loopback test database.

Uses synthetic data. Never connects to MEMORY_DATABASE_URL or production rooms.
It captures actual TTS frames; it does not assert browser/device playback or SLOs.
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend/tests"))

from avatarweave_agent.memory.config import MemorySettings
from avatarweave_agent.memory.contracts import PersonaCard
from avatarweave_agent.memory.conversation import PrivateConversation
from avatarweave_agent.memory.embedding import EmbeddingGateway
from avatarweave_agent.memory.facts import load_record
from avatarweave_agent.memory.gateway import ModelGateway
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.service import MemoryService
from avatarweave_agent.memory.storage import MemoryBase, MemoryDatabase, MemorySession
from avatarweave_agent.memory.tables import FactVersion, Job, OutputLedger
from avatarweave_agent.memory.voice import MemoryAgentMixin
from avatarweave_agent.pipeline import build_provider, load_pipeline
from livekit.agents import Agent, AgentSession, utils
from livekit.agents.voice.io import AudioOutput, AudioOutputCapabilities
from sqlalchemy import select
from test_memory_foundation import account, event_request, session


class Capture(AudioOutput):
    def __init__(self):
        super().__init__(
            label="synthetic-test-capture",
            capabilities=AudioOutputCapabilities(pause=False),
        )
        self.samples = 0
        self.first = asyncio.Event()
        self.first_at = None

    async def capture_frame(self, frame):
        await super().capture_frame(frame)
        self.samples += frame.samples_per_channel
        if self.first_at is None:
            self.first_at = monotonic()
        self.first.set()

    def flush(self):
        super().flush()
        # This sink captures frames and has no device playback. Report zero played.
        self.on_playback_finished(playback_position=0, interrupted=True)

    def clear_buffer(self):
        self.on_playback_finished(playback_position=0, interrupted=True)


class Companion(MemoryAgentMixin, Agent):
    def __init__(self, private):
        super().__init__(instructions=private.persona.authored_identity)
        self.private, self.private_actions, self.notices = private, None, []

    async def memory_notice(self, state, *, source_saved=False, code=None):
        self.notices.append((state, source_saved, code))


async def main():
    url = os.environ.get("MEMORY_SMOKE_DATABASE_URL", "")
    parsed = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    if (
        parsed.hostname != "127.0.0.1"
        or parsed.port != 55436
        or parsed.path != "/avatar_memory_test"
    ):
        raise SystemExit(
            "Set MEMORY_SMOKE_DATABASE_URL to the isolated 127.0.0.1:55436/avatar_memory_test container only"
        )
    database = MemoryDatabase(url)
    gateway = ModelGateway.from_pipeline()
    embedding = EmbeddingGateway.from_settings(MemorySettings())
    if gateway is None or embedding is None:
        await database.close()
        raise SystemExit("Configure the real extraction and embedding profiles first")
    voice_session = None
    try:
        await database.create_schema()
        service = MemoryService(database, companions={"companion"})
        subject = "synthetic-provider-smoke-" + uuid4().hex
        scope = await account(service, subject)
        source = await event_request(service, scope)
        async with database.sessions() as db:
            source_event_room = (
                await db.get(MemorySession, source.session_id)
            ).room
        source = source.model_copy(
            update={
                "event": source.event.model_copy(
                    update={"content": "请记住，我叫陆星，我喜欢喝茉莉花茶。"}
                )
            }
        )
        assert (await service.input_event(source)).data.persisted
        await service.next_job("provider-smoke", gateway, embedding)
        async with database.sessions() as db:
            jobs = list(await db.scalars(select(Job)))
            assert jobs and all(job.state == "done" for job in jobs), [
                (j.state, j.attempt, j.last_error_json) for j in jobs
            ]
            records = list(
                await db.scalars(
                    select(FactVersion).where(FactVersion.status == "active")
                )
            )
            assert len(records) >= 2
        event_refs = []
        for text in ("下午有个会议", "下午3.00的会议"):
            owner = await service.claim_owner(
                scope, source.session_id, source_event_room, "worker"
            )
            await service.renew_owner(scope, source.session_id, "worker", owner)
            event = source.event.model_copy(
                update={
                    "event_id": uuid4().hex,
                    "turn_id": uuid4().hex,
                    "content": text,
                    "occurred_at": service.clock(),
                    "received_at": service.clock(),
                }
            )
            incoming = source.model_copy(
                update={
                    "event": event,
                    "meta": meta(service, budget_ms=3000),
                    "fence": source.fence.model_copy(update={"owner_epoch": owner}),
                }
            )
            accepted = await service.input_event(incoming)
            assert accepted.status == "ok", (
                accepted.error.code if accepted.error else accepted.status
            )
            assert accepted.data.persisted
            await service.next_job("provider-smoke", gateway, embedding)
            event_refs.append(event.event_id)
        async with database.sessions() as db:
            meeting_records = [
                load_record(row)
                for row in await db.scalars(
                    select(FactVersion).where(
                        FactVersion.status == "active",
                        FactVersion.system_to.is_(None),
                        FactVersion.predicate.in_(["plan", "important_event"]),
                    )
                )
            ]
            assert len(meeting_records) == 1, (
                "Synthetic meeting refinement was not unique"
            )
            meeting = meeting_records[0]
            assert meeting.statement.valid_time.precision == "instant"
            assert {p.event_id for p in meeting.sources} == set(event_refs)
            assert "3.00" in meeting.display_text
        # Resolve the same stable identity anew, then start a separate session.
        assert (
            await service.resolve_account("https://issuer.test", subject)
            == scope.user_id
        )
        view, _ = await session(service, scope)
        persona = PersonaCard(
            persona_id="companion",
            version=1,
            authored_identity="用简短中文自然回答。只根据合法记忆回答用户的问题。",
            behavior_boundaries=["不捏造用户经历，不承诺已记住未提交的信息。"],
            preferred_style=["简短中文"],
            lore_refs=[],
            approved_at=service.clock(),
        )
        conversation = await PrivateConversation.bind(
            service,
            view.session_id,
            view.room_name,
            "voice-smoke",
            persona=persona,
            embedding_gateway=embedding,
        )
        agent = Companion(conversation)
        pipeline = load_pipeline()
        capture = Capture()
        replies = []
        async with utils.http_context.open():
            llm = build_provider("llm", pipeline.llm)
            tts = build_provider("tts", pipeline.tts)
            voice_session = AgentSession(
                llm=llm,
                tts=tts,
                turn_handling={"preemptive_generation": {"enabled": False}},
            )
            voice_session.output.audio = capture

            @voice_session.on("conversation_item_added")
            def message(event):
                if getattr(event.item, "role", None) == "assistant":
                    replies.append(event.item.text_content)

            await voice_session.start(agent, record=False)
            started = monotonic()
            handle = voice_session.generate_reply(
                user_input="还记得我吗？先说我的姓名，再说我喜欢喝什么茶。"
            )
            await asyncio.wait_for(capture.first.wait(), 20)
            async with asyncio.timeout(20):
                await handle
            assert capture.samples > 0, agent.notices
            text = "".join(replies)
            assert "陆星" in text and "茉莉" in text, (
                "Expected synthetic facts were absent from the actual reply"
            )
            assert any(
                state == "source_saved" and saved for state, saved, _ in agent.notices
            )
            async with database.sessions() as db:
                outputs = list(await db.scalars(select(OutputLedger)))
                assert outputs and all(
                    output.state != "completed" for output in outputs
                )
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "real_extraction": True,
                        "real_embedding_dimensions": embedding.profile.dimensions,
                        "cross_session_facts": len(records),
                        "two_utterance_meeting_refined": True,
                        "natural_identity_query": True,
                        "voice_hooks": True,
                        "tts_samples_captured": capture.samples,
                        "first_frame_ms": round((capture.first_at - started) * 1000, 2),
                        "reply_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "device_playback_measured": False,
                        "capacity_or_quality_acceptance": False,
                    },
                    ensure_ascii=False,
                )
            )
            await voice_session.aclose()
            voice_session = None
            await llm.aclose()
            await tts.aclose()
    finally:
        if voice_session:
            await voice_session.aclose()
        await gateway.close()
        await embedding.close()
        async with database.engine.begin() as conn:
            await conn.run_sync(MemoryBase.metadata.drop_all)
        await database.close()


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())
