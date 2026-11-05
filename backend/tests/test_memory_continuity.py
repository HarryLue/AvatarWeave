"""Regression: evidenced time refinements and profile continuity across sessions."""
# ruff: noqa: F811 -- pytest injects imported fixtures

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from test_memory_foundation import (  # noqa: F401
    account,
    candidate_for,
    event_request,
    foundation,
    plan_commit,
    session,
)

from avatarweave_agent.memory.contracts import (
    AdmissionInput,
    AdmissionOutput,
    Budget,
    PersonaCard,
    QuerySpec,
    RetrievalInput,
)
from avatarweave_agent.memory.conversation import PrivateConversation
from avatarweave_agent.memory.execution import MemoryFault
from avatarweave_agent.memory.facts import load_record
from avatarweave_agent.memory.gateway import ModelGateway, literal_self_name
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.lifecycle import resolve_time
from avatarweave_agent.memory.storage import SourceRevision
from avatarweave_agent.memory.tables import DerivedView, FactVersion

ANCHOR = datetime(2026, 11, 5, 1, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("我叫林小舟", "林小舟"),
        ("请记住，我叫林小舟。", "林小舟"),
        ("我的名字是林小舟", "林小舟"),
        ("我叫你别说话", None),
        ("我叫什么名字", None),
        ("我叫林小舟吗？", None),
        ("假如我叫林小舟", None),
        ("他说我叫林小舟", None),
        ("我朋友叫林小舟", None),
        ("我叫林小舟，我喜欢茶", None),
    ],
)
async def test_literal_self_name_only_handles_complete_unambiguous_introduction(
    foundation, text, expected
):
    service = foundation
    write = await event_request(service, await account(service))
    event = write.event.model_copy(update={"content": text})
    request = AdmissionInput(
        meta=meta(service),
        scope=write.scope,
        fence=write.fence,
        events=[event],
        memory_enabled=True,
        extraction_prompt_version="memory-extract.v1",
    )
    result = literal_self_name(request)
    if expected is None:
        assert result is None
    else:
        assert result.candidates[0].statement.canonical_value == expected
        assert result.candidates[0].statement.subject_id == write.scope.user_id
        for change in [{"fictional": True}, {"is_final": False}, {"origin": "assistant_text"}]:
            assert (
                literal_self_name(
                    request.model_copy(update={"events": [event.model_copy(update=change)]})
                )
                is None
            )
        assert literal_self_name(request.model_copy(update={"memory_enabled": False})) is None


@pytest.mark.parametrize(
    "text,hour,minute",
    [
        ("下午3.00", 15, 0),
        ("下午三点", 15, 0),
        ("下午三点半", 15, 30),
        ("下午三点一刻", 15, 15),
        ("下午三点三刻", 15, 45),
        ("15:00", 15, 0),
        ("下午３：００", 15, 0),
        ("晚上八点十五分", 20, 15),
    ],
)
def test_clock_grounding(text, hour, minute):
    window, confidence, clarification = resolve_time(text, ANCHOR, "Asia/Shanghai")
    assert clarification is None and confidence == "derived"
    assert window.start == ANCHOR.replace(hour=hour - 8, minute=minute)
    assert window.start == window.end and window.precision == "instant"
    assert window.original_expression == text


@pytest.mark.parametrize("text", ["三点", "下午3:99", "下午25点", "下午三点到两点"])
def test_ambiguous_or_invalid_clock_does_not_invent_time(text):
    window, confidence, clarification = resolve_time(text, ANCHOR, "Asia/Shanghai")
    assert window.start is None and confidence == "ambiguous" and clarification


