"""M11/M12 bounded hybrid recall with RRF, explicit time and evidence gates."""

import asyncio
import json
import re
from collections import defaultdict

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from .contracts import (
    CandidateRank,
    EmbeddingInput,
    ModuleResult,
    QueryOutput,
    QuerySpec,
    RetrievalOutput,
    RetrievedMemory,
    VersionRef,
)
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .lifecycle import overlaps, resolve_time
from .storage import SourceRevision
from .tables import Capsule, FactVersion, VectorEntry
from .utils import utc


def lexemes(text):
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]{2,}", lowered)
    for segment in re.findall(r"[\u3400-\u9fff]+", lowered):
        words.extend(segment[i : i + 2] for i in range(len(segment) - 1))
    return list(dict.fromkeys(words))[:32]


def recall_categories(text):
    """Server-owned life intents, including questions without shared fact words."""
    categories = set()
    for words, predicates in [
        (("喜欢", "偏好", "口味", "讨厌", "爱好"), {"preference"}),
        (("重要的人", "家人", "朋友", "人物", "人际关系"), {"relationship"}),
        (("人生", "时间线", "经历", "往事", "发生过"), {"important_event"}),
        (("计划", "打算", "约定", "安排"), {"plan"}),
    ]:
        if any(word in text for word in words):
            categories.update(predicates)
    if any(
        word in text for word in ("回忆一下", "记得什么", "记住了什么", "了解我", "关于我的记忆")
    ):
        categories.update(
            {
                "name",
                "current_city",
                "occupation",
                "preference",
                "relationship",
                "important_event",
                "plan",
            }
        )
    return categories


def plan_queries(request):
    text = request.latest_event.content
    queries = [
        QuerySpec(
            query_id="latest",
            text=text[:2048],
            route="lexical",
            entity_ids=request.known_entity_ids[:16],
            purpose="reminisce" if recall_categories(text) & {"important_event"} else "answer",
        )
    ]
    if len(recall_categories(text)) > 1 and request.known_entity_ids:
        queries[0] = queries[0].model_copy(update={"entity_ids": []})
        if len(queries) < request.budget.max_queries:
            queries.append(
                QuerySpec(
                    query_id="people",
                    text=text[:2048],
                    route="entity",
                    entity_ids=request.known_entity_ids[:16],
                    purpose="answer",
                )
            )
    if request.recent_topic and len(queries) < request.budget.max_queries:
        queries.append(
            QuerySpec(
                query_id="continuity",
                text=request.recent_topic,
                route="entity",
                entity_ids=request.known_entity_ids[:16],
                purpose="continuity",
            )
        )
    match = re.search(
        r"\d{4}年(?:到|至|—|-)\d{4}年|\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?|(?:\d{1,3}|[一二两三四五六七八九十]{1,3})年前|(?:去年|前年|今年)(?:\d{1,2}月)?|上个月|这个月|本月|(?:上周|本周|下周)[一二三四五六日天]?|昨天|前天",
        text,
    )
    if match:
        period, _, _ = resolve_time(match[0], request.latest_event.occurred_at, "Asia/Shanghai")
        if period.start:
            queries[0] = queries[0].model_copy(
                update={"route": "temporal", "time_filter": period, "purpose": "reminisce"}
            )
    return QueryOutput(
        queries=queries,
        ambiguous=len(text) > 2048,
        clarification="请缩小需要回顾的范围。" if len(text) > 2048 else None,
        planner_version="rules.v1",
    )


