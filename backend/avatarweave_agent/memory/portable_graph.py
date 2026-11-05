"""Version 3 portable data graph: history, provenance and atomic scoped restore.

The archive format stays at v2. This independently versioned transfer format has
an explicit v2 reader; import never promotes an assertion to user confirmation.
"""

import hashlib
import json
from datetime import timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter
from sqlalchemy import select

from .archive import ArchiveBundle
from .contracts import (
    CandidateMemory,
    ClientCapabilities,
    Contract,
    EntityCandidate,
    EvidenceCapsule,
    EvidencePointer,
    EvidenceRef,
    Identifier,
    OpenLoop,
    PersonaCard,
    PositiveInt,
    PrivacyPreference,
    SourceDescriptor,
    SourceEvent,
    SummaryNode,
    VersionRef,
)
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .lifecycle import fact_key
from .reflections import ReflectionSnapshot
from .storage import EventHead, MemorySession, SourceRevision
from .tables import (
    Capsule,
    ControlOperation,
    Dependency,
    DerivedView,
    Entity,
    EvidenceSuppression,
    FactKey,
    FactSource,
    FactVersion,
    ImportMapping,
    OperationReceipt,
    Tombstone,
)
from .utils import utc


class LegacyPortableBundle(Contract):
    schema_version: Literal[2] = 2
    owner_user_id: Identifier
    companion_id: Identifier
    bundle: ArchiveBundle
    raw_sources: Annotated[list[SourceEvent], Field(max_length=1000)] = Field(default_factory=list)


class PortableSource(Contract):
    descriptor: SourceDescriptor
    expires_at: AwareDatetime
    head_revision: PositiveInt


class PortableSession(Contract):
    session_id: Identifier
    created_at: AwareDatetime


class InventoryEntry(Contract):
    name: Literal[
        "records",
        "capsules",
        "source_metadata",
        "raw_sources",
        "sources",
        "sessions",
        "entities",
        "summaries",
        "open_loops",
        "personas",
        "reflections",
    ]
    count: int = Field(ge=0)
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PortableBundle(LegacyPortableBundle):
    schema_version: Literal[3] = 3
    captured_at: AwareDatetime
    memory_revision: int = Field(ge=0)
    source_watermark: int = Field(ge=0)
    sources: Annotated[list[PortableSource], Field(max_length=1000)]
    sessions: Annotated[list[PortableSession], Field(max_length=1000)]
    entities: Annotated[list[EntityCandidate], Field(max_length=1000)]
    summaries: Annotated[list[SummaryNode], Field(max_length=1000)]
    open_loops: Annotated[list[OpenLoop], Field(max_length=1000)]
    personas: Annotated[list[PersonaCard], Field(max_length=32)]
    reflections: Annotated[list[ReflectionSnapshot], Field(max_length=1000)] = Field(
        default_factory=list
    )
    inventory: Annotated[list[InventoryEntry], Field(max_length=11)]


PortablePackage = Annotated[
    LegacyPortableBundle | PortableBundle, Field(discriminator="schema_version")
]
PACKAGE_READER = TypeAdapter(PortablePackage)


def read_package(payload):
    return PACKAGE_READER.validate_json(payload)


def digest(values):
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def inventory_for(package):
    parts = {
        key: getattr(package.bundle, key) for key in ("records", "capsules", "source_metadata")
    }
    parts.update(
        {
            key: getattr(package, key)
            for key in (
                "raw_sources",
                "sources",
                "sessions",
                "entities",
                "summaries",
                "open_loops",
                "personas",
                "reflections",
            )
        }
    )
    return [
        InventoryEntry(
            name=name, count=len(rows), sha256=digest([r.model_dump(mode="json") for r in rows])
        )
        for name, rows in sorted(parts.items())
    ]


def refkey(ref):
    return ref.object_id, ref.version


def mapping_key(kind, object_id):
    return hashlib.sha256((kind + ":" + object_id).encode()).hexdigest()


def candidate_of(record):
    return CandidateMemory(
        candidate_id=record.ref.object_id,
        namespace=record.namespace,
        content_kind=record.content_kind,
        assertion_kind=record.assertion_kind,
        statement=record.statement,
        display_text=record.display_text,
        interpretation="asserted",
        sensitivity=record.sensitivity,
        proposed_importance=record.importance,
        sources=[
            EvidenceRef(
                event_id="portable-validation",
                source_revision=1,
                start_char=0,
                end_char=len(record.statement.canonical_value),
                quote=record.statement.canonical_value,
                origin="user_ui",
            )
        ],
    )


