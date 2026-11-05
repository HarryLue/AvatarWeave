"""M24 explicit embedding spaces, finite vectors and scoped transactional projections."""

import asyncio
import json
import math

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .contracts import EmbeddedItem, EmbeddingInput, EmbeddingOutput, ModuleResult
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .tables import FactVersion, VectorEntry


class ProviderVector(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    index: int = Field(ge=0)
    embedding: list[float] = Field(min_length=1, max_length=65536)


class ProviderEmbedding(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    model: str
    data: list[ProviderVector] = Field(max_length=128)
    usage: dict[str, int]


class EmbeddingGateway:
    @classmethod
    def from_settings(cls, settings):
        if not settings.embedding_model:
            return None
        endpoint, key = settings.embedding_base_url, settings.embedding_api_key.get_secret_value()
        if settings.embedding_reference_env:
            from dotenv import dotenv_values

            reference = dotenv_values(settings.embedding_reference_env)
            endpoint = endpoint or reference.get("BASE_URL", "")
            key = (
                key or reference.get("OPENAI_API_KEY", "") or reference.get("DASHSCOPE_API_KEY", "")
            )
        if not endpoint or not key:
            return None
        from .contracts import EmbeddingProfile

        profile = EmbeddingProfile(
            profile_id=f"{settings.embedding_model}:{settings.embedding_dimensions}",
            model_id=settings.embedding_model,
            revision=settings.embedding_revision,
            dimensions=settings.embedding_dimensions,
            metric="cosine",
            normalized=True,
        )
        return cls(base_url=endpoint, api_key=key, profile=profile)

    def __init__(self, *, base_url, api_key, profile, client=None):
        self.base_url, self._api_key, self.profile = base_url.rstrip("/"), api_key, profile
        self.client = client or httpx.AsyncClient(timeout=3.0, follow_redirects=False)
        self.running = 0

    async def close(self):
        await self.client.aclose()

    async def embed(self, request: EmbeddingInput):
        if request.profile != self.profile:
            raise MemoryFault(
                "version_conflict", "Embedding profile does not match the configured model space"
            )
        if self.running >= 2:
            raise MemoryFault("overloaded", "Embedding resource is busy", retryable=True)
        if len(request.items) > 10:
            raise MemoryFault("budget_exceeded", "Embedding batch exceeds the provider limit")
        self.running += 1
        try:
            async with asyncio.timeout(request.meta.execution.budget_ms / 1000):
                async with self.client.stream(
                    "POST",
                    self.base_url + "/embeddings",
                    headers={"Authorization": "Bearer " + self._api_key},
                    json={
                        "model": self.profile.model_id,
                        "dimensions": self.profile.dimensions,
                        "input": [text for _, text in request.items],
                        "encoding_format": "float",
                    },
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 4 * 1024 * 1024:
                            raise MemoryFault(
                                "budget_exceeded", "Embedding response exceeds its budget"
                            )
                        body.extend(chunk)
                data = ProviderEmbedding.model_validate_json(body)
            if len(data.data) != len(request.items) or {d.index for d in data.data} != set(
                range(len(request.items))
            ):
                raise MemoryFault(
                    "invalid_model_output", "Embedding response indices do not match the batch"
                )
            if data.model != self.profile.model_id:
                raise MemoryFault(
                    "version_conflict", "Provider returned a different embedding model"
                )
            items = []
            for source in sorted(data.data, key=lambda d: d.index):
                if len(source.embedding) != self.profile.dimensions:
                    raise MemoryFault(
                        "invalid_model_output", "Embedding dimensions do not match the profile"
                    )
                norm = math.sqrt(sum(v * v for v in source.embedding))
                if not math.isfinite(norm) or norm <= 0:
                    raise MemoryFault(
                        "invalid_model_output", "Embedding vector has no finite nonzero norm"
                    )
                values = (
                    [v / norm for v in source.embedding]
                    if self.profile.normalized
                    else source.embedding
                )
                items.append(EmbeddedItem(ref=request.items[source.index][0], values=values))
            return EmbeddingOutput(
                profile=self.profile,
                items=items,
                input_tokens=data.usage.get("total_tokens", data.usage.get("prompt_tokens", 0)),
                index_generation="index:" + self.profile.revision,
            )
        except ValidationError:
            raise MemoryFault("invalid_model_output", "Malformed embedding response") from None
        except httpx.HTTPError:
            raise MemoryFault(
                "provider_unavailable", "Embedding service unavailable", retryable=True
            ) from None
        except TimeoutError:
            raise MemoryFault(
                "deadline_exceeded", "Embedding deadline exceeded", retryable=True
            ) from None
        finally:
            self.running -= 1


class EmbeddingMixin:
    async def embedding(self, request, gateway):
        async def operation():
            if request.purpose == "query":
                async with self.database.sessions() as db:
                    account = await self._account(db, request.scope)
                    self._privacy(account, request)
            # Query vectors are ephemeral. Index/migration input text is rendered
            # from the owned current record, never accepted verbatim from a caller.
            if request.purpose != "query":
                async with self.database.sessions() as db:
                    account = await self._account(db, request.scope)
                    self._privacy(account, request)
                    from .job_context import verify_job_lease

                    await verify_job_lease(db, account.id, self.clock())
                    for ref, text in request.items:
                        row = await db.scalar(
                            select(FactVersion).where(
                                *fact_scope(request.scope),
                                FactVersion.memory_id == ref.object_id,
                                FactVersion.version == ref.version,
                                FactVersion.system_to.is_(None),
                                FactVersion.status == "active",
                            )
                        )
                        if row is None or render_record(load_record(row)) != text:
                            raise MemoryFault(
                                "version_conflict",
                                "Index input is not the current authorized record",
                            )
            result = await gateway.embed(request)
            if (
                result.profile != request.profile
                or result.index_generation != "index:" + request.profile.revision
                or len(result.items) != len(request.items)
                or {item.ref.model_dump_json() for item in result.items}
                != {ref.model_dump_json() for ref, _ in request.items}
                or any(
                    len(item.values) != request.profile.dimensions
                    or not all(math.isfinite(v) for v in item.values)
                    or sum(v * v for v in item.values) <= 0
                    for item in result.items
                )
            ):
                raise MemoryFault(
                    "invalid_model_output", "Embedding result does not match the authorized batch"
                )
            if request.purpose != "query":
                async with self.database.sessions.begin() as db:
                    account = await self._account(db, request.scope, lock=True)
                    self._privacy(account, request)
                    from .job_context import verify_job_lease

                    await verify_job_lease(db, account.id, self.clock())
                    for item in result.items:
                        row = await db.scalar(
                            select(FactVersion).where(
                                *fact_scope(request.scope),
                                FactVersion.memory_id == item.ref.object_id,
                                FactVersion.version == item.ref.version,
                                FactVersion.system_to.is_(None),
                                FactVersion.status == "active",
                            )
                        )
                        if row is None:
                            raise MemoryFault(
                                "version_conflict", "Memory changed while embedding was computed"
                            )
                        key = (
                            account.id,
                            item.ref.object_id,
                            item.ref.version,
                            result.profile.profile_id,
                            result.index_generation,
                        )
                        existing = await db.get(VectorEntry, key)
                        if existing and existing.profile_json != result.profile.model_dump_json():
                            raise MemoryFault(
                                "version_conflict",
                                "Profile ID cannot be reused for a different vector space",
                            )
                        if not existing:
                            db.add(
                                VectorEntry(
                                    account_id=account.id,
                                    companion_id=request.scope.companion_id,
                                    memory_id=item.ref.object_id,
                                    version=item.ref.version,
                                    profile_id=result.profile.profile_id,
                                    index_generation=result.index_generation,
                                    profile_json=result.profile.model_dump_json(),
                                    vector_json=json.dumps(item.values),
                                    projection=item.values,
                                )
                            )
                    if request.purpose == "index":
                        await db.flush()
                        await self.settle_index_sources(db, request.scope, request.profile)
                        await db.flush()
                        await self.advance_watermarks(db, account)
            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                from .job_context import verify_job_lease

                await verify_job_lease(db, account.id, self.clock())
                visibility = self._visibility(account, None, [], [])
            return ModuleResult[EmbeddingOutput](status="ok", data=result, visibility=visibility)

        return await self.executor.run(request, operation)

    async def settle_index_sources(self, db, scope, profile, limit=64):
        """Reconcile a bounded source page using current facts, not job success alone."""
        from sqlalchemy import and_, case, func, tuple_

        from .storage import SourceRevision
        from .tables import FactSource

        pending = list(
            await db.scalars(
                select(SourceRevision)
                .where(
                    SourceRevision.account_id == scope.user_id,
                    SourceRevision.companion_id == scope.companion_id,
                    SourceRevision.structured_settled.is_(True),
                    SourceRevision.index_settled.is_(False),
                    SourceRevision.revoked.is_(False),
                )
                .order_by(SourceRevision.sequence)
                .limit(limit)
            )
        )
        if not pending:
            return 0
        counts = {
            (event, revision): (total, missing)
            for event, revision, total, missing in (
                await db.execute(
                    select(
                        FactSource.event_id,
                        FactSource.source_revision,
                        func.count(),
                        func.sum(case((VectorEntry.memory_id.is_(None), 1), else_=0)),
                    )
                    .join(
                        FactVersion,
                        and_(
                            FactVersion.memory_id == FactSource.memory_id,
                            FactVersion.version == FactSource.version,
                        ),
                    )
                    .outerjoin(
                        VectorEntry,
                        and_(
                            VectorEntry.account_id == scope.user_id,
                            VectorEntry.companion_id == scope.companion_id,
                            VectorEntry.memory_id == FactVersion.memory_id,
                            VectorEntry.version == FactVersion.version,
                            VectorEntry.profile_id == profile.profile_id,
                            VectorEntry.profile_json == profile.model_dump_json(),
                            VectorEntry.index_generation == "index:" + profile.revision,
                        ),
                    )
                    .where(
                        *fact_scope(scope),
                        FactVersion.status == "active",
                        FactVersion.system_to.is_(None),
                        tuple_(FactSource.event_id, FactSource.source_revision).in_(
                            [(r.event_id, r.revision) for r in pending]
                        ),
                    )
                    .group_by(FactSource.event_id, FactSource.source_revision)
                )
            ).all()
        }
        for source in pending:
            total, missing = counts.get((source.event_id, source.revision), (0, 0))
            if missing == 0:
                source.index_settled = True
                source.state = "indexed" if total else "rejected"
        return sum(row.index_settled for row in pending)


def render_record(record):
    # Stable, explicit render version. Never embed a whole life summary.
    return json.dumps(
        {
            "render_version": 1,
            "subject": record.statement.subject_id,
            "predicate": record.statement.predicate,
            "value": record.statement.canonical_value,
            "polarity": record.statement.polarity,
            "qualifiers": [q.model_dump() for q in record.statement.qualifiers],
            "time": record.statement.valid_time.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