class RecallMixin:
    async def query(self, request):
        async def operation():
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                if request.latest_event.scope != request.scope:
                    raise MemoryFault("scope_mismatch", "Query input crosses scope")
                from .lifecycle import normalized
                from .tables import EntityAlias

                by_mention = defaultdict(set)
                text = normalized(request.latest_event.content)
                contains = (
                    func.strpos(text, EntityAlias.alias)
                    if self.database.engine.dialect.name == "postgresql"
                    else func.instr(text, EntityAlias.alias)
                )
                alias_query = select(EntityAlias.entity_id).where(
                    EntityAlias.account_id == account.id,
                    EntityAlias.companion_id == request.scope.companion_id,
                    contains > 0,
                )
                entities = list(await db.scalars(alias_query.distinct().limit(21)))
                aliases = (
                    await db.execute(
                        select(EntityAlias.alias, EntityAlias.entity_id).where(
                            EntityAlias.account_id == account.id,
                            EntityAlias.companion_id == request.scope.companion_id,
                            EntityAlias.entity_id.in_(entities[:20]),
                            contains > 0,
                        )
                    )
                ).all()
                for alias, entity_id in aliases:
                    by_mention[alias].add(entity_id)
                by_mention = {
                    alias: ids
                    for alias, ids in by_mention.items()
                    if not any(alias != other and alias in other for other in by_mention)
                }
                ambiguous = len(entities) > 20 or any(len(ids) > 1 for ids in by_mention.values())
                resolved = sorted(
                    {value for ids in by_mention.values() if len(ids) == 1 for value in ids}
                )
                planned = plan_queries(request.model_copy(update={"known_entity_ids": resolved}))
                if ambiguous:
                    planned = planned.model_copy(
                        update={
                            "ambiguous": True,
                            "clarification": "这个称呼对应多个人，请先确认具体是谁。",
                            "queries": [
                                q.model_copy(update={"purpose": "clarify"}) for q in planned.queries
                            ],
                        }
                    )
                return ModuleResult[QueryOutput](
                    status="ok",
                    data=planned,
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def retrieve(self, request, embedding_gateway=None):
        async def operation():
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                revision = account.memory_revision
            base = select(FactVersion).where(
                *fact_scope(request.scope),
                FactVersion.record_json.is_not(None),
                FactVersion.status.in_(["active", "superseded"])
                if request.read_system_as_of
                else FactVersion.status == "active",
            )
            if request.read_system_as_of:
                base = base.where(
                    FactVersion.system_from <= request.read_system_as_of,
                    or_(
                        FactVersion.system_to.is_(None),
                        FactVersion.system_to > request.read_system_as_of,
                    ),
                )
            else:
                base = base.where(FactVersion.system_to.is_(None))
            if not request.include_historical:
                base = base.where(
                    or_(
                        FactVersion.predicate.in_(["plan", "important_event"]),
                        and_(
                            or_(
                                FactVersion.valid_start.is_(None),
                                FactVersion.valid_start <= self.clock(),
                            ),
                            or_(
                                FactVersion.valid_end.is_(None),
                                FactVersion.valid_end > self.clock(),
                            ),
                        ),
                    ),
                )
            ranked, records, failures = defaultdict(list), {}, []
            participating = set()
            profile_keys = set()

            # Independent branches have their own DB session. At most two branches
            # run here; per-query SQL in each branch is sequential and bounded.
            async def exact_branch():
                async with self.database.sessions() as db:
                    # The account's unique current self-name is useful across
                    # sessions even for "还记得我吗". Resolve it deterministically,
                    # with the same scope, privacy and evidence gates as other hits.
                    profile_query = next(
                        (
                            q
                            for q in request.queries
                            if not q.entity_ids and not q.time_filter and q.purpose != "clarify"
                        ),
                        None,
                    )
                    if (
                        profile_query
                        and not request.include_historical
                        and not request.read_system_as_of
                    ):
                        names = list(
                            await db.scalars(
                                base.where(
                                    FactVersion.subject_id == request.scope.user_id,
                                    FactVersion.predicate == "name",
                                ).limit(3)
                            )
                        )
                        if len(names) == 1:
                            record = load_record(names[0])
                            if (
                                record.namespace == "life"
                                and record.statement.polarity == "positive"
                            ):
                                key = (record.ref.object_id, record.ref.version)
                                records[key] = record
                                profile_keys.add(key)
                                ranked[key].append(
                                    CandidateRank(
                                        ref=record.ref,
                                        route="lexical",
                                        query_id=profile_query.query_id,
                                        rank=1,
                                        raw_score=1.0,
                                        summary_path=[],
                                    )
                                )
                                participating.add("lexical")
                    for query in request.queries:
                        terms = lexemes(query.text)
                        filters = [
                            FactVersion.record_json.contains(term, autoescape=True)
                            for term in terms[:16]
                        ]
                        predicates = {"interaction_boundary"} | recall_categories(query.text)
                        if any(word in query.text for word in ("名字", "叫什么", "称呼", "我是谁")):
                            predicates.add("name")
                        if any(word in query.text for word in ("喜欢", "偏好", "口味")):
                            predicates.add("preference")
                        if any(word in query.text for word in ("住哪", "住在", "哪个城市", "现居")):
                            predicates.add("current_city")
                        if any(word in query.text for word in ("职业", "工作", "做什么")):
                            predicates.add("occupation")
                        filters.append(FactVersion.predicate.in_(predicates))
                        route = "lexical"
                        if query.time_filter:
                            route = "temporal"
                            if query.time_filter.start:
                                filters = [
                                    or_(
                                        FactVersion.valid_end.is_(None),
                                        FactVersion.valid_end >= query.time_filter.start,
                                    )
                                ]
                            if query.time_filter.end:
                                filters.append(
                                    or_(
                                        FactVersion.valid_start.is_(None),
                                        FactVersion.valid_start <= query.time_filter.end,
                                    )
                                )
                        elif query.route == "entity" and query.entity_ids:
                            route, filters = (
                                "entity",
                                [FactVersion.subject_id.in_(query.entity_ids)],
                            )
                        if not filters:
                            continue
                        scoped_query = base
                        if query.entity_ids:
                            scoped_query = scoped_query.where(
                                FactVersion.subject_id.in_(query.entity_ids)
                            )
                        selected = list(
                            await db.scalars(
                                scoped_query.where(
                                    and_(*filters) if route == "temporal" else or_(*filters)
                                )
                                .order_by(FactVersion.system_from.desc())
                                .limit(request.budget.max_candidates)
                            )
                        )
                        categories = recall_categories(query.text)
                        if len(categories) > 1 and route == "lexical":
                            # Reserve bounded candidates for each life category before
                            # ranking: a busy recent topic must not hide older people/events.
                            selected = []
                            per_category = max(1, request.budget.max_candidates // len(categories))
                            for predicate in sorted(categories):
                                selected.extend(
                                    await db.scalars(
                                        scoped_query.where(FactVersion.predicate == predicate)
                                        .order_by(FactVersion.system_from.desc())
                                        .limit(per_category)
                                    )
                                )
                            selected = selected[: request.budget.max_candidates]
                        scored = []
                        for row in selected:
                            record = load_record(row)
                            if query.time_filter and not overlaps(
                                record.statement.valid_time, query.time_filter
                            ):
                                continue
                            score = sum(
                                term in record.display_text.lower()
                                or term in record.statement.canonical_value.lower()
                                for term in terms
                            ) / max(1, len(terms))
                            if (
                                route == "temporal"
                                or route == "entity"
                                or score > 0
                                or row.predicate in predicates
                            ):
                                scored.append((score, record))
                        participating.add(route)
                        ordered = sorted(scored, key=lambda x: x[0], reverse=True)
                        if len(categories) > 1:
                            first, rest, seen = [], [], set()
                            for item in ordered:
                                predicate = item[1].statement.predicate
                                if predicate not in seen:
                                    first.append(item)
                                    seen.add(predicate)
                                else:
                                    rest.append(item)
                            ordered = first + rest
                        for rank, (score, record) in enumerate(ordered[:20], 1):
                            key = (record.ref.object_id, record.ref.version)
                            records[key] = record
                            ranked[key].append(
                                CandidateRank(
                                    ref=record.ref,
                                    route=route,
                                    query_id=query.query_id,
                                    rank=rank,
                                    raw_score=float(score),
                                    summary_path=[],
                                )
                            )

            async def semantic_branch():
                if embedding_gateway is None:
                    return
                embedding_request = EmbeddingInput(
                    meta=request.meta,
                    scope=request.scope,
                    fence=request.fence,
                    profile=request.profile,
                    purpose="query",
                    items=[
                        (VersionRef(object_id=q.query_id, version=1), q.text)
                        for q in request.queries
                    ],
                )
                try:
                    async with asyncio.timeout(
                        0.12 if request.meta.execution.lane == "dialogue" else 2
                    ):
                        embedded = await embedding_gateway.embed(embedding_request)
                except TimeoutError:
                    from .contracts import ErrorInfo

                    failures.append(
                        ErrorInfo(
                            code="deadline_exceeded",
                            message="Semantic route exceeded its budget",
                            retryable=True,
                        )
                    )
                    return
                except MemoryFault as exc:
                    failures.append(exc.info)
                    return
                async with self.database.sessions() as db:
                    for query, vector in zip(request.queries, embedded.items, strict=True):
                        vectors = select(VectorEntry, FactVersion).join(
                            FactVersion,
                            and_(
                                VectorEntry.memory_id == FactVersion.memory_id,
                                VectorEntry.version == FactVersion.version,
                            ),
                        )
                        vectors = vectors.where(
                            *base._where_criteria,
                            VectorEntry.account_id == request.scope.user_id,
                            VectorEntry.companion_id == request.scope.companion_id,
                            VectorEntry.profile_id == request.profile.profile_id,
                            VectorEntry.profile_json == request.profile.model_dump_json(),
                            VectorEntry.index_generation == embedded.index_generation,
                        )
                        if query.entity_ids:
                            vectors = vectors.where(FactVersion.subject_id.in_(query.entity_ids))
                        if self.database.engine.dialect.name == "postgresql":
                            vectors = vectors.order_by(
                                VectorEntry.projection.cosine_distance(vector.values)
                            ).limit(20)
                        else:
                            vectors = vectors.limit(request.budget.max_candidates)
                        candidates = []
                        for entry, row in (await db.execute(vectors)).all():
                            values = json.loads(entry.vector_json)
                            norm = (
                                sum(v * v for v in values) ** 0.5
                                * sum(v * v for v in vector.values) ** 0.5
                            )
                            similarity = (
                                sum(a * b for a, b in zip(values, vector.values, strict=True))
                                / norm
                            )
                            if similarity >= 0.30:
                                record = load_record(row)
                                if not query.time_filter or overlaps(
                                    record.statement.valid_time, query.time_filter
                                ):
                                    candidates.append((similarity, record))
                        participating.add("semantic")
                        for rank, (score, record) in enumerate(
                            sorted(candidates, key=lambda x: x[0], reverse=True)[:20], 1
                        ):
                            key = (record.ref.object_id, record.ref.version)
                            records[key] = record
                            ranked[key].append(
                                CandidateRank(
                                    ref=record.ref,
                                    route="semantic",
                                    query_id=query.query_id,
                                    rank=rank,
                                    raw_score=float(score),
                                    summary_path=[],
                                )
                            )

            async def optional_semantic():
                try:
                    await semantic_branch()
                except SQLAlchemyError:
                    from .contracts import ErrorInfo

                    failures.append(
                        ErrorInfo(
                            code="storage_unavailable",
                            message="Semantic projection is temporarily unavailable",
                            retryable=True,
                        )
                    )

            # A one-branch parent cannot silently start two children.
            if request.meta.execution.max_parallel_branches >= 2:
                tasks = [
                    asyncio.create_task(exact_branch()),
                    asyncio.create_task(optional_semantic()),
                ]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            else:
                await exact_branch()
                await optional_semantic()
            selected = []
            historical = request.include_historical or any(q.time_filter for q in request.queries)
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                if account.memory_revision != revision:
                    raise MemoryFault("version_conflict", "Memory changed while recall was running")
                for key, record in records.items():
                    levels = []
                    valid = True
                    for pointer in record.sources:
                        source = await db.get(
                            SourceRevision, (pointer.event_id, pointer.source_revision)
                        )
                        if source is None or source.account_id != account.id or source.revoked:
                            valid = False
                            break
                        level = (
                            "raw"
                            if source.source_json and utc(source.expires_at) > self.clock()
                            else "summary_only"
                        )
                        if level != "raw" and pointer.capsule_id:
                            capsule = await db.get(Capsule, pointer.capsule_id)
                            if capsule and capsule.account_id == account.id:
                                level = "evidence_capsule"
                        levels.append(level)
                    if not valid or record.sensitivity == "restricted":
                        continue
                    # One contribution per actual route/query pair, never duplicate hits.
                    ranks = {(r.route, r.query_id): r for r in ranked[key]}
                    rrf = sum(1 / (60 + r.rank) for r in ranks.values())
                    denominator = max(1, len(participating) * len(request.queries)) / 61
                    relevance = min(1.0, rrf / denominator)
                    age = max(
                        0,
                        (
                            self.clock() - (record.last_user_confirmed_at or record.mentioned_at)
                        ).total_seconds()
                        / 86400,
                    )
                    half_life = (
                        3650
                        if record.importance >= 0.8
                        else 365
                        if record.content_kind == "preference"
                        else 30
                    )
                    freshness = 1.0 if record.user_pinned else 0.05 + 0.95 * 2 ** (-age / half_life)
                    time_score = 1.0 if historical else 0.5
                    entity_score = float(
                        any(record.statement.subject_id in q.entity_ids for q in request.queries)
                    )
                    score = (
                        0.5 * relevance
                        + 0.15 * entity_score
                        + 0.15 * time_score
                        + 0.1 * record.importance
                    )
                    score += 0.1 * freshness if not historical else 0.1 * time_score
                    selected.append(
                        RetrievedMemory(
                            record=record,
                            ranks=list(ranks.values()),
                            fused_score=rrf,
                            decay_freshness=freshness,
                            final_score=score,
                            evidence_level="raw"
                            if "raw" in levels
                            else "evidence_capsule"
                            if "evidence_capsule" in levels
                            else "summary_only",
                            permitted_use="clarify_only"
                            if record.statement.subject_id != request.scope.user_id
                            and any(q.purpose == "clarify" for q in request.queries)
                            else "implicit_only"
                            if record.sensitivity == "private"
                            else "normal",
                        )
                    )
                visibility = self._visibility(account, None, [], [])
            selected.sort(
                key=lambda r: (
                    (r.record.ref.object_id, r.record.ref.version) in profile_keys,
                    r.final_score,
                ),
                reverse=True,
            )
            # Broad life reviews must not spend the whole context on one category.
            categories = set().union(*(recall_categories(q.text) for q in request.queries))
            if len(categories) > 1:
                representatives, remaining, seen_categories = [], [], set()
                for hit in selected:
                    predicate = hit.record.statement.predicate
                    if predicate in categories and predicate not in seen_categories:
                        representatives.append(hit)
                        seen_categories.add(predicate)
                    else:
                        remaining.append(hit)
                selected = representatives + remaining
            selected = selected[: request.budget.max_candidates]
            output = RetrievalOutput(
                candidates=selected,
                selected_refs=[r.record.ref for r in selected[: request.budget.max_selected]],
                cold_status="not_needed",
                next_cursor=None,
                archive_cursor=None,
                missing_reason="Semantic route unavailable" if failures else None,
                memory_revision=revision,
                visibility=visibility,
            )
            return ModuleResult[RetrievalOutput](
                status="partial" if failures else "ok",
                data=output,
                error=failures[0] if failures else None,
                visibility=visibility,
            )

        return await self.executor.run(request, operation)