def test_relative_dates_ranges_and_weekdays():
    tomorrow, _, _ = resolve_time("明天下午三点", ANCHOR, "Asia/Shanghai")
    assert tomorrow.start == ANCHOR.replace(hour=7) + timedelta(days=1)
    interval, _, _ = resolve_time("明天下午三点到五点", ANCHOR, "Asia/Shanghai")
    assert interval.start == tomorrow.start and interval.end == tomorrow.start + timedelta(hours=2)
    week, _, _ = resolve_time("下周", ANCHOR, "Asia/Shanghai")
    assert week.end - week.start == timedelta(days=7)
    friday, _, _ = resolve_time("本周五", ANCHOR, "Asia/Shanghai")
    assert friday.start.astimezone(ZoneInfo("Asia/Shanghai")).weekday() == 4


def utterance(write, text, turn):
    return write.model_copy(
        update={
            "meta": write.meta.model_copy(update={"operation_id": uuid4().hex}),
            "expected_turn_revision": 0,
            "event": write.event.model_copy(
                update={
                    "event_id": uuid4().hex,
                    "turn_id": turn,
                    "content": text,
                    "occurred_at": ANCHOR + timedelta(seconds=int(turn)),
                    "received_at": ANCHOR + timedelta(seconds=int(turn)),
                }
            ),
        }
    )


class MeetingExtractor:
    def __init__(self, predicate):
        self.predicate = predicate

    async def extract(self, request):
        from types import SimpleNamespace

        write = SimpleNamespace(event=request.events[0], scope=request.scope)
        value = next(
            (term for term in ("客户会议", "项目会议") if term in write.event.content), "会议"
        )
        candidate = candidate_for(write, predicate=self.predicate, value=value)
        candidate = candidate.model_copy(
            update={
                "content_kind": "future_intent" if self.predicate == "plan" else "episode",
            }
        )
        return AdmissionOutput(candidates=[candidate], decisions=[], input_watermark=0)


async def meeting_input(service, write, text, turn, predicate):
    update = utterance(write, text, turn)
    result = await service.input_event(update)
    assert result.status == "ok", result
    assert await service.next_job("worker", MeetingExtractor(predicate))
    return update


