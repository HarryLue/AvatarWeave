"""M06-M10 deterministic plans, source-anchored summaries and revocable hypotheses."""

import hashlib
from datetime import timedelta

from sqlalchemy import and_, select

from .context import token_bound
from .contracts import (
    CompressionOutput,
    DecayOutput,
    DecayScore,
    ForgetAction,
    ForgetOutput,
    ModuleResult,
    ReflectionOutput,
    SummaryClaim,
    SummaryNode,
    TieringOutput,
    TierMove,
    VersionRef,
)
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .storage import SourceRevision
from .tables import Dependency, DerivedView, FactSource, FactVersion


class MaintenanceMixin:
    async def _current_records(self, db, request, supplied):
        account = await self._account(db, request.scope)
        self._privacy(account, request)
        from .job_context import verify_job_lease

        await verify_job_lease(db, account.id, self.clock())
        verified = []
        for record in supplied:
            row = await db.scalar(
                select(FactVersion).where(
                    *fact_scope(request.scope),
                    FactVersion.memory_id == record.ref.object_id,
                    FactVersion.version == record.ref.version,
                    FactVersion.system_to.is_(None),
                    FactVersion.status.in_(["active", "uncertain"]),
                )
            )
            actual = load_record(row) if row else None
            if actual is None or actual.model_copy(update={"tier": record.tier}) != record:
                raise MemoryFault(
                    "version_conflict", "Maintenance input is not the current authorized snapshot"
                )
            verified.append(actual)
        return account, verified

    async def tiering(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account, records = await self._current_records(db, request, request.records)
                if request.policy.hot_days >= request.policy.warm_days:
                    raise MemoryFault("invalid_transition", "Tier boundaries are not ordered")
                moves, retained = [], []
                confirmed = {(r.object_id, r.version) for r in request.confirmed_recall_refs}
                for record in records:
                    age = max(0, (request.meta.now - record.mentioned_at).total_seconds() / 86400)
                    protected = record.user_pinned or record.content_kind == "future_intent"
                    target = (
                        "hot"
                        if protected or age <= request.policy.hot_days
                        else "warm"
                        if age <= request.policy.warm_days
                        else "cold"
                    )
                    if (record.ref.object_id, record.ref.version) in confirmed:
                        target = "hot"
                    if target != record.tier and len(moves) < request.policy.max_moves_per_job:
                        moves.append(
                            TierMove(
                                ref=record.ref,
                                from_tier=record.tier,
                                to_tier=target,
                                reason="user_pin"
                                if record.user_pinned
                                else "open_loop"
                                if protected
                                else "verified_recall"
                                if (record.ref.object_id, record.ref.version) in confirmed
                                else "age",
                                requires_verified_archive=target == "cold",
                            )
                        )
                    else:
                        retained.append(record.ref)
                return ModuleResult(
                    status="ok",
                    data=TieringOutput(moves=moves, retained_refs=retained),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def decay(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account, records = await self._current_records(db, request, request.records)
                scores = []
                for record in records:
                    confirmations = set(
                        (
                            await db.execute(
                                select(SourceRevision.session_id)
                                .join(
                                    FactSource,
                                    and_(
                                        FactSource.event_id == SourceRevision.event_id,
                                        FactSource.source_revision == SourceRevision.revision,
                                    ),
                                )
                                .where(
                                    FactSource.memory_id == record.ref.object_id,
                                    FactSource.version == record.ref.version,
                                    SourceRevision.account_id == account.id,
                                    SourceRevision.revoked.is_(False),
                                    SourceRevision.origin.in_(
                                        ["user_text", "user_transcript", "user_ui"]
                                    ),
                                )
                            )
                        ).scalars()
                    )
                    age = max(
                        0,
                        (
                            request.meta.now
                            - (record.last_user_confirmed_at or record.mentioned_at)
                        ).total_seconds()
                        / 86400,
                    )
                    policy = request.policy
                    half = policy.half_life_days * (
                        1
                        + policy.reinforcement_gain
                        * min(len(confirmations), policy.reinforcement_cap)
                    )
                    freshness = (
                        1.0
                        if record.user_pinned
                        else policy.floor + (1 - policy.floor) * 2 ** (-age / half)
                    )
                    scores.append(
                        DecayScore(
                            ref=record.ref,
                            age_days=age,
                            effective_half_life_days=half,
                            freshness=freshness,
                            eligible=record.status == "active",
                            invalid_reason=None
                            if record.status == "active"
                            else "Memory requires confirmation",
                        )
                    )
                return ModuleResult(
                    status="ok",
                    data=DecayOutput(scores=scores),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def compression(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account, records = await self._current_records(db, request, request.source_records)
                revision = account.memory_revision
            if len(records) > request.policy.max_leaf_records:
                raise MemoryFault("budget_exceeded", "Compression leaf batch exceeds the policy")
            requested = {(r.object_id, r.version) for r in request.mandatory_refs}
            by_ref = {(r.ref.object_id, r.ref.version): r for r in records}
            if not requested <= by_ref.keys():
                raise MemoryFault(
                    "evidence_missing", "Mandatory memory is absent from compression input"
                )
            requested |= {
                key
                for key, r in by_ref.items()
                if r.user_pinned
                or r.statement.polarity == "negative"
                or r.content_kind in {"future_intent", "interaction_preference"}
                or r.importance >= 0.8
            }
            selected = [by_ref[key] for key in sorted(requested)]
            used = sum(token_bound(r.display_text) for r in selected)
            reason = None
            if request.policy.minimum_mandatory_coverage != 1.0:
                raise MemoryFault("invalid_transition", "Mandatory coverage cannot be relaxed")
            if len(selected) > 256 or used > request.policy.target_tokens:
                reason = "Protected facts exceed this summary page budget; split pages or retain atomic facts"
            else:
                for record in sorted(
                    records,
                    key=lambda r: r.importance / max(1, token_bound(r.display_text)),
                    reverse=True,
                ):
                    if record in selected or record.status != "active":
                        continue
                    cost = token_bound(record.display_text)
                    if len(selected) < 256 and used + cost <= request.policy.target_tokens:
                        selected.append(record)
                        used += cost
            if reason or not selected:
                output = CompressionOutput(
                    summary=None,
                    accepted=False,
                    mandatory_coverage=0.0 if requested else 1.0,
                    unsupported_claims=[],
                    preserved_refs=[r.ref for r in records],
                    input_tokens=sum(token_bound(r.display_text) for r in records),
                    output_tokens=0,
                    rejection_reason=reason or "No eligible facts",
                )
            else:
                # Extractive claims avoid semantic drift; this baseline does not claim
                # a model paraphrase or a guaranteed compression ratio.
                if any(len(r.display_text) > 2048 for r in selected):
                    raise MemoryFault(
                        "budget_exceeded", "A claim must be split without dropping protected text"
                    )
                summary_id = hashlib.sha256(
                    (request.scope.user_id + request.meta.operation_id).encode()
                ).hexdigest()
                summary = SummaryNode(
                    ref=VersionRef(object_id=summary_id, version=1),
                    scope=request.scope,
                    level=request.level,
                    period=request.period,
                    claims=[
                        SummaryClaim(
                            text=r.display_text,
                            supported_by=[r.ref],
                            evidence_refs=r.sources,
                            preserves_negation=True,
                            valid_time=r.statement.valid_time,
                        )
                        for r in selected
                    ],
                    child_refs=[r.ref for r in selected],
                    leaf_manifest_ref=summary_id + ":leaves",
                    input_watermark=revision,
                    prompt_version="extractive-summary.v1",
                    token_count=used,
                    state="valid",
                )
                if any(len(r.display_text) > 2048 for r in selected):
                    raise MemoryFault(
                        "budget_exceeded", "A claim must be split without dropping protected text"
                    )
                async with self.database.sessions.begin() as db:
                    account, _ = await self._current_records(db, request, records)
                    if account.memory_revision != revision:
                        raise MemoryFault("version_conflict", "Summary input changed")
                    existing = await db.get(DerivedView, (summary_id, 1))
                    if existing:
                        if existing.state != "valid":
                            raise MemoryFault("version_conflict", "Summary was invalidated")
                        prior = SummaryNode.model_validate_json(existing.document_json)
                        if prior.child_refs != summary.child_refs:
                            raise MemoryFault("version_conflict", "Summary operation input changed")
                        summary = prior
                    else:
                        db.add(
                            DerivedView(
                                id=summary.ref.object_id,
                                version=1,
                                account_id=account.id,
                                companion_id=request.scope.companion_id,
                                kind="summary",
                                state="valid",
                                document_json=summary.model_dump_json(),
                            )
                        )
                    for record in [] if existing else selected:
                        db.add(
                            Dependency(
                                account_id=account.id,
                                parent_id=record.ref.object_id,
                                parent_version=record.ref.version,
                                child_id=summary.ref.object_id,
                                child_version=1,
                                relation="summary_of",
                            )
                        )
                output = CompressionOutput(
                    summary=summary,
                    accepted=True,
                    mandatory_coverage=1.0,
                    unsupported_claims=[],
                    preserved_refs=[r.ref for r in selected],
                    input_tokens=sum(token_bound(r.display_text) for r in records),
                    output_tokens=used,
                )
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())

                return ModuleResult(
                    status="ok", data=output, visibility=self._visibility(account, None, [], [])
                )

        return await self.executor.run(request, operation)

    async def reflection(self, request, gateway):
        async def operation():
            import json

            from .contracts import ModelMessage, ModelTaskInput

            async with self.database.sessions() as db:
                account, records = await self._current_records(db, request, request.records)
                independent = set()
                dates = set()
                for record in records:
                    for pointer in record.sources:
                        source = await self._source(
                            db, request.scope, pointer.event_id, pointer.source_revision
                        )
                        independent.add(source.session_id)
                        dates.add(source.occurred_at.date())
                if len(independent) < request.minimum_distinct_sessions or len(dates) < 2:
                    return ModuleResult(
                        status="ok",
                        data=ReflectionOutput(
                            hypotheses=[],
                            rejected_reasons=["Insufficient independent sessions/dates"],
                        ),
                        visibility=self._visibility(account, None, [], []),
                    )
            encoded = json.dumps([r.model_dump(mode="json") for r in records], ensure_ascii=False)
            if len(encoded) > 16000:
                raise MemoryFault(
                    "budget_exceeded", "Reflection input requires another bounded batch"
                )
            result = await gateway.task(
                ModelTaskInput(
                    meta=request.meta,
                    scope=request.scope,
                    fence=request.fence,
                    task="reflect",
                    profile_id="reflection.v1",
                    prompt_version="reflection.v1",
                    output_schema="reflection.v1",
                    messages=[
                        ModelMessage(
                            role="system",
                            content="只提出可撤销假设。不得诊断、贴人格标签或改写角色身份。"
                            "查支持与反例；所有引用必须来自输入。requires_confirmation=true，status=needs_confirmation。",
                        ),
                        ModelMessage(
                            role="user",
                            content=encoded,
                        ),
                    ],
                    max_output_tokens=2000,
                )
            )
            hypotheses, rejected = [], []
            legal = {(r.ref.object_id, r.ref.version) for r in records}
            for value in result.payload.hypotheses[: request.max_hypotheses]:
                if not {(r.object_id, r.version) for r in value.supports + value.counters} <= legal:
                    raise MemoryFault(
                        "evidence_missing", "Reflection referenced unprovided evidence"
                    )
                if value.counters:
                    rejected.append("Counter-evidence requires review")
                value = value.model_copy(
                    update={
                        "requires_confirmation": True,
                        "status": "needs_confirmation",
                        "review_after": request.meta.now + timedelta(days=30),
                    }
                )
                hypotheses.append(value)
            hypotheses = await self.persist_reflections(
                request, ReflectionOutput(hypotheses=hypotheses, rejected_reasons=rejected)
            )
            async with self.database.sessions() as db:
                account, _ = await self._current_records(db, request, records)
                return ModuleResult(
                    status="ok",
                    data=ReflectionOutput(hypotheses=hypotheses, rejected_reasons=rejected),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def forget(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account, records = await self._current_records(db, request, request.records)
                explicit = {(r.object_id, r.version) for r in request.explicit_delete_refs}
                actions = []
                for record in records:
                    ref = (record.ref.object_id, record.ref.version)
                    action = (
                        "delete_by_user"
                        if ref in explicit
                        else "expire"
                        if record.expires_at and record.expires_at <= request.meta.now
                        else "retain"
                    )
                    actions.append(
                        ForgetAction(
                            ref=record.ref,
                            action=action,
                            reason="Explicit control requires M19"
                            if ref in explicit
                            else "Retention and truth are separate from priority",
                            legal_or_user_basis_ref=request.meta.operation_id
                            if ref in explicit
                            else None,
                            source_granularity_after="none"
                            if ref in explicit
                            else "evidence_capsule",
                        )
                    )
                return ModuleResult(
                    status="ok",
                    data=ForgetOutput(
                        actions=actions,
                        requires_deletion_job=bool(explicit or request.expired_source_ids),
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)
