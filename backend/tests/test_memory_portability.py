"""Actual graph loss/restore on both database backends, not a re-attestation mock."""
# ruff: noqa: F811 -- pytest imports and injects shared fixtures by name

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from test_memory_foundation import (  # noqa: F401 -- shared fixtures
    account,
    candidate_for,
    encrypted_store,
    event_request,
    foundation,
    plan_commit,
)

from avatarweave_agent.memory.contracts import (
    CompressionInput,
    CompressionPolicy,
    CreateExportBody,
    EntityCandidate,
    ExportInput,
    Hypothesis,
    PageRequest,
    PersonaCard,
    ReflectionOutput,
    TimeWindow,
    VersionRef,
)
from avatarweave_agent.memory.facts import load_record
from avatarweave_agent.memory.http import meta
from avatarweave_agent.memory.portability import ExportManifest
from avatarweave_agent.memory.portable_graph import (
    PortableBundle,
    inventory_for,
    mapping_key,
    read_package,
    validate_graph,
)
from avatarweave_agent.memory.storage import (
    Account,
    EventHead,
    MemorySession,
    Outbox,
    SessionReceipt,
    SourceRevision,
)
from avatarweave_agent.memory.tables import (
    Capsule,
    Dependency,
    DerivedView,
    Entity,
    FactKey,
    FactSource,
    FactVersion,
    ImportMapping,
    ObjectManifest,
    Tombstone,
)


async def snapshot(service, store):
    write = await event_request(service, await account(service))
    for year, value in [(2020, "北京"), (2022, "上海")]:
        event = write.event.model_copy(
            update={
                "event_id": uuid4().hex,
                "turn_id": uuid4().hex,
                "content": f"我从{year}年起住在{value}",
            }
        )
        request = write.model_copy(update={"event": event})
        await service.input_event(request)
        result, _ = await plan_commit(
            service,
            request,
            candidate_for(
                request, value=value, start=service.clock().replace(year=year, month=1, day=1)
            ),
        )
        assert result.status == "ok", result
    event = write.event.model_copy(
        update={"event_id": uuid4().hex, "turn_id": uuid4().hex, "content": "我妈妈李云住在杭州"}
    )
    request = write.model_copy(update={"event": event})
    await service.input_event(request)
    entity = EntityCandidate(
        entity_id="mother",
        entity_type="person",
        canonical_name="李云",
        aliases=["妈妈"],
        context_refs=[VersionRef(object_id=event.event_id, version=1)],
    )
    async with service.database.sessions.begin() as db:
        db.add(
            Entity(
                id=entity.entity_id,
                account_id=write.scope.user_id,
                companion_id="companion",
                entity_json=entity.model_dump_json(),
            )
        )
    candidate = candidate_for(request, value="杭州")
    candidate = candidate.model_copy(
        update={
            "statement": candidate.statement.model_copy(update={"subject_id": entity.entity_id})
        }
    )
    assert (await plan_commit(service, request, candidate))[0].status == "ok"
    event = write.event.model_copy(
        update={"event_id": uuid4().hex, "turn_id": uuid4().hex, "content": "我计划参加面试"}
    )
    request = write.model_copy(update={"event": event})
    await service.input_event(request)
    assert (
        await plan_commit(
            service,
            request,
            candidate_for(request, predicate="plan", value="参加面试").model_copy(
                update={"content_kind": "future_intent"}
            ),
        )
    )[0].status == "ok"
    await service.register_persona(
        write.scope,
        PersonaCard(
            persona_id="companion",
            version=1,
            authored_identity="温和的陪伴者",
            behavior_boundaries=["不捏造经历"],
            preferred_style=[],
            lore_refs=[],
            approved_at=service.clock(),
        ),
    )
    async with service.database.sessions() as db:
        records = [
            load_record(r)
            for r in await db.scalars(
                select(FactVersion).where(
                    FactVersion.account_id == write.scope.user_id,
                    FactVersion.system_to.is_(None),
                    FactVersion.status == "active",
                )
            )
        ]
    summary = await service.compression(
        CompressionInput(
            meta=meta(service, lane="maintenance", budget_ms=5000),
            scope=write.scope,
            fence=write.fence,
            source_records=records,
            previous_summaries=[],
            level="life_chapter",
            period=TimeWindow(precision="unknown"),
            mandatory_refs=[r.ref for r in records],
            policy=CompressionPolicy(target_tokens=6000, verifier_version="extractive.v1"),
        )
    )
    assert summary.data.accepted, summary
    await service.persist_reflections(
        request,
        ReflectionOutput(
            hypotheses=[
                Hypothesis(
                    text="这可能是一个需要日后确认的生活阶段",
                    supports=[record.ref for record in records],
                    counters=[],
                    scope_conditions=[],
                    review_after=service.clock() + timedelta(days=30),
                    requires_confirmation=True,
                    status="candidate",
                )
            ],
            rejected_reasons=[],
        ),
    )
    body = CreateExportBody(
        include_source_capsules=True,
        include_raw_transcript=True,
        confirmed=True,
        idempotency_key="graph-export",
    )
    control, _ = await service.control_event(
        write.scope, key=body.idempotency_key, operation="export_control", body=body, text="export"
    )
    request = ExportInput(
        meta=meta(service, lane="deep_recall", budget_ms=10000),
        scope=write.scope,
        fence=write.fence,
        operation="create",
        export_id=None,
        selected_namespaces=["life", "relationship", "character_lore"],
        include_source_capsules=True,
        include_raw_transcript=True,
        explicit_user_confirmation_event=control.event_id,
    )
    result = await service.export(request, store)
    assert result.status == "ok", result
    async with service.database.sessions() as db:
        catalog = await db.get(ObjectManifest, result.data.export_id)
        manifest = ExportManifest.model_validate_json(catalog.manifest_json)
    package = read_package(
        await store.read(
            "test", manifest.object_ref, manifest.encryption_key_version, 8 * 1024 * 1024
        )
    )
    assert isinstance(package, PortableBundle)
    return request.model_copy(update={"export_id": result.data.export_id}), package