async def current_events(service):
    async with service.database.sessions() as db:
        return [
            load_record(row)
            for row in await db.scalars(
                select(FactVersion).where(
                    FactVersion.system_to.is_(None), FactVersion.status == "active"
                )
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate", ["important_event", "plan"])
@pytest.mark.parametrize("detail", ["下午3.00的会议", "下午三点有个会议"])
async def test_second_utterance_refines_one_event_preserving_history_and_sources(
    foundation, predicate, detail
):
    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    first = await meeting_input(service, write, "下午有个会议", "1", predicate)
    original = (await current_events(service))[0]
    second = await meeting_input(service, write, detail, "2", predicate)
    records = await current_events(service)
    assert len(records) == 1
    refined = records[0]
    assert refined.ref.object_id == original.ref.object_id and refined.ref.version == 2
    assert refined.statement.valid_time.start == ANCHOR.replace(hour=7)
    assert refined.statement.valid_time.precision == "instant"
    assert refined.statement.valid_time.original_expression in refined.display_text
    assert {p.event_id for p in refined.sources} == {first.event.event_id, second.event.event_id}
    async with service.database.sessions() as db:
        old = await db.get(FactVersion, (original.ref.object_id, 1))
        assert old.status == "superseded" and old.system_to is not None
        if predicate == "plan":
            loops = list(
                await db.scalars(
                    select(DerivedView).where(
                        DerivedView.kind == "open_loop", DerivedView.state == "valid"
                    )
                )
            )
            assert loops
    assert not await service.next_job("worker", MeetingExtractor(predicate))


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["下午另外有个会议", "明天下午有个会议"])
async def test_unrelated_events_do_not_get_merged(foundation, extra):
    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    await meeting_input(service, write, "下午有个会议", "1", "plan")
    await meeting_input(service, write, extra, "2", "plan")
    await meeting_input(service, write, "下午三点的会议", "3", "plan")
    records = await current_events(service)
    if "另外" in extra:
        assert len(records) == 2 and all(
            r.statement.valid_time.precision == "range" for r in records
        )
    else:
        assert (
            len(records) == 2
            and sum(r.statement.valid_time.precision == "instant" for r in records) == 1
        )


@pytest.mark.asyncio
async def test_precise_conflicting_time_needs_confirmation_unless_explicitly_rescheduled(
    foundation,
):
    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    await meeting_input(service, write, "下午有个会议", "1", "plan")
    await meeting_input(service, write, "下午三点的会议", "2", "plan")
    conflict = await meeting_input(service, write, "下午四点的会议", "3", "plan")
    assert (await current_events(service))[0].statement.valid_time.start == ANCHOR.replace(hour=7)
    async with service.database.sessions() as db:
        assert (
            await db.get(SourceRevision, (conflict.event.event_id, 1))
        ).state == "needs_confirmation"
    await meeting_input(service, write, "会议改到下午四点", "4", "plan")
    assert (await current_events(service))[0].statement.valid_time.start == ANCHOR.replace(hour=8)


@pytest.mark.asyncio
async def test_late_time_detail_enriches_coarse_event_without_overriding_newer_reschedule(
    foundation,
):
    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    await meeting_input(service, write, "下午有个会议", "3", "plan")
    await meeting_input(service, write, "下午3.00的会议", "1", "plan")
    original = (await current_events(service))[0]
    assert original.statement.valid_time.start == ANCHOR.replace(hour=7)
    await meeting_input(service, write, "会议改到下午四点", "4", "plan")
    await meeting_input(service, write, "下午三点的会议", "2", "plan")
    records = await current_events(service)
    assert len(records) == 1 and records[0].ref.object_id == original.ref.object_id
    assert records[0].statement.valid_time.start == ANCHOR.replace(hour=8)


@pytest.mark.asyncio
async def test_point_event_can_be_recalled_before_and_after_its_time(foundation):
    from test_memory_foundation import tiny_profile

    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    await meeting_input(service, write, "下午有个会议", "1", "plan")
    await meeting_input(service, write, "下午三点有个会议", "2", "plan")
    for anchor in [ANCHOR, ANCHOR + timedelta(hours=8)]:
        service.clock = lambda anchor=anchor: anchor
        service.executor.clock = service.clock
        request = RetrievalInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            queries=[
                QuerySpec(
                    query_id="q",
                    text="会议几点开始",
                    route="lexical",
                    entity_ids=[],
                    purpose="answer",
                )
            ],
            profile=tiny_profile(),
            budget=Budget(),
            include_historical=False,
        )
        recalled = await service.retrieve(request)
        assert recalled.status == "ok" and len(recalled.data.candidates) == 1
        assert recalled.data.candidates[0].record.statement.valid_time.precision == "instant"


@pytest.mark.asyncio
async def test_distinct_explicit_meeting_topics_do_not_merge(foundation):
    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    await meeting_input(service, write, "下午有个客户会议", "1", "plan")
    await meeting_input(service, write, "下午三点的项目会议", "2", "plan")
    assert len(await current_events(service)) == 2


@pytest.mark.asyncio
async def test_prior_partial_records_are_consolidated_by_evidenced_commit(foundation):
    from avatarweave_agent.memory.contracts import TemporalInput
    from avatarweave_agent.memory.lifecycle import temporal

    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    coarse = utterance(write, "下午开会", "2")
    await service.input_event(coarse)
    original, _ = await plan_commit(
        service, coarse, candidate_for(coarse, predicate="important_event", value="下午开会")
    )
    precise = utterance(write, "下午三点有个会议", "1")
    await service.input_event(precise)
    candidate = candidate_for(precise, predicate="important_event", value="会议")
    candidate = temporal(
        TemporalInput(
            meta=meta(service),
            scope=write.scope,
            fence=write.fence,
            candidates=[candidate],
            user_timezone="Asia/Shanghai",
            anchor_event=precise.event,
        )
    ).candidates[0]
    partial, _ = await plan_commit(service, precise, candidate)
    assert partial.status == "ok" and len(await current_events(service)) == 2
    linked = await service.link_event_update(write.scope, precise.event, candidate)
    assert not linked.requires_confirmation and len(linked.candidate.sources) == 2
    consolidated, _ = await plan_commit(service, precise, linked.candidate)
    assert consolidated.status == "ok", consolidated
    records = await current_events(service)
    assert len(records) == 1 and records[0].ref.object_id == original.data.committed[0].object_id
    assert records[0].statement.valid_time.start == ANCHOR.replace(hour=7)
    assert {p.event_id for p in records[0].sources} == {
        coarse.event.event_id,
        precise.event.event_id,
    }
    assert {r.object_id for r in consolidated.data.superseded} == {
        original.data.committed[0].object_id,
        partial.data.committed[0].object_id,
    }
    retry, _ = await plan_commit(service, precise, linked.candidate)
    assert retry.data.committed == [records[0].ref]