def validate_graph(package, scope):
    """Validate a closed graph, not merely parse individually well-typed rows."""
    if package.owner_user_id != scope.user_id or package.companion_id != scope.companion_id:
        raise MemoryFault("scope_mismatch", "Portable graph owner differs")
    if inventory_for(package) != package.inventory:
        raise MemoryFault("invalid_model_output", "Portable inventory count/checksum differs")

    def unique(rows, key):
        result = {key(row): row for row in rows}
        if len(result) != len(rows):
            raise MemoryFault("invalid_model_output", "Duplicate portable object/version")
        return result

    records = unique(package.bundle.records, lambda r: refkey(r.ref))
    summaries = unique(package.summaries, lambda r: refkey(r.ref))
    sources = unique(package.sources, lambda s: (s.descriptor.event_id, s.descriptor.revision))
    metadata = unique(package.bundle.source_metadata, lambda s: (s.event_id, s.revision))
    raw = unique(package.raw_sources, lambda e: (e.event_id, e.revision))
    sessions = unique(package.sessions, lambda s: s.session_id)
    capsules = unique(package.bundle.capsules, lambda c: c.capsule_id)
    entities = unique(package.entities, lambda e: e.entity_id)
    loops = unique(package.open_loops, lambda r: refkey(r.ref))
    reflections = unique(package.reflections, lambda r: refkey(r.ref))
    unique(package.personas, lambda p: (p.persona_id, p.version))
    object_ids = (
        [r.ref.object_id for r in package.bundle.records]
        + [s.ref.object_id for s in package.summaries]
        + [l.ref.object_id for l in package.open_loops]
    )
    if set(records) & set(summaries) or set(records) & set(loops) or set(summaries) & set(loops):
        raise MemoryFault("invalid_model_output", "Portable object kinds overlap")
    if len(set(object_ids)) > 3000 or metadata.keys() != sources.keys():
        raise MemoryFault("invalid_model_output", "Portable source catalog is incomplete")
    if sources.keys() & (records.keys() | summaries.keys()):
        raise MemoryFault("invalid_model_output", "Portable reference kinds are ambiguous")
    heads = {}
    for key, source in sources.items():
        desc = source.descriptor
        if desc != metadata[key] or desc.scope != scope or desc.session_id not in sessions:
            raise MemoryFault("scope_mismatch", "Source scope/session/catalog mismatch")
        if (
            source.head_revision < desc.revision
            or (desc.event_id, source.head_revision) not in sources
        ):
            raise MemoryFault("evidence_missing", "Source revision head is absent")
        if heads.setdefault(desc.event_id, source.head_revision) != source.head_revision:
            raise MemoryFault("invalid_model_output", "Source head revisions disagree")
        if desc.raw_available != (key in raw):
            raise MemoryFault("evidence_missing", "Raw availability differs from package")
        event = raw.get(key)
        if event and (
            event.scope != scope
            or event.session_id != desc.session_id
            or event.origin != desc.origin
            or event.occurred_at != desc.occurred_at
            or not event.is_final
            or desc.revoked
            or source.expires_at <= package.captured_at
        ):
            raise MemoryFault("evidence_missing", "Raw source metadata or retention differs")
        if (
            event
            and event.supersedes
            and (
                refkey(event.supersedes) not in sources
                or event.supersedes.object_id != event.event_id
                or event.supersedes.version >= event.revision
            )
        ):
            raise MemoryFault("evidence_missing", "Raw source revision chain is incomplete")
    if not raw.keys() <= sources.keys():
        raise MemoryFault("evidence_missing", "Raw source has no catalog entry")

    def legal_pointer(pointer):
        key = pointer.event_id, pointer.source_revision
        source = sources.get(key)
        if (
            source is None
            or source.descriptor.revoked
            or source.head_revision != pointer.source_revision
        ):
            raise MemoryFault("evidence_missing", "Evidence source is revoked or superseded")
        if pointer.capsule_id:
            capsule = capsules.get(pointer.capsule_id)
            if capsule is None or (capsule.source_event_id, capsule.source_revision) != key:
                raise MemoryFault("evidence_missing", "Evidence capsule reference is absent")
        elif key not in raw and pointer.level != "metadata_only":
            raise MemoryFault("evidence_missing", "Portable evidence text was omitted")

    for capsule in capsules.values():
        evidence = capsule.evidence
        key = capsule.source_event_id, capsule.source_revision
        if (
            capsule.scope != scope
            or key not in sources
            or sources[key].descriptor.revoked
            or evidence.event_id != capsule.source_event_id
            or evidence.source_revision != capsule.source_revision
            or evidence.origin != sources[key].descriptor.origin
            or capsule.source_session_id != sources[key].descriptor.session_id
            or hashlib.sha256(evidence.quote.encode()).hexdigest() != capsule.integrity_sha256
        ):
            raise MemoryFault("evidence_missing", "Capsule scope/source/checksum mismatch")
        event = raw.get(key)
        if event and event.content[evidence.start_char : evidence.end_char] != evidence.quote:
            raise MemoryFault("evidence_missing", "Capsule quote differs from original source")
    for record in records.values():
        if record.scope != scope or record.status not in {"active", "superseded", "uncertain"}:
            raise MemoryFault("scope_mismatch", "Portable fact scope/state differs")
        if (
            record.statement.subject_id != scope.user_id
            and record.statement.subject_id not in entities
        ):
            raise MemoryFault("evidence_missing", "Portable subject entity is absent")
        for pointer in record.sources:
            legal_pointer(pointer)
    for entity in entities.values():
        for ref in entity.context_refs:
            legal_pointer(
                EvidencePointer(
                    event_id=ref.object_id,
                    source_revision=ref.version,
                    level="metadata_only",
                    capsule_id=None,
                    start_char=None,
                    end_char=None,
                )
            )
    # DFS checks cycles; every leaf must be an exported atomic version.
    done, visiting = set(), set()

    def visit(key):
        if key in sources:
            if sources[key].descriptor.revoked or sources[key].head_revision != key[1]:
                raise MemoryFault("evidence_missing", "A dependency source is revoked")
            return
        if key in done:
            return
        if key not in summaries and key not in records or key in visiting:
            raise MemoryFault("evidence_missing", "Summary DAG is cyclic or incomplete")
        visiting.add(key)
        if key in records:
            for parent in records[key].source_dependencies:
                visit(refkey(parent))
            visiting.remove(key)
            done.add(key)
            return
        summary = summaries[key]
        if summary.scope != scope or summary.state not in {"valid", "pending_review"}:
            raise MemoryFault("scope_mismatch", "Summary scope/state differs")
        for ref in summary.child_refs:
            visit(refkey(ref))
        for claim in summary.claims:
            for ref in claim.supported_by:
                visit(refkey(ref))
            for pointer in claim.evidence_refs:
                legal_pointer(pointer)
        visiting.remove(key)
        done.add(key)

    for key in records.keys() | summaries.keys():
        visit(key)
    for loop in loops.values():
        if any(refkey(r) not in records for r in loop.linked_memory_refs):
            raise MemoryFault("evidence_missing", "Open loop references an absent memory")
        for pointer in loop.sources:
            legal_pointer(pointer)
    for snapshot in reflections.values():
        if any(
            refkey(r) not in records
            for r in snapshot.hypothesis.supports + snapshot.hypothesis.counters
        ):
            raise MemoryFault("evidence_missing", "Hypothesis evidence is absent")
        if snapshot.confirmed_memory_ref and refkey(snapshot.confirmed_memory_ref) not in records:
            raise MemoryFault("evidence_missing", "Confirmed hypothesis has no user fact")
    return records, sources


