"""Incremental scheduler and shared queue invariants on SQLite and pgvector."""
# ruff: noqa: F811 -- pytest imports shared fixture functions

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from test_memory_foundation import (  # noqa: F401
    account,
    candidate_for,
    encrypted_store,
    event_request,
    foundation,
    plan_commit,
    tiny_profile,
)

from avatarweave_agent.memory.contracts import (
    AdmissionOutput,
    EmbeddedItem,
    EmbeddingInput,
    EmbeddingOutput,
)
from avatarweave_agent.memory.embedding import render_record
from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.facts import load_record
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.storage import Account, Outbox, SourceRevision
from avatarweave_agent.memory.tables import FactVersion, Job, ObjectManifest, VectorEntry


class Embedding:
    profile = tiny_profile()
    calls = 0

    async def embed(self, request):
        self.calls += 1
        return EmbeddingOutput(
            profile=self.profile,
            input_tokens=1,
            index_generation="index:r1",
            items=[EmbeddedItem(ref=ref, values=[1.0, 0.0, 0.0]) for ref, _ in request.items],
        )


@pytest.mark.asyncio
async def test_provider_recovery_schedules_missing_index_and_settles_source(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)

    class Extractor:
        async def extract(self, request):
            return AdmissionOutput(
                candidates=[candidate_for(write)], decisions=[], input_watermark=0
            )

    await service.next_job("extractor", Extractor())
    async with service.database.sessions() as db:
        source = await db.get(SourceRevision, (write.event.event_id, 1))
        assert source.structured_settled and not source.index_settled
    embedding = Embedding()
    assert await service.schedule_incremental(embedding) == 1
    await service.next_job("repair", None, embedding)
    async with service.database.sessions() as db:
        assert (await db.get(SourceRevision, (write.event.event_id, 1))).index_settled
        owner = await db.get(Account, write.scope.user_id)
        assert owner.index_watermark == owner.source_watermark
        assert await db.scalar(select(func.count()).select_from(VectorEntry)) == 1
    for _ in range(3):
        assert await service.schedule_incremental(embedding) == 0
    assert embedding.calls == 1


@pytest.mark.asyncio
async def test_outbox_and_jobs_share_one_count_and_byte_budget(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write))
    service.max_outbox = 1
    async with service.database.sessions() as db:
        record = load_record(await db.scalar(select(FactVersion)))
    payload = EmbeddingInput(
        meta=meta(service, lane="maintenance"),
        scope=write.scope,
        fence=write.fence,
        profile=tiny_profile(),
        purpose="index",
        items=[(record.ref, render_record(record))],
    )
    with pytest.raises(MemoryFault) as busy:
        await service.enqueue_maintenance("embed", payload)
    assert busy.value.info.code == "overloaded"
    await service.dispatch_outbox()
    async with service.database.sessions() as db:
        total, used, _, user = await service.queue_usage(db, write.scope.user_id)
        assert total == user == 1 and used > 0  # dispatch is not a second queue slot


@pytest.mark.asyncio
async def test_cold_tiering_verifies_archive_before_changing_location(foundation, encrypted_store):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我住在北京"})}
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write))
    async with service.database.sessions.begin() as db:
        row = await db.scalar(select(FactVersion))
        old = load_record(row).model_copy(
            update={"mentioned_at": service.clock() - timedelta(days=400)}
        )
        row.record_json = old.model_dump_json()
        await db.execute(update(Outbox).values(state="done"))
    assert await service.schedule_incremental(store=encrypted_store) == 1
    await service.next_job("tier-worker", None, store=encrypted_store)
    async with service.database.sessions() as db:
        assert load_record(await db.scalar(select(FactVersion))).tier == "hot"
        assert await db.scalar(select(Job).where(Job.kind == "archive", Job.state == "queued"))
    await service.next_job("archive-worker", None, store=encrypted_store)
    async with service.database.sessions() as db:
        assert load_record(await db.scalar(select(FactVersion))).tier == "cold"
        assert (await db.scalar(select(ObjectManifest))).state == "active"
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == 1


@pytest.mark.asyncio
async def test_retry_uses_bounded_jitter(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    await service.input_event(write)

    class Failing:
        async def extract(self, request):
            raise MemoryFault("provider_unavailable", "Temporarily unavailable", retryable=True)

    before = service.clock()
    await service.next_job("worker", Failing())
    from avatarweave_agent.memory.utils import utc

    async with service.database.sessions() as db:
        job = await db.scalar(select(Job))
        assert job.state == "retry_wait" and job.attempt == 1
        assert 30 <= (utc(job.available_at) - before).total_seconds() <= 37
