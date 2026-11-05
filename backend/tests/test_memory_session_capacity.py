"""Session slots follow live workers/reservations, not abandoned historical rows."""
# ruff: noqa: F811 -- pytest injects imported fixtures

from datetime import timedelta

import pytest
from test_memory_foundation import account, event_request, foundation, session  # noqa: F401

from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.storage import MemorySession, SourceRevision


@pytest.mark.asyncio
async def test_expired_workers_do_not_block_new_session_or_erase_sources(foundation):
    service = foundation
    scope = await account(service)
    write = await event_request(service, scope)
    assert (await service.input_event(write)).status == "ok"
    generation, fence = await service.begin_generation(
        scope, write.session_id, write.fence.owner_epoch
    )
    second, _ = await session(service, scope)
    await service.claim_owner(scope, second.session_id, second.room_name, "second")
    now = service.clock()
    service.clock = lambda: now + timedelta(seconds=31)
    third, _ = await session(service, scope)
    assert await service.claim_owner(scope, third.session_id, third.room_name, "third") == 1
    assert not await service.generation_valid(scope, write.session_id, generation, fence)
    async with service.database.sessions() as db:
        source = await db.get(SourceRevision, (write.event.event_id, 1))
        assert source.source_json and not source.revoked
        assert (await db.get(MemorySession, write.session_id)).status != "ended"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_reservation_expires_but_resume_and_delayed_worker_still_obey_capacity(
    foundation, legacy
):
    service = foundation
    scope = await account(service)
    first, request = await session(service, scope)
    second, _ = await session(service, scope)
    if legacy:
        async with service.database.sessions.begin() as db:
            for view in (first, second):
                (await db.get(MemorySession, view.session_id)).lease_until = None
    third_request = request.model_copy(update={"idempotency_key": "third"})
    assert (await service.session(third_request)).error.code == "quota_exceeded"
    now = service.clock()
    service.clock = lambda: now + timedelta(minutes=3)
    service.executor.clock = service.clock
    third_request = third_request.model_copy(update={"meta": meta(service, budget_ms=1000)})
    third = await service.session(third_request)
    assert third.status == "ok", third
    fourth, _ = await session(service, scope)
    resume = request.model_copy(
        update={
            "operation": "resume",
            "session_id": first.session_id,
            "idempotency_key": "resume",
            "meta": meta(service, budget_ms=1000),
        }
    )
    assert (await service.session(resume)).error.code == "quota_exceeded"
    with pytest.raises(MemoryFault) as caught:
        await service.claim_owner(scope, second.session_id, second.room_name, "delayed")
    assert caught.value.info.code == "quota_exceeded"
    ended = request.model_copy(
        update={
            "operation": "end",
            "session_id": fourth.session_id,
            "idempotency_key": "end-fourth",
            "meta": meta(service, budget_ms=1000),
        }
    )
    assert (await service.session(ended)).status == "ok"
    recovered = await service.session(resume)
    assert recovered.status == "ok"
    assert recovered.data.room_name != first.room_name
    with pytest.raises(MemoryFault):
        await service.claim_owner(scope, first.session_id, first.room_name, "old-room")
    assert (
        await service.claim_owner(scope, first.session_id, recovered.data.room_name, "resumed") == 1
    )


@pytest.mark.asyncio
async def test_resume_preserves_live_owner_and_does_not_displace_another_conversation(foundation):
    service = foundation
    scope = await account(service)
    first, request = await session(service, scope)
    owner = await service.claim_owner(scope, first.session_id, first.room_name, "first")
    second, _ = await session(service, scope)
    second_owner = await service.claim_owner(scope, second.session_id, second.room_name, "second")
    resumed = await service.session(
        request.model_copy(
            update={
                "operation": "resume",
                "session_id": first.session_id,
                "idempotency_key": "resume-live",
            }
        )
    )
    assert resumed.status == "ok" and resumed.data.owner_epoch == owner
    assert resumed.data.room_name == first.room_name
    await service.release_owner(scope, second.session_id, "second", second_owner)
    await session(service, scope)
    blocked = await service.session(
        request.model_copy(
            update={
                "operation": "resume",
                "session_id": second.session_id,
                "idempotency_key": "resume-blocked",
            }
        )
    )
    assert blocked.error.code == "quota_exceeded"
    async with service.database.sessions() as db:
        assert (await db.get(MemorySession, first.session_id)).owner_id == "first"
        assert (await db.get(MemorySession, second.session_id)).owner_id is None
