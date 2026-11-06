"""Confirmed people, stable aliases and deferred-source reprocessing."""
# ruff: noqa: F811 -- imported pytest fixtures

from uuid import uuid4

import pytest
from sqlalchemy import select
from test_memory_foundation import (  # noqa: F401
    account,
    candidate_for,
    event_request,
    foundation,
    plan_commit,
    tiny_profile,
)

from avatarweave_agent.memory.contracts import AdmissionOutput, Budget, PageRequest, QueryInput
from avatarweave_agent.memory.entities import EntityBody
from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.facts import load_record
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.storage import SourceRevision
from avatarweave_agent.memory.tables import Entity, FactVersion, Job


@pytest.mark.asyncio
async def test_person_confirmation_reprocesses_original_source_without_promoting_fact(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我的同事李云住在杭州"})}
    )
    await service.input_event(write)
    candidate = candidate_for(write, value="杭州")
    candidate = candidate.model_copy(
        update={"statement": candidate.statement.model_copy(update={"subject_id": "同事"})}
    )

    class Extractor:
        async def extract(self, request):
            return AdmissionOutput(candidates=[candidate], decisions=[], input_watermark=0)

    await service.next_job("worker", Extractor())
    async with service.database.sessions() as db:
        assert (
            await db.get(SourceRevision, (write.event.event_id, 1))
        ).state == "needs_confirmation"
    body = EntityBody(
        canonical_name="李云",
        aliases=["同事"],
        expected_version=None,
        idempotency_key="confirm-mother",
    )
    person = await service.save_entity(write.scope, body)
    assert person.entity_id != "同事" and person.version == 1
    assert (await service.save_entity(write.scope, body)) == person
    await service.next_job("worker", Extractor())
    async with service.database.sessions() as db:
        record = load_record(await db.scalar(select(FactVersion)))
        assert record.statement.subject_id == person.entity_id
        assert record.assertion_kind == "user_report"
        assert len(list(await db.scalars(select(Job)))) == 2
    renamed = await service.save_entity(
        write.scope,
        EntityBody(
            canonical_name="李芸",
            aliases=["同事", "李云"],
            expected_version=1,
            idempotency_key="rename",
        ),
        person.entity_id,
    )
    assert renamed.entity_id == person.entity_id and renamed.version == 2
    forged = await account(service)
    with pytest.raises(MemoryFault):
        await service.save_entity(
            forged,
            body.model_copy(update={"idempotency_key": "forged", "expected_version": 2}),
            person.entity_id,
        )
    from avatarweave_agent.memory.contracts import DeleteMemoryBody

    deleted = await service.delete_entity(
        write.scope,
        person.entity_id,
        DeleteMemoryBody(
            expected_version=2,
            confirmed=True,
            delete_related_derivatives=True,
            idempotency_key="forget-person",
        ),
    )
    assert deleted.state == "revoked_cleaning"
    async with service.database.sessions() as db:
        assert await db.get(Entity, person.entity_id) is None
        assert (await db.scalar(select(FactVersion))).record_json is None
    with pytest.raises(MemoryFault):
        await service.save_entity(
            write.scope,
            body.model_copy(update={"idempotency_key": "stale", "expected_version": 1}),
            person.entity_id,
        )


@pytest.mark.asyncio
async def test_same_name_people_stay_ambiguous_until_unique_alias_is_used(foundation):
    service = foundation
    write = await event_request(service, await account(service))
    people = []
    for alias in ["同事小林", "室友小林"]:
        people.append(
            await service.save_entity(
                write.scope,
                EntityBody(
                    canonical_name="小林",
                    aliases=[alias],
                    expected_version=None,
                    idempotency_key=uuid4().hex,
                ),
            )
        )
    query = QueryInput(
        meta=meta(service, lane="dialogue", budget_ms=1000),
        scope=write.scope,
        fence=write.fence,
        latest_event=write.event.model_copy(update={"content": "小林住在哪里"}),
        recent_topic=None,
        unresolved_refs=[],
        known_entity_ids=[],
        budget=Budget(),
    )
    planned = await service.query(query)
    assert planned.data.ambiguous and all(q.purpose == "clarify" for q in planned.data.queries)
    unique_query = await service.query(
        query.model_copy(
            update={
                "latest_event": query.latest_event.model_copy(
                    update={"content": "同事小林住在哪里"}
                )
            }
        )
    )
    assert not unique_query.data.ambiguous
    assert unique_query.data.queries[0].entity_ids == [people[0].entity_id]
    candidate = candidate_for(write).model_copy(
        update={
            "statement": candidate_for(write).statement.model_copy(update={"subject_id": "小林"})
        }
    )
    async with service.database.sessions() as db:
        with pytest.raises(MemoryFault):
            await service.normalize_subject(db, write.scope, candidate)
        unique = await service.normalize_subject(
            db,
            write.scope,
            candidate.model_copy(
                update={
                    "statement": candidate.statement.model_copy(update={"subject_id": "同事小林"})
                }
            ),
        )
        assert unique.statement.subject_id == people[0].entity_id
    page = await service.public_entities(write.scope, PageRequest())
    assert len(page.items) == 2
    async with service.database.sessions() as db:
        assert len(list(await db.scalars(select(Entity)))) == 2