@pytest.mark.asyncio
@pytest.mark.parametrize("which_source", [0, 1])
async def test_revising_either_source_invalidates_refined_event(foundation, which_source):
    from avatarweave_agent.memory.contracts import VersionRef

    service = foundation
    service.clock = lambda: ANCHOR
    service.executor.clock = service.clock
    write = await event_request(service, await account(service))
    sources = [
        await meeting_input(service, write, "下午有个会议", "1", "plan"),
        await meeting_input(service, write, "下午三点的会议", "2", "plan"),
    ]
    original = sources[which_source]
    revised = original.model_copy(
        update={
            "expected_turn_revision": 1,
            "event": original.event.model_copy(
                update={
                    "revision": 2,
                    "content": "这句转写错误",
                    "supersedes": VersionRef(object_id=original.event.event_id, version=1),
                }
            ),
        }
    )
    assert (await service.input_event(revised)).status == "ok"
    assert not await current_events(service)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_invalid", ["value", "schema", "source"])
async def test_gateway_repairs_once_without_accepting_fabricated_evidence(
    foundation, first_invalid
):
    service = foundation
    write = await event_request(service, await account(service))
    candidate = candidate_for(write, predicate="preference", value="茶")
    output = AdmissionOutput(candidates=[candidate], decisions=[], input_watermark=0)
    broken = output.model_dump(mode="json")
    if first_invalid == "value":
        broken["candidates"][0]["statement"]["canonical_value"] = "咖啡"
    elif first_invalid == "source":
        broken["candidates"][0]["sources"][0]["event_id"] = "forged"
    else:
        broken["candidates"][0]["interpretation"] = "unsupported"
    calls = []
    always_invalid = False

    def response(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(broken)
                            if len(calls) == 1 or always_invalid
                            else output.model_dump_json()
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    gateway = ModelGateway(
        base_url="https://model.test/v1",
        api_key="test",
        model="test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    try:
        request = AdmissionInput(
            meta=meta(service, lane="maintenance", budget_ms=1000),
            scope=write.scope,
            fence=write.fence,
            events=[write.event],
            memory_enabled=True,
            extraction_prompt_version="memory-extract.v1",
        )
        assert (await gateway.extract(request)).candidates[0].statement.canonical_value == "茶"
        assert len(calls) == 2 and len(calls[1]["messages"]) == len(calls[0]["messages"]) + 1
        # A second invalid result must fail rather than consume unbounded calls.
        calls.clear()
        always_invalid = True
        with pytest.raises(MemoryFault):
            await gateway.extract(request)
        assert len(calls) == 2
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_background_name_commit_during_context_does_not_cancel_current_intro(foundation):
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
    original_context = service.context

    class Extractor:
        async def extract(self, request):
            return literal_self_name(request)

    async def commit_before_context(request):
        assert await service.next_job("background", Extractor())
        return await original_context(request)

    service.context = commit_before_context
    for turn in ["intro-first", "intro-repeat"]:
        prepared = await private.prepare(turn, "我叫林小舟", origin="user_text")
        assert prepared.blocks[-1].text == "我叫林小舟"
        assert not any(block.kind == "memory" for block in prepared.blocks)
        await private.check()
    assert len(await current_events(service)) == 1


@pytest.mark.asyncio
async def test_background_meeting_refinement_does_not_invalidate_current_clarification(foundation):
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
    await private.prepare("coarse", "下午有个会议", origin="user_text")
    assert await service.next_job("background", MeetingExtractor("plan"))
    original_retrieve = service.retrieve

    async def commit_after_recall(request, embedding_gateway=None):
        result = await original_retrieve(request, embedding_gateway)
        assert result.status == "ok" and result.data.candidates
        assert embedding_gateway is None
        assert await service.next_job("background", MeetingExtractor("plan"))
        return result

    service.retrieve = commit_after_recall
    prepared = await private.prepare("detail", "下午三点有个会议", origin="user_text")
    assert not any(block.kind == "memory" for block in prepared.blocks)
    assert (await current_events(service))[0].statement.valid_time.precision == "instant"
    await private.check()


@pytest.mark.asyncio
async def test_new_login_and_session_recalls_name_for_natural_question_and_opt_out_is_respected(
    foundation,
):
    service = foundation
    scope = await account(service, "stable-user")
    write = await event_request(service, scope)
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我叫林小舟"})}
    )
    assert (await service.input_event(write)).status == "ok"
    saved, _ = await plan_commit(
        service, write, candidate_for(write, predicate="name", value="林小舟")
    )
    assert saved.status == "ok", saved
    assert await account(service, "stable-user") == scope
    persona = PersonaCard(
        persona_id="companion",
        version=1,
        authored_identity="中文助手",
        behavior_boundaries=["不编造"],
        preferred_style=[],
        lore_refs=[],
        approved_at=service.clock(),
    )
    new_session, _ = await session(service, scope)
    conversation = await PrivateConversation.bind(
        service, new_session.session_id, new_session.room_name, "new-worker", persona=persona
    )
    prepared = await conversation.prepare("new-turn", "还记得我吗？")
    memories = [b for b in prepared.blocks if b.kind == "memory"]
    assert any("林小舟" in b.text for b in memories)
    assert any("predicate=name" in b.text and "subject=user_self" in b.text for b in memories)
    assert all(not b.trusted_instruction for b in memories)
    # End the first active worker before opening a temporary conversation.
    await service.release_owner(scope, write.session_id, "worker", write.fence.owner_epoch)
    temporary, _ = await session(service, scope, enabled=False)
    no_memory = await PrivateConversation.bind(
        service, temporary.session_id, temporary.room_name, "temporary-worker", persona=persona
    )
    assert not any(
        b.kind == "memory" for b in (await no_memory.prepare("temp", "还记得我吗？")).blocks
    )


@pytest.mark.asyncio
async def test_self_profile_does_not_enter_third_person_clarification_or_other_scope(foundation):
    from test_memory_foundation import tiny_profile

    service = foundation
    write = await event_request(service, await account(service))
    write = write.model_copy(
        update={"event": write.event.model_copy(update={"content": "我叫林小舟"})}
    )
    await service.input_event(write)
    await plan_commit(service, write, candidate_for(write, predicate="name", value="林小舟"))
    request = RetrievalInput(
        meta=meta(service),
        scope=write.scope,
        fence=write.fence,
        queries=[
            QuerySpec(query_id="q", text="你好", route="lexical", entity_ids=[], purpose="answer")
        ],
        profile=tiny_profile(),
        budget=Budget(),
        include_historical=False,
    )
    assert len((await service.retrieve(request)).data.candidates) == 1
    for query in [
        request.queries[0].model_copy(update={"entity_ids": ["third-person"]}),
        request.queries[0].model_copy(update={"purpose": "clarify"}),
    ]:
        assert not (
            await service.retrieve(request.model_copy(update={"queries": [query]}))
        ).data.candidates
    for scope in [await account(service), write.scope.model_copy(update={"companion_id": "other"})]:
        assert not (
            await service.retrieve(request.model_copy(update={"scope": scope}))
        ).data.candidates