class PortableGraphMixin:
    async def register_persona(self, scope, card):
        """Trusted approved card registry; transfer files cannot replace live persona."""
        key = "persona:" + digest([scope.user_id, scope.companion_id, card.persona_id])
        async with self.database.sessions.begin() as db:
            await self._account(db, scope, lock=True)
            old = await db.get(DerivedView, (key, card.version))
            if old:
                prior = PersonaCard.model_validate_json(old.document_json)
                if prior.model_dump(exclude={"approved_at"}) != card.model_dump(
                    exclude={"approved_at"}
                ):
                    raise MemoryFault("version_conflict", "Approved persona version changed")
                return
            db.add(
                DerivedView(
                    id=key,
                    version=card.version,
                    account_id=scope.user_id,
                    companion_id=scope.companion_id,
                    kind="persona",
                    state="valid",
                    document_json=card.model_dump_json(),
                )
            )
            account = await self._account(db, scope)
            account.memory_revision += 1

    async def build_portable_graph(self, db, request, account):
        from .storage import EventHead

        async def bounded(query):
            rows = list(await db.scalars(query.limit(1001)))
            if len(rows) > 1000:
                raise MemoryFault(
                    "budget_exceeded", "Portable shard exceeds 1000 objects; split the export"
                )
            return rows

        source_rows = await bounded(
            select(SourceRevision)
            .where(
                SourceRevision.account_id == account.id,
                SourceRevision.companion_id == request.scope.companion_id,
            )
            .order_by(SourceRevision.sequence)
        )
        heads = dict(
            (
                await db.execute(
                    select(EventHead.event_id, EventHead.revision).where(
                        EventHead.event_id.in_({r.event_id for r in source_rows})
                    )
                )
            ).all()
        )
        legal = {
            (r.event_id, r.revision)
            for r in source_rows
            if not r.revoked and heads[r.event_id] == r.revision
        }
        rows = await bounded(
            select(FactVersion)
            .where(
                *fact_scope(request.scope),
                FactVersion.status.in_(["active", "superseded", "uncertain"]),
                FactVersion.record_json.is_not(None),
                FactVersion.namespace.in_(request.selected_namespaces),
            )
            .order_by(FactVersion.memory_id, FactVersion.version)
        )
        records = [load_record(row) for row in rows]
        records = [
            r for r in records if all((p.event_id, p.source_revision) in legal for p in r.sources)
        ]
        capids = {p.capsule_id for r in records for p in r.sources if p.capsule_id}
        capsules = []
        if request.include_source_capsules:
            capsules = [
                EvidenceCapsule.model_validate_json(r.capsule_json)
                for r in await bounded(
                    select(Capsule)
                    .where(
                        Capsule.account_id == account.id,
                        Capsule.companion_id == request.scope.companion_id,
                        Capsule.id.in_(capids),
                    )
                    .order_by(Capsule.id)
                )
            ]
        retained_caps = {c.capsule_id for c in capsules}
        raw = [
            SourceEvent.model_validate_json(r.source_json)
            for r in source_rows
            if request.include_raw_transcript
            and r.source_json
            and (r.event_id, r.revision) in legal
            and utc(r.expires_at) > self.clock()
        ]
        rawkeys = {(e.event_id, e.revision) for e in raw}

        # Omitted text remains explicitly metadata-only, never a broken capsule pointer.
        def pointer(p):
            if p.capsule_id in retained_caps:
                return p
            return p.model_copy(
                update={
                    "capsule_id": None,
                    "level": "raw"
                    if (p.event_id, p.source_revision) in rawkeys
                    else "metadata_only",
                }
            )

        records = [
            r.model_copy(update={"sources": [pointer(p) for p in r.sources]}) for r in records
        ]
        metadata = [
            SourceDescriptor(
                event_id=r.event_id,
                revision=r.revision,
                scope=request.scope,
                session_id=r.session_id,
                origin=r.origin,
                occurred_at=utc(r.occurred_at),
                raw_available=(r.event_id, r.revision) in rawkeys,
                capsule_ids=[
                    c.capsule_id
                    for c in capsules
                    if (c.source_event_id, c.source_revision) == (r.event_id, r.revision)
                ],
                revoked=r.revoked,
            )
            for r in source_rows
        ]
        sources = [
            PortableSource(
                descriptor=d, expires_at=utc(r.expires_at), head_revision=heads[r.event_id]
            )
            for d, r in zip(metadata, source_rows, strict=True)
        ]
        sessions = [
            PortableSession(session_id=r.id, created_at=utc(r.created_at))
            for r in await bounded(
                select(MemorySession)
                .where(
                    MemorySession.account_id == account.id,
                    MemorySession.companion_id == request.scope.companion_id,
                    MemorySession.id.in_({d.session_id for d in metadata}),
                )
                .order_by(MemorySession.id)
            )
        ]
        entities = [
            EntityCandidate.model_validate_json(r.entity_json)
            for r in await bounded(
                select(Entity)
                .where(
                    Entity.account_id == account.id,
                    Entity.companion_id == request.scope.companion_id,
                )
                .order_by(Entity.id)
            )
        ]
        entities = [e for e in entities if all(refkey(r) in legal for r in e.context_refs)]
        views = await bounded(
            select(DerivedView)
            .where(
                DerivedView.account_id == account.id,
                DerivedView.companion_id == request.scope.companion_id,
                DerivedView.state.in_(
                    ["valid", "pending_review", "needs_confirmation", "confirmed", "rejected"]
                ),
                DerivedView.kind.in_(
                    ["summary", "open_loop", "persona", "reflection", "persona_ref"]
                ),
            )
            .order_by(DerivedView.id, DerivedView.version)
        )
        summaries = [
            SummaryNode.model_validate_json(v.document_json) for v in views if v.kind == "summary"
        ]
        available = {refkey(r.ref) for r in records}
        selected = []
        while summaries:
            ready = [
                s
                for s in summaries
                if all(refkey(r) in available for r in s.child_refs)
                and all(refkey(r) in available for c in s.claims for r in c.supported_by)
            ]
            if not ready:
                break
            selected.extend(ready)
            available.update(refkey(s.ref) for s in ready)
            summaries = [s for s in summaries if s not in ready]
        selected = [
            s.model_copy(
                update={
                    "claims": [
                        c.model_copy(
                            update={"evidence_refs": [pointer(p) for p in c.evidence_refs]}
                        )
                        for c in s.claims
                    ]
                }
            )
            for s in selected
        ]
        loops = [
            OpenLoop.model_validate_json(v.document_json) for v in views if v.kind == "open_loop"
        ]
        persona_cards = {}
        for view in views:
            if view.kind in {"persona", "persona_ref"}:
                card = PersonaCard.model_validate_json(view.document_json)
                key = card.persona_id, card.version
                old = persona_cards.get(key)
                if old and old.model_dump(exclude={"approved_at"}) != card.model_dump(
                    exclude={"approved_at"}
                ):
                    raise MemoryFault(
                        "version_conflict", "Persona reference version has conflicting definitions"
                    )
                persona_cards[key] = card
        record_refs = {refkey(r.ref) for r in records}
        loops = [
            l.model_copy(update={"sources": [pointer(p) for p in l.sources]})
            for l in loops
            if all(refkey(r) in available for r in l.linked_memory_refs)
            and all((p.event_id, p.source_revision) in legal for p in l.sources)
        ]
        reflections = []
        for view in views:
            if view.kind != "reflection":
                continue
            snapshot = ReflectionSnapshot.model_validate_json(view.document_json)
            if all(
                refkey(ref) in record_refs
                for ref in snapshot.hypothesis.supports + snapshot.hypothesis.counters
            ) and (
                snapshot.confirmed_memory_ref is None
                or refkey(snapshot.confirmed_memory_ref) in record_refs
            ):
                reflections.append(snapshot)
        package = PortableBundle(
            owner_user_id=account.id,
            companion_id=request.scope.companion_id,
            bundle=ArchiveBundle(records=records, capsules=capsules, source_metadata=metadata),
            raw_sources=raw,
            captured_at=self.clock(),
            memory_revision=account.memory_revision,
            source_watermark=account.source_watermark,
            sources=sources,
            sessions=sessions,
            entities=entities,
            summaries=selected,
            open_loops=loops,
            personas=list(persona_cards.values()),
            reflections=reflections,
            inventory=[],
        )
        package = package.model_copy(update={"inventory": inventory_for(package)})
        validate_graph(package, request.scope)
        return package

    async def _graph_ids(self, db, request, package):
        groups = {
            "memory": {r.ref.object_id for r in package.bundle.records},
            "source": {s.descriptor.event_id for s in package.sources},
            "session": {s.session_id for s in package.sessions},
            "entity": {e.entity_id for e in package.entities},
            "capsule": {c.capsule_id for c in package.bundle.capsules},
            "summary": {s.ref.object_id for s in package.summaries},
            "loop": {l.ref.object_id for l in package.open_loops},
            "reflection": {r.ref.object_id for r in package.reflections},
        }
        keys = {mapping_key(kind, old): (kind, old) for kind, ids in groups.items() for old in ids}
        prior = {}
        keylist = sorted(keys)
        for offset in range(0, len(keylist), 512):
            for row in await db.scalars(
                select(ImportMapping)
                .where(
                    ImportMapping.account_id == request.scope.user_id,
                    ImportMapping.old_id.in_(keylist[offset : offset + 512]),
                )
                .order_by(ImportMapping.export_id)
            ):
                prior.setdefault(keys[row.old_id], row.new_id)
        # Reusing a provably identical existing owned object is an explicit mapping,
        # not blind insertion under the old ID. All other IDs enter a fresh namespace.
        ids = {
            (kind, old): prior.get(
                (kind, old), digest([request.scope.user_id, request.export_id, kind, old])[:32]
            )
            for kind, values in groups.items()
            for old in values
        }
        for kind, model, column in [
            ("memory", FactVersion, FactVersion.memory_id),
            ("source", SourceRevision, SourceRevision.event_id),
            ("session", MemorySession, MemorySession.id),
            ("entity", Entity, Entity.id),
            ("summary", DerivedView, DerivedView.id),
            ("loop", DerivedView, DerivedView.id),
            ("reflection", DerivedView, DerivedView.id),
        ]:
            oldids = sorted(groups[kind])
            for offset in range(0, len(oldids), 512):
                found = set(
                    await db.scalars(
                        select(column).where(
                            model.account_id == request.scope.user_id,
                            model.companion_id == request.scope.companion_id,
                            column.in_(oldids[offset : offset + 512]),
                        )
                    )
                )
                for old in found:
                    ids[(kind, old)] = old
        return ids

    async def graph_conflicts(self, db, request, package):
        validate_graph(package, request.scope)
        account = await self._account(db, request.scope)
        prefs = PrivacyPreference.model_validate_json(account.preferences_json)
        if not prefs.long_term_memory_enabled:
            return ["Enable long-term memory before restoring personal data"]
        if package.bundle.capsules and not prefs.retain_evidence_capsules:
            return ["Enable evidence capsule retention before restoring source excerpts"]
        ids = await self._graph_ids(db, request, package)
        opaque_ids = {old for _, old in ids} | set(ids.values())
        # Also cover every earlier import of the same IDs, not just this upload.
        mapping_hashes = [mapping_key(kind, old) for kind, old in ids]
        for offset in range(0, len(mapping_hashes), 512):
            opaque_ids.update(
                await db.scalars(
                    select(ImportMapping.new_id).where(
                        ImportMapping.account_id == request.scope.user_id,
                        ImportMapping.old_id.in_(mapping_hashes[offset : offset + 512]),
                    )
                )
            )
        failures = []
        values = sorted(opaque_ids)
        for offset in range(0, len(values), 512):
            if await db.scalar(
                select(Tombstone.object_id)
                .where(
                    Tombstone.account_id == account.id,
                    Tombstone.object_id.in_(values[offset : offset + 512]),
                )
                .limit(1)
            ):
                failures.append(
                    "Deleted objects or previously deleted import mappings cannot be restored"
                )
        source_ids = {s.descriptor.event_id for s in package.sources} | {
            ids[("source", s.descriptor.event_id)] for s in package.sources
        }
        for offset in range(0, len(source_ids), 512):
            if await db.scalar(
                select(EvidenceSuppression.event_id)
                .where(
                    EvidenceSuppression.account_id == account.id,
                    EvidenceSuppression.event_id.in_(sorted(source_ids)[offset : offset + 512]),
                )
                .limit(1)
            ):
                failures.append("Corrected source evidence cannot be restored")
        for source in package.sources:
            desc = source.descriptor
            existing = await db.get(EventHead, ids[("source", desc.event_id)])
            if existing and existing.revision != source.head_revision:
                failures.append("A source was revised after this package was created")
        for entity in package.entities:
            existing = await db.get(Entity, ids[("entity", entity.entity_id)])
            if existing:
                actual = EntityCandidate.model_validate_json(existing.entity_json)
                if (
                    existing.account_id != account.id
                    or existing.companion_id != request.scope.companion_id
                    or actual.canonical_name != entity.canonical_name
                    or actual.aliases != entity.aliases
                ):
                    failures.append("An entity binding changed after export; review its mapping")
        for record in package.bundle.records:
            target_id = ids[("memory", record.ref.object_id)]
            existing = await db.get(FactVersion, (target_id, record.ref.version))
            statement = record.statement.model_copy(
                update={
                    "subject_id": request.scope.user_id
                    if record.statement.subject_id == request.scope.user_id
                    else ids[("entity", record.statement.subject_id)]
                }
            )
            if existing:
                actual = load_record(existing)
                dependency_kinds = {refkey(r.ref): "memory" for r in package.bundle.records}
                dependency_kinds.update({refkey(r.ref): "summary" for r in package.summaries})
                dependency_kinds.update(
                    {
                        (s.descriptor.event_id, s.descriptor.revision): "source"
                        for s in package.sources
                    }
                )
                expected = record.model_copy(
                    update={
                        "statement": statement,
                        "source_dependencies": [
                            VersionRef(
                                object_id=ids[(dependency_kinds[refkey(r)], r.object_id)],
                                version=r.version,
                            )
                            for r in record.source_dependencies
                        ],
                    }
                )
                if (
                    existing.account_id != account.id
                    or existing.companion_id != request.scope.companion_id
                    or actual.model_dump(exclude={"ref", "scope", "sources", "tier"})
                    != expected.model_dump(exclude={"ref", "scope", "sources", "tier"})
                ):
                    failures.append(
                        "A fact version changed after export; an explicit merge is required"
                    )
                continue
            if record.system_to is not None or record.status != "active":
                continue
            probe = candidate_of(record).model_copy(update={"statement": statement})
            comparable = await self._comparable(db, request.scope, fact_key(request.scope, probe))
            from .lifecycle import overlaps

            if any(
                overlaps(load_record(row).statement.valid_time, statement.valid_time)
                for row in comparable
            ):
                failures.append(
                    "Imported facts overlap current knowledge; an explicit merge is required"
                )
        return list(dict.fromkeys(failures))[:128]

    async def restore_portable_graph(self, request, package):
        """One transaction exposes a complete graph or nothing; no model calls."""
        checksum = digest(package.model_dump(mode="json"))
        operation_id = digest(["portable-graph", request.export_id])
        async with self.database.sessions.begin() as db:
            account = await self._account(db, request.scope, lock=True)
            self._privacy(account, request)
            if (
                await db.scalar(
                    select(ControlOperation.event_id).where(
                        ControlOperation.account_id == account.id,
                        ControlOperation.companion_id == request.scope.companion_id,
                        ControlOperation.event_id == request.explicit_user_confirmation_event,
                    )
                )
                is None
            ):
                raise MemoryFault("unauthorized", "Restore requires an authenticated control event")
            previous = await db.get(OperationReceipt, (account.id, operation_id))
            if previous:
                if previous.request_json != checksum:
                    raise MemoryFault("version_conflict", "Portable import operation changed")
                return
            conflicts = await self.graph_conflicts(db, request, package)
            if conflicts:
                raise MemoryFault("manual_review_required", conflicts[0])
            ids = await self._graph_ids(db, request, package)

            def mapped(kind, value):
                return ids[(kind, value)]

            def ref(kind, value):
                return VersionRef(object_id=mapped(kind, value.object_id), version=value.version)

            def pointer(p):
                return p.model_copy(
                    update={
                        "event_id": mapped("source", p.event_id),
                        "capsule_id": mapped("capsule", p.capsule_id) if p.capsule_id else None,
                    }
                )

            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            capabilities = ClientCapabilities(
                command_versions=[1],
                action_cancel=False,
                action_completed_receipt=False,
                playback_granularity="none",
                media_clock_supported=False,
            )
            for old in package.sessions:
                new_id = mapped("session", old.session_id)
                if await db.get(MemorySession, new_id) is None:
                    db.add(
                        MemorySession(
                            id=new_id,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            room="vm-import-" + new_id,
                            participant="import-" + new_id,
                            memory_enabled=True,
                            status="ended",
                            capabilities_json=capabilities.model_dump_json(),
                            idempotency_key="import:" + new_id,
                            owner_epoch=0,
                            created_at=old.created_at,
                        )
                    )
            await db.flush()
            for source in package.sources:
                desc = source.descriptor
                new_id = mapped("source", desc.event_id)
                if await db.get(EventHead, new_id) is None:
                    db.add(
                        EventHead(
                            event_id=new_id,
                            session_id=mapped("session", desc.session_id),
                            turn_id="import:" + new_id,
                            revision=source.head_revision,
                        )
                    )
            await db.flush()
            raw = {(e.event_id, e.revision): e for e in package.raw_sources}
            added_sources = []
            for source in package.sources:
                desc = source.descriptor
                new_id = mapped("source", desc.event_id)
                if await db.get(SourceRevision, (new_id, desc.revision)):
                    continue
                event = raw.get((desc.event_id, desc.revision))
                expires = min(
                    source.expires_at, desc.occurred_at + timedelta(days=prefs.raw_transcript_days)
                )
                if event:
                    event = event.model_copy(
                        update={
                            "event_id": new_id,
                            "session_id": mapped("session", desc.session_id),
                            "turn_id": "import:" + new_id,
                            "scope": request.scope,
                            "supersedes": ref("source", event.supersedes)
                            if event.supersedes
                            else None,
                        }
                    )
                account.source_watermark += 1
                added_sources.append(
                    SourceRevision(
                        event_id=new_id,
                        revision=desc.revision,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        session_id=mapped("session", desc.session_id),
                        sequence=account.source_watermark,
                        source_json=event.model_dump_json()
                        if event and not desc.revoked and expires > self.clock()
                        else None,
                        origin=desc.origin,
                        occurred_at=desc.occurred_at,
                        expires_at=expires,
                        revoked=desc.revoked,
                        state="revoked" if desc.revoked else "structured",
                        structured_settled=True,
                        index_settled=desc.revoked,
                    )
                )
            db.add_all(added_sources)
            await db.flush()
            for entity in package.entities:
                new_id = mapped("entity", entity.entity_id)
                if await db.get(Entity, new_id) is None:
                    value = entity.model_copy(
                        update={
                            "entity_id": new_id,
                            "context_refs": [ref("source", r) for r in entity.context_refs],
                        }
                    )
                    db.add(
                        Entity(
                            id=new_id,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            entity_json=value.model_dump_json(),
                        )
                    )
                    await self.index_entity_aliases(db, request.scope, value)
            source_by_ref = {
                (s.descriptor.event_id, s.descriptor.revision): s for s in package.sources
            }
            for capsule in package.bundle.capsules:
                new_id = mapped("capsule", capsule.capsule_id)
                evidence = capsule.evidence.model_copy(
                    update={"event_id": mapped("source", capsule.source_event_id)}
                )
                value = capsule.model_copy(
                    update={
                        "capsule_id": new_id,
                        "scope": request.scope,
                        "source_session_id": mapped("session", capsule.source_session_id),
                        "source_event_id": mapped("source", capsule.source_event_id),
                        "evidence": evidence,
                        "retention_authorization_ref": request.explicit_user_confirmation_event,
                        "privacy_epoch": account.privacy_epoch,
                        "original_source_available": (
                            capsule.source_event_id,
                            capsule.source_revision,
                        )
                        in raw
                        and min(
                            source_by_ref[
                                (capsule.source_event_id, capsule.source_revision)
                            ].expires_at,
                            source_by_ref[
                                (capsule.source_event_id, capsule.source_revision)
                            ].descriptor.occurred_at
                            + timedelta(days=prefs.raw_transcript_days),
                        )
                        > self.clock(),
                    }
                )
                if await db.get(Capsule, new_id) is None:
                    db.add(
                        Capsule(
                            id=new_id,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            event_id=value.source_event_id,
                            source_revision=value.source_revision,
                            capsule_json=value.model_dump_json(),
                        )
                    )
            imported = []
            dependency_kinds = {refkey(r.ref): "memory" for r in package.bundle.records}
            dependency_kinds.update({refkey(r.ref): "summary" for r in package.summaries})
            dependency_kinds.update(
                {(s.descriptor.event_id, s.descriptor.revision): "source" for s in package.sources}
            )
            for old in package.bundle.records:
                statement = old.statement.model_copy(
                    update={
                        "subject_id": account.id
                        if old.statement.subject_id == account.id
                        else mapped("entity", old.statement.subject_id)
                    }
                )
                value = old.model_copy(
                    update={
                        "ref": ref("memory", old.ref),
                        "scope": request.scope,
                        "statement": statement,
                        "sources": [pointer(p) for p in old.sources],
                        "source_dependencies": [
                            ref(dependency_kinds[refkey(r)], r) for r in old.source_dependencies
                        ],
                    }
                )
                if await db.get(FactVersion, (value.ref.object_id, value.ref.version)) is None:
                    imported.append(value)
            keys = {fact_key(request.scope, candidate_of(r)) for r in imported}
            existing_keys = set(await db.scalars(select(FactKey.id).where(FactKey.id.in_(keys))))
            for key in sorted(keys - existing_keys):
                db.add(
                    FactKey(
                        id=key,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        lock_revision=0,
                    )
                )
            await db.flush()
            for value in imported:
                db.add(
                    FactVersion(
                        memory_id=value.ref.object_id,
                        version=value.ref.version,
                        account_id=account.id,
                        companion_id=request.scope.companion_id,
                        fact_key=fact_key(request.scope, candidate_of(value)),
                        subject_id=value.statement.subject_id,
                        predicate=value.statement.predicate,
                        namespace=value.namespace,
                        system_from=value.system_from,
                        system_to=value.system_to,
                        valid_start=value.statement.valid_time.start,
                        valid_end=value.statement.valid_time.end,
                        status=value.status,
                        record_json=value.model_dump_json(),
                    )
                )
            await db.flush()
            for value in imported:
                for event_id, revision in {(p.event_id, p.source_revision) for p in value.sources}:
                    db.add(
                        FactSource(
                            memory_id=value.ref.object_id,
                            version=value.ref.version,
                            event_id=event_id,
                            source_revision=revision,
                        )
                    )
            dependencies = set()
            for value in imported:
                for parent in value.source_dependencies:
                    dependencies.add(
                        (
                            parent.object_id,
                            parent.version,
                            value.ref.object_id,
                            value.ref.version,
                            "inferred_from",
                        )
                    )
            for entity in package.entities:
                for source in entity.context_refs:
                    dependencies.add(
                        (
                            mapped("source", source.object_id),
                            source.version,
                            mapped("entity", entity.entity_id),
                            1,
                            "entity_from",
                        )
                    )
            records = {refkey(r.ref): r for r in package.bundle.records}

            def child_ref(value):
                return ref("memory" if refkey(value) in records else "summary", value)

            for summary in package.summaries:
                claims = [
                    c.model_copy(
                        update={
                            "supported_by": [child_ref(r) for r in c.supported_by],
                            "evidence_refs": [pointer(p) for p in c.evidence_refs],
                        }
                    )
                    for c in summary.claims
                ]
                # Restored paraphrases await review. Exact extractive claims are
                # independently checked against atomic leaves before becoming valid.
                verified = summary.state == "valid" and all(
                    c.preserves_negation
                    and any(
                        refkey(r) in records
                        and records[refkey(r)].display_text == c.text
                        and records[refkey(r)].statement.valid_time == c.valid_time
                        for r in c.supported_by
                    )
                    for c in summary.claims
                )
                value = summary.model_copy(
                    update={
                        "ref": ref("summary", summary.ref),
                        "scope": request.scope,
                        "claims": claims,
                        "child_refs": [child_ref(r) for r in summary.child_refs],
                        "leaf_manifest_ref": digest([request.export_id, summary.leaf_manifest_ref]),
                        "input_watermark": account.memory_revision + 1,
                        "state": "valid" if verified else "pending_review",
                    }
                )
                if await db.get(DerivedView, refkey(value.ref)) is None:
                    db.add(
                        DerivedView(
                            id=value.ref.object_id,
                            version=value.ref.version,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            kind="summary",
                            state=value.state,
                            document_json=value.model_dump_json(),
                        )
                    )
                    for parent in value.child_refs:
                        dependencies.add(
                            (
                                parent.object_id,
                                parent.version,
                                value.ref.object_id,
                                value.ref.version,
                                "summary_of",
                            )
                        )
            for loop in package.open_loops:
                value = loop.model_copy(
                    update={
                        "ref": ref("loop", loop.ref),
                        "sources": [pointer(p) for p in loop.sources],
                        "linked_memory_refs": [ref("memory", r) for r in loop.linked_memory_refs],
                        # Follow-up permission is never silently enabled by importing a file.
                        "user_allows_followup": False,
                    }
                )
                if await db.get(DerivedView, refkey(value.ref)) is None:
                    db.add(
                        DerivedView(
                            id=value.ref.object_id,
                            version=value.ref.version,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            kind="open_loop",
                            state="valid",
                            document_json=value.model_dump_json(),
                        )
                    )
                    for parent in value.linked_memory_refs:
                        dependencies.add(
                            (
                                parent.object_id,
                                parent.version,
                                value.ref.object_id,
                                value.ref.version,
                                "open_loop_of",
                            )
                        )
            for persona in package.personas:
                # Imported role versions are a reference library, not active instructions.
                key = "persona-import:" + digest([request.export_id, persona.persona_id])
                if await db.get(DerivedView, (key, persona.version)) is None:
                    db.add(
                        DerivedView(
                            id=key,
                            version=persona.version,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            kind="persona_ref",
                            state="valid",
                            document_json=persona.model_dump_json(),
                        )
                    )
            for snapshot in package.reflections:
                value = snapshot.model_copy(
                    update={
                        "ref": ref("reflection", snapshot.ref),
                        "hypothesis": snapshot.hypothesis.model_copy(
                            update={
                                "supports": [
                                    ref("memory", r) for r in snapshot.hypothesis.supports
                                ],
                                "counters": [
                                    ref("memory", r) for r in snapshot.hypothesis.counters
                                ],
                            }
                        ),
                        "confirmed_memory_ref": ref("memory", snapshot.confirmed_memory_ref)
                        if snapshot.confirmed_memory_ref
                        else None,
                    }
                )
                if await db.get(DerivedView, refkey(value.ref)) is None:
                    db.add(
                        DerivedView(
                            id=value.ref.object_id,
                            version=value.ref.version,
                            account_id=account.id,
                            companion_id=request.scope.companion_id,
                            kind="reflection",
                            state="pending_review"
                            if value.state == "needs_confirmation"
                            else value.state,
                            document_json=value.model_dump_json(),
                        )
                    )
                    for parent in value.hypothesis.supports + value.hypothesis.counters:
                        dependencies.add(
                            (
                                parent.object_id,
                                parent.version,
                                value.ref.object_id,
                                value.ref.version,
                                "inferred_from",
                            )
                        )
            for parent_id, parent_version, child_id, child_version, relation in dependencies:
                if (
                    await db.get(
                        Dependency, (account.id, parent_id, parent_version, child_id, child_version)
                    )
                    is None
                ):
                    db.add(
                        Dependency(
                            account_id=account.id,
                            parent_id=parent_id,
                            parent_version=parent_version,
                            child_id=child_id,
                            child_version=child_version,
                            relation=relation,
                        )
                    )
            # Mappings cover sources and derived objects too, so deleting any earlier
            # mapped object blocks a second upload of the same original data.
            versions = {(kind, old): {1} for kind, old in ids}
            for kind, values in [
                ("memory", package.bundle.records),
                ("summary", package.summaries),
                ("loop", package.open_loops),
            ]:
                for value in values:
                    versions.setdefault((kind, value.ref.object_id), set()).add(value.ref.version)
            for source in package.sources:
                versions.setdefault(("source", source.descriptor.event_id), set()).add(
                    source.descriptor.revision
                )
            for (kind, old), values in versions.items():
                for version in values:
                    pk = (account.id, request.export_id, mapping_key(kind, old), version)
                    if await db.get(ImportMapping, pk) is None:
                        db.add(
                            ImportMapping(
                                account_id=account.id,
                                export_id=request.export_id,
                                old_id=pk[2],
                                old_version=version,
                                new_id=mapped(kind, old),
                                new_version=version,
                            )
                        )
            account.memory_revision += 1
            await db.flush()
            await self.advance_watermarks(db, account)
            db.add(
                OperationReceipt(
                    account_id=account.id,
                    companion_id=request.scope.companion_id,
                    operation_id=operation_id,
                    operation="portable_graph",
                    request_json=checksum,
                    result_json=json.dumps(
                        {
                            "record_count": len(imported),
                            "source_count": len(added_sources),
                            "memory_revision": account.memory_revision,
                        }
                    ),
                )
            )