@pytest.mark.asyncio
async def test_full_graph_restore_preserves_history_provenance_ttl_and_idempotence(
    foundation, encrypted_store
):
    service = foundation
    request, package = await snapshot(service, encrypted_store)
    assert len(package.bundle.records) == 5  # includes the superseded pre-closure version
    assert package.summaries and package.personas and package.entities and package.open_loops
    assert package.reflections and package.reflections[0].state == "needs_confirmation"
    validate_graph(package, request.scope)
    async with service.database.sessions.begin() as db:
        # Simulate data loss, not a user deletion. Real deletions leave tombstones
        # and must be refused, as the second test verifies.
        for model in [
            FactSource,
            FactVersion,
            FactKey,
            Capsule,
            Dependency,
            DerivedView,
            Entity,
            SourceRevision,
            EventHead,
            SessionReceipt,
            MemorySession,
            Outbox,
        ]:
            if model is FactSource or model is EventHead:
                await db.execute(delete(model))
            else:
                await db.execute(delete(model).where(model.account_id == request.scope.user_id))
        row = await db.get(Account, request.scope.user_id)
        row.structured_watermark = row.index_watermark = row.source_watermark
    control, _ = await service.control_event(
        request.scope,
        key="graph-import",
        operation="import_control",
        body=CreateExportBody(
            include_source_capsules=True,
            include_raw_transcript=True,
            confirmed=True,
            idempotency_key="graph-import",
        ),
        text="import",
    )
    request = request.model_copy(
        update={
            "explicit_user_confirmation_event": control.event_id,
            "operation": "import_dry_run",
            "meta": meta(service, lane="deep_recall", budget_ms=10000),
        }
    )
    preflight = await service.export(request, encrypted_store)
    assert preflight.data.state == "verified", preflight
    apply = request.model_copy(
        update={
            "operation": "import_apply",
            "meta": meta(service, lane="deep_recall", budget_ms=10000),
        }
    )
    restored = await service.export(apply, encrypted_store)
    assert restored.status == "ok", restored
    assert restored.data.state == "imported", restored
    async with service.database.sessions() as db:
        facts = list(
            await db.scalars(
                select(FactVersion).where(FactVersion.account_id == request.scope.user_id)
            )
        )
        assert len(facts) == len(package.bundle.records)
        assert {r.memory_id for r in facts}.isdisjoint(
            {r.ref.object_id for r in package.bundle.records}
        )
        assert sorted(
            (r.version, r.system_to is None, load_record(r).statement.canonical_value)
            for r in facts
        ) == sorted(
            (r.ref.version, r.system_to is None, r.statement.canonical_value)
            for r in package.bundle.records
        )
        assert all(load_record(r).assertion_kind == "user_report" for r in facts)
        summaries = list(await db.scalars(select(DerivedView).where(DerivedView.kind == "summary")))
        assert len(summaries) == 1 and summaries[0].state == "valid"
        assert await db.scalar(select(func.count()).select_from(Dependency)) >= 5
        assert (await db.scalar(select(Entity))).id != "mother"
        from avatarweave_agent.memory.tables import EntityAlias

        assert await db.scalar(select(EntityAlias.entity_id).where(EntityAlias.alias == "妈妈"))
        reflection = await db.scalar(select(DerivedView).where(DerivedView.kind == "reflection"))
        assert reflection.id != package.reflections[0].ref.object_id
        assert reflection.state == "pending_review"
        for source in package.sources:
            mapped = await db.get(
                ImportMapping,
                (
                    request.scope.user_id,
                    request.export_id,
                    mapping_key("source", source.descriptor.event_id),
                    source.descriptor.revision,
                ),
            )
            actual = await db.get(SourceRevision, (mapped.new_id, mapped.new_version))
            assert actual and actual.expires_at.replace(tzinfo=source.expires_at.tzinfo) == min(
                source.expires_at, source.descriptor.occurred_at + timedelta(days=90)
            )
    page = await service.list_reflections(request.scope, PageRequest())
    assert page.items[0].state == "needs_confirmation"
    again = await service.export(
        apply.model_copy(update={"meta": meta(service, lane="deep_recall", budget_ms=10000)}),
        encrypted_store,
    )
    assert again.data.state == "imported", again
    async with service.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(FactVersion)) == len(
            package.bundle.records
        )


