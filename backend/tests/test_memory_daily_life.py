"""Life companionship recall must work without lexical overlap or embeddings."""

# ruff: noqa: F811
import pytest
from test_memory_foundation import (
    account,
    candidate_for,
    event_request,
    foundation,  # noqa: F401
    plan_commit,
    tiny_profile,
)

from avatarweave_agent.memory.contracts import Budget, QueryInput, RetrievalInput
from avatarweave_agent.memory.http import meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,predicate,value",
    [
        ("你记住了什么", "preference", "茉莉花茶"),
        ("回忆一下我的人生时间线", "important_event", "毕业旅行去了大理"),
        ("聊聊我重要的人", "relationship", "姐姐林雨"),
        ("我有什么爱好", "preference", "徒步"),
        ("还记得我的安排吗", "plan", "周末去看展"),
    ],
)
async def test_life_category_recall_without_shared_words(foundation, question, predicate, value):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(update={"event": write.event.model_copy(update={"content": value})})
    await service.input_event(write)
    candidate = candidate_for(write, value=value)
    candidate = candidate.model_copy(
        update={"statement": candidate.statement.model_copy(update={"predicate": predicate})}
    )
    committed, _ = await plan_commit(service, write, candidate)
    planned = await service.query(
        QueryInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            latest_event=write.event.model_copy(update={"content": question}),
            recent_topic=None,
            unresolved_refs=[],
            known_entity_ids=[],
            budget=Budget(),
        )
    )
    assert planned.status == "ok", planned
    recalled = await service.retrieve(
        RetrievalInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            queries=planned.data.queries,
            profile=tiny_profile(),
            budget=Budget(),
            include_historical=any(q.purpose == "reminisce" for q in planned.data.queries),
        )
    )
    assert recalled.status == "ok", recalled
    assert {r.object_id for r in committed.data.committed} <= {
        r.object_id for r in recalled.data.selected_refs
    }
    if predicate == "important_event":
        assert planned.data.queries[0].purpose == "reminisce"


@pytest.mark.asyncio
async def test_person_is_automatically_created_from_grounded_report(foundation):
    from sqlalchemy import select

    from avatarweave_agent.memory.contracts import AdmissionOutput
    from avatarweave_agent.memory.facts import load_record
    from avatarweave_agent.memory.storage import SourceRevision
    from avatarweave_agent.memory.tables import Entity, FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我妈妈喜欢茉莉花茶"})}
    )
    await service.input_event(write)
    candidate = candidate_for(write, value="茉莉花茶")
    candidate = candidate.model_copy(
        update={
            "statement": candidate.statement.model_copy(
                update={"subject_id": "妈妈", "predicate": "preference"}
            )
        }
    )

    class Extractor:
        async def extract(self, request):
            return AdmissionOutput(candidates=[candidate], decisions=[], input_watermark=0)

    await service.next_job("automatic", Extractor())
    async with service.database.sessions() as db:
        entity = await db.scalar(select(Entity))
        record = load_record(await db.scalar(select(FactVersion)))
        assert entity and record.statement.subject_id == entity.id
        assert record.assertion_kind == "user_report"
        assert (await db.get(SourceRevision, (write.event.event_id, 1))).structured_settled
    assert await service.schedule_source_repair(write.scope) == 1
    await service.next_job("repair", Extractor())
    assert await service.schedule_source_repair(write.scope) == 0
    async with service.database.sessions() as db:
        assert len(list(await db.scalars(select(Entity)))) == 1


@pytest.mark.asyncio
async def test_legacy_missed_source_repaired_without_operator(foundation):
    from sqlalchemy import select

    from avatarweave_agent.memory.contracts import AdmissionOutput
    from avatarweave_agent.memory.storage import SourceRevision
    from avatarweave_agent.memory.tables import FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    await service.input_event(write)

    class Missed:
        async def extract(self, request):
            return AdmissionOutput(candidates=[], decisions=[], input_watermark=0)

    class Fixed:
        async def extract(self, request):
            return AdmissionOutput(
                candidates=[candidate_for(write, value="茶")], decisions=[], input_watermark=0
            )

    await service.next_job("old", Missed())
    assert await service.schedule_source_repair(write.scope) == 1
    await service.next_job("new", Fixed())
    async with service.database.sessions() as db:
        assert (await db.get(SourceRevision, (write.event.event_id, 1))).structured_settled
        assert await db.scalar(select(FactVersion)) is not None
    assert await service.schedule_source_repair(write.scope) == 0


