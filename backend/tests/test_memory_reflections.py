"""Hypotheses remain reviewable proposals; user wording supplies new authority."""
# ruff: noqa: F811 -- pytest shared fixtures

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_memory_foundation import account, candidate_for, event_request, foundation, plan_commit  # noqa: F401

from avatarweave_agent.memory.contracts import (
    Hypothesis,
    ModelTaskOutput,
    PageRequest,
    ReflectionInput,
    ReflectionOutput,
)
from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.facts import load_record
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.reflections import ReflectionReviewBody
from avatarweave_agent.memory.storage import MemorySession
from avatarweave_agent.memory.tables import DerivedView, FactVersion


async def reflection_fixture(service):
    scope = await account(service)
    write = None
    for day, value in enumerate(["茶", "安静", "独处"]):
        write = await event_request(service, scope)
        write = write.model_copy(
            update={
                "event": write.event.model_copy(
                    update={
                        "content": "我喜欢" + value,
                        "occurred_at": service.clock() - timedelta(days=day),
                    }
                )
            }
        )
        await service.input_event(write)
        result, _ = await plan_commit(
            service,
            write,
            candidate_for(write, predicate="preference", value=value).model_copy(
                update={"content_kind": "preference"}
            ),
        )
        assert result.status == "ok", result
        async with service.database.sessions.begin() as db:
            (await db.get(MemorySession, write.session_id)).status = "ended"
    async with service.database.sessions() as db:
        records = [load_record(r) for r in await db.scalars(select(FactVersion))]
    hypothesis = Hypothesis(
        text="你可能更喜欢安静的休息方式",
        supports=[r.ref for r in records],
        counters=[],
        scope_conditions=[],
        review_after=service.clock() + timedelta(days=30),
        requires_confirmation=False,
        status="candidate",
    )

    class Gateway:
        async def task(self, request):
            return ModelTaskOutput(
                task="reflect",
                profile_id=request.profile_id,
                payload=ReflectionOutput(hypotheses=[hypothesis], rejected_reasons=[]),
                input_tokens=1,
                output_tokens=1,
                provider_request_ref="fixture-reflection",
                latency_ms=1.0,
            )

    request = ReflectionInput(
        meta=meta(service, lane="maintenance", budget_ms=5000),
        scope=scope,
        fence=write.fence,
        records=records,
    )
    result = await service.reflection(request, Gateway())
    assert result.status == "ok" and result.data.hypotheses[0].requires_confirmation, result
    return scope, request, records, Gateway()


@pytest.mark.asyncio
async def test_reflection_persistence_user_confirmation_and_idempotence(foundation):
    service = foundation
    scope, request, records, gateway = await reflection_fixture(service)
    page = await service.list_reflections(scope, PageRequest())
    assert len(page.items) == 1 and page.items[0].state == "needs_confirmation"
    await service.reflection(
        request.model_copy(
            update={
                "meta": meta(service, lane="maintenance", budget_ms=5000).model_copy(
                    update={"operation_id": request.meta.operation_id}
                )
            }
        ),
        gateway,
    )
    assert len((await service.list_reflections(scope, PageRequest())).items) == 1
    item = page.items[0]
    with pytest.raises(MemoryFault):
        await service.review_reflection(
            scope,
            item.reflection_id,
            ReflectionReviewBody(
                expected_version=1, action="confirm", confirmation_text="", idempotency_key="empty"
            ),
        )
    body = ReflectionReviewBody(
        expected_version=1,
        action="confirm",
        confirmation_text="我休息时喜欢安静",
        predicate="preference",
        polarity="positive",
        idempotency_key="my-confirmation",
    )
    saved = await service.review_reflection(scope, item.reflection_id, body)
    assert saved.state == "confirmed" and saved.confirmed_memory_id
    assert (await service.review_reflection(scope, item.reflection_id, body)) == saved
    async with service.database.sessions() as db:
        confirmed = load_record(
            await db.scalar(
                select(FactVersion).where(FactVersion.memory_id == saved.confirmed_memory_id)
            )
        )
        assert confirmed.assertion_kind == "user_confirmed"
        assert confirmed.statement.canonical_value == body.confirmation_text
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == len(records) + 1
    with pytest.raises(MemoryFault):
        await service.review_reflection(
            await account(service),
            item.reflection_id,
            body.model_copy(update={"idempotency_key": "forged"}),
        )


@pytest.mark.asyncio
async def test_invalidated_hypothesis_cannot_be_confirmed(foundation):
    from avatarweave_agent.memory.facts import invalidate

    service = foundation
    scope, _, records, _ = await reflection_fixture(service)
    item = (await service.list_reflections(scope, PageRequest())).items[0]
    async with service.database.sessions.begin() as db:
        await invalidate(db, scope.user_id, [records[0].ref])
        assert (await db.get(DerivedView, (item.reflection_id, 1))).state == "invalidated"
    with pytest.raises(MemoryFault):
        await service.review_reflection(
            scope,
            item.reflection_id,
            ReflectionReviewBody(
                expected_version=1,
                action="confirm",
                confirmation_text="这是我希望保留的偏好",
                idempotency_key="late",
            ),
        )