@pytest.mark.asyncio
async def test_schema_v2_upgrade_rebuilds_scoped_unicode_alias_projection(foundation):
    from avatarweave_agent.memory.contracts import EntityCandidate
    from avatarweave_agent.memory.storage import SchemaVersion
    from avatarweave_agent.memory.tables import EntityAlias

    service = foundation
    scope = await account(service)
    candidate = EntityCandidate(
        entity_id=uuid4().hex,
        entity_type="person",
        canonical_name="Ａｌｉｃｅ",
        aliases=["Alice", "阿丽"],
        context_refs=[],
    )
    async with service.database.sessions.begin() as db:
        (await db.get(SchemaVersion, 1)).version = 2
        db.add(
            Entity(
                id=candidate.entity_id,
                account_id=scope.user_id,
                companion_id=scope.companion_id,
                entity_json=candidate.model_dump_json(),
            )
        )
    await service.database.create_schema()
    await service.database.check_schema()
    async with service.database.sessions() as db:
        rows = list(
            await db.scalars(select(EntityAlias).where(EntityAlias.account_id == scope.user_id))
        )
        assert {row.alias for row in rows} == {"Alice", "阿丽"}
        assert all(row.entity_id == candidate.entity_id for row in rows)
        assert (await db.get(SchemaVersion, 1)).version == 3


@pytest.mark.asyncio
async def test_named_person_filters_lexical_temporal_and_vector_routes(foundation):
    from datetime import timedelta

    import httpx

    from avatarweave_agent.memory.contracts import (
        EmbeddingInput,
        QuerySpec,
        RetrievalInput,
        TimeWindow,
    )
    from avatarweave_agent.memory.embedding import EmbeddingGateway, render_record

    service = foundation
    scope = await account(service)
    write = await event_request(service, scope)
    people, records = [], []
    for name, alias, city in [("李云", "妈妈", "杭州"), ("林雨", "同事小林", "苏州")]:
        person = await service.save_entity(
            scope,
            EntityBody(canonical_name=name, aliases=[alias], idempotency_key=uuid4().hex),
        )
        people.append(person)
        source = write.model_copy(
            update={
                "event": write.event.model_copy(
                    update={
                        "event_id": uuid4().hex,
                        "turn_id": uuid4().hex,
                        "content": f"{alias}{name}住在{city}",
                    }
                )
            }
        )
        persisted = await service.input_event(source)
        assert persisted.status == "ok", persisted
        candidate = candidate_for(source, value=city)
        candidate = candidate.model_copy(
            update={
                "statement": candidate.statement.model_copy(update={"subject_id": person.entity_id})
            }
        )
        result, _ = await plan_commit(service, source, candidate)
        assert result.status == "ok", result
        async with service.database.sessions() as db:
            records.append(
                load_record(
                    await db.scalar(
                        select(FactVersion).where(
                            FactVersion.memory_id == result.data.committed[0].object_id
                        )
                    )
                )
            )

    def provider(request):
        import json

        texts = json.loads(request.content)["input"]
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "data": [
                    {"index": index, "embedding": [1.0, 0.0, 0.0]} for index in range(len(texts))
                ],
                "usage": {"total_tokens": len(texts)},
            },
        )

    gateway = EmbeddingGateway(
        base_url="https://embedding.test/v1",
        api_key="test",
        profile=tiny_profile(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    try:
        indexed = await service.embedding(
            EmbeddingInput(
                meta=meta(service, lane="maintenance", budget_ms=3000),
                scope=scope,
                fence=write.fence,
                profile=tiny_profile(),
                purpose="index",
                items=[(record.ref, render_record(record)) for record in records],
            ),
            gateway,
        )
        assert indexed.status == "ok", indexed
        for route in ("lexical", "temporal", "semantic"):
            query = QuerySpec(
                query_id="specific-person",
                text="妈妈现在住在哪个城市",
                route=route,
                entity_ids=[people[0].entity_id],
                time_filter=TimeWindow(
                    start=service.clock() - timedelta(days=1),
                    end=service.clock() + timedelta(days=1),
                    precision="day",
                )
                if route == "temporal"
                else None,
                purpose="answer",
            )
            recalled = await service.retrieve(
                RetrievalInput(
                    meta=meta(service, lane="deep_recall", budget_ms=5000),
                    scope=scope,
                    fence=write.fence,
                    queries=[query],
                    profile=tiny_profile(),
                    budget=Budget(),
                    include_historical=True,
                ),
                gateway,
            )
            assert recalled.status == "ok", recalled
            assert recalled.data.selected_refs == [records[0].ref], recalled
            if route == "temporal":
                assert any(
                    rank.route == "temporal"
                    for item in recalled.data.candidates
                    for rank in item.ranks
                )
            assert all(
                item.record.statement.subject_id == people[0].entity_id
                for item in recalled.data.candidates
            )
    finally:
        await gateway.close()