@pytest.mark.parametrize(
    "expression,year,month",
    [
        ("去年3月", 2025, 3),
        ("前年", 2024, 1),
        ("上个月", 2026, 10),
        ("十一年前", 2015, 1),
        ("上周一", 2026, 10),
    ],
)
def test_life_timeline_relative_dates(expression, year, month):
    from datetime import UTC, datetime

    from avatarweave_agent.memory.lifecycle import resolve_time

    period, confidence, clarification = resolve_time(
        expression, datetime(2026, 11, 5, tzinfo=UTC), "Asia/Shanghai"
    )
    from zoneinfo import ZoneInfo

    local = period.start.astimezone(ZoneInfo("Asia/Shanghai"))
    assert (local.year, local.month) == (year, month)
    assert confidence == "derived" and clarification is None


@pytest.mark.asyncio
async def test_real_provider_daily_life_chain(foundation, monkeypatch):
    import os

    if os.environ.get("MEMORY_REAL_MODEL_TEST") != "1":
        pytest.skip("explicit real provider opt-in")
    from sqlalchemy import select

    from avatarweave_agent.memory.contracts import EntityCandidate, ModelMessage, ModelTaskInput
    from avatarweave_agent.memory.facts import load_record
    from avatarweave_agent.memory.gateway import ModelGateway
    from avatarweave_agent.memory.tables import Entity, FactVersion, Job

    monkeypatch.delenv("PIPELINE_MODE", raising=False)
    monkeypatch.delenv("PIPELINE_PROFILE", raising=False)
    gateway = ModelGateway.from_pipeline()
    assert gateway is not None
    service = foundation
    scope = await account(service)
    write = await event_request(service, scope)
    write = write.model_copy(
        update={
            "event": write.event.model_copy(
                update={
                    "content": "我喜欢茉莉花茶。我妈妈喜欢种兰花。2022年我毕业去了杭州工作。明天我打算去看画展。"
                }
            )
        }
    )
    try:
        await service.input_event(write)
        await service.next_job("real-provider", gateway)
        async with service.database.sessions() as db:
            records = [
                load_record(r)
                for r in await db.scalars(select(FactVersion).where(FactVersion.status == "active"))
            ]
            people = {
                row.id: EntityCandidate.model_validate_json(row.entity_json).canonical_name
                for row in await db.scalars(select(Entity))
            }
            assert people, [
                (job.state, job.last_error_json) for job in await db.scalars(select(Job))
            ]
        assert {"preference", "important_event", "plan"} <= {r.statement.predicate for r in records}
        query = await service.query(
            QueryInput(
                meta=meta(service),
                scope=scope,
                fence=write.fence,
                latest_event=write.event.model_copy(
                    update={"content": "回忆一下你记住了什么，包括我妈妈的爱好和我的经历、计划"}
                ),
                recent_topic=None,
                unresolved_refs=[],
                known_entity_ids=[],
                budget=Budget(),
            )
        )
        recalled = await service.retrieve(
            RetrievalInput(
                meta=meta(service),
                scope=scope,
                fence=write.fence,
                queries=query.data.queries,
                profile=tiny_profile(),
                budget=Budget(),
                include_historical=True,
            )
        )
        assert recalled.data.candidates
        from test_memory_foundation import session

        from avatarweave_agent.memory.contracts import PersonaCard
        from avatarweave_agent.memory.conversation import PrivateConversation

        view, _ = await session(service, scope)
        conversation = await PrivateConversation.bind(
            service,
            view.session_id,
            view.room_name,
            "real-dialogue",
            persona=PersonaCard(
                persona_id="companion",
                version=1,
                authored_identity="日常生活陪伴伙伴",
                behavior_boundaries=["不编造经历"],
                preferred_style=[],
                lore_refs=[],
                approved_at=service.clock(),
            ),
        )
        prepared = await conversation.prepare(
            "recall-turn", "回忆一下你记住了什么，包括我妈妈的爱好和我的经历、计划"
        )
        evidence = "\n".join(block.text for block in prepared.blocks if block.kind == "memory")
        assert "妈妈" in evidence and "茉莉花茶" in evidence and "画展" in evidence
        answer = await gateway.task(
            ModelTaskInput(
                meta=meta(service, lane="maintenance", budget_ms=15000),
                scope=scope,
                fence=write.fence,
                task="dialogue",
                profile_id="daily-life-eval.v1",
                prompt_version="daily-life-eval.v1",
                output_schema="dialogue.text.v1",
                messages=[
                    ModelMessage(
                        role="system", content="只根据给定记忆回答，区分人物和时间，不能编造。"
                    ),
                    ModelMessage(
                        role="user",
                        content="我的妈妈喜欢什么？我喜欢什么？我2022年经历了什么？明天有什么计划？\n参考："
                        + evidence,
                    ),
                ],
                max_output_tokens=1000,
            )
        )
        assert all(word in answer.payload.text for word in ["兰花", "茉莉花茶", "杭州", "画展"])
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_explicit_family_name_links_automatically_and_scoped(foundation):
    from sqlalchemy import select

    from avatarweave_agent.memory.contracts import AdmissionOutput, PageRequest
    from avatarweave_agent.memory.tables import Entity, EntityAlias

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我妈妈叫李云"})}
    )
    await service.input_event(write)
    candidate = candidate_for(write, predicate="relationship", value="妈妈叫李云")

    class Extractor:
        async def extract(self, request):
            return AdmissionOutput(candidates=[candidate], decisions=[], input_watermark=0)

    await service.next_job("auto-family", Extractor())
    async with service.database.sessions() as db:
        assert len(list(await db.scalars(select(Entity)))) == 1
        aliases = dict((await db.execute(select(EntityAlias.alias, EntityAlias.entity_id))).all())
        assert aliases["妈妈"] == aliases["李云"]
        assert aliases["妈妈"] != write.scope.user_id
    other = await account(service)
    people = await service.public_entities(other, PageRequest())
    assert not people.items