@pytest.mark.asyncio
async def test_graph_inventory_dag_scope_and_prior_import_deletion_are_checked(
    foundation, encrypted_store
):
    from avatarweave_agent.memory.execution import MemoryFault

    service = foundation
    request, package = await snapshot(service, encrypted_store)
    broken = package.model_copy(
        update={"bundle": package.bundle.model_copy(update={"capsules": []})}
    )
    with pytest.raises(MemoryFault):
        validate_graph(broken, request.scope)
    cyclic = package.summaries[0].model_copy(update={"child_refs": [package.summaries[0].ref]})
    broken = package.model_copy(update={"summaries": [cyclic]})
    broken = broken.model_copy(update={"inventory": inventory_for(broken)})
    with pytest.raises(MemoryFault):
        validate_graph(broken, request.scope)
    async with service.database.sessions.begin() as db:
        # A previous upload used a different handle. Its deletion still wins.
        old = package.bundle.records[0]
        db.add(
            ImportMapping(
                account_id=request.scope.user_id,
                export_id="earlier-upload",
                old_id=mapping_key("memory", old.ref.object_id),
                old_version=old.ref.version,
                new_id="previously-imported-and-deleted",
                new_version=old.ref.version,
            )
        )
        db.add(
            Tombstone(
                account_id=request.scope.user_id,
                object_id="previously-imported-and-deleted",
                kind="memory",
                privacy_epoch=1,
                created_at=service.clock(),
            )
        )
    async with service.database.sessions() as db:
        assert any(
            "Deleted" in message for message in await service.graph_conflicts(db, request, package)
        )