def test_current_preference_does_not_expire_immediately():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from avatarweave_agent.memory.contracts import (
        Scope,
        SourceEvent,
        TemporalInput,
        TimeWindow,
        VersionFence,
    )
    from avatarweave_agent.memory.http import meta
    from avatarweave_agent.memory.lifecycle import temporal

    now = datetime(2026, 11, 5, tzinfo=UTC)
    scope = Scope(workspace_id="default", user_id="user", companion_id="companion")
    event = SourceEvent(
        event_id="source",
        revision=1,
        session_id="session",
        turn_id="turn",
        scope=scope,
        origin="user_text",
        content="我现在不喜欢咖啡了",
        occurred_at=now,
        received_at=now,
        is_final=True,
    )
    # Reuse the evidence constructor so the actual lifecycle receives a grounded span.
    candidate = candidate_for(
        SimpleNamespace(event=event, scope=scope), predicate="preference", value="咖啡"
    )
    candidate = candidate.model_copy(
        update={
            "statement": candidate.statement.model_copy(
                update={
                    "polarity": "negative",
                    "valid_time": TimeWindow(precision="unknown", original_expression="现在"),
                }
            )
        }
    )
    result = temporal(
        TemporalInput(
            meta=meta(SimpleNamespace(clock=lambda: now)),
            scope=scope,
            fence=VersionFence(privacy_epoch=0, memory_revision=0, owner_epoch=0),
            candidates=[candidate],
            user_timezone="Asia/Shanghai",
            anchor_event=event,
        )
    )
    assert result.candidates[0].statement.valid_time.start == now
    assert result.candidates[0].statement.valid_time.end is None


@pytest.mark.asyncio
async def test_companion_nickname_is_not_saved_as_user_name_or_left_pending(foundation):
    from sqlalchemy import select

    from avatarweave_agent.memory.contracts import AdmissionOutput
    from avatarweave_agent.memory.storage import SourceRevision
    from avatarweave_agent.memory.tables import FactVersion

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我叫你小S吧"})}
    )
    await service.input_event(write)

    class MistakenExtractor:
        async def extract(self, request):
            return AdmissionOutput(
                candidates=[candidate_for(write, predicate="name", value="小S")],
                decisions=[],
                input_watermark=0,
            )

    await service.next_job("wrong-speaker", MistakenExtractor())
    async with service.database.sessions() as db:
        assert await db.scalar(select(FactVersion)) is None
        source = await db.get(SourceRevision, (write.event.event_id, 1))
        assert source.state == "rejected" and source.structured_settled
