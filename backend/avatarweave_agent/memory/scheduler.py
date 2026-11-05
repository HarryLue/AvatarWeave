"""Incremental, bounded background indexing, compression, tiering and archive checks."""

import hashlib
from datetime import timedelta

from pydantic import AwareDatetime
from sqlalchemy import select, tuple_

from .contracts import (
    ArchiveInput,
    CompressionInput,
    CompressionPolicy,
    Contract,
    EmbeddingInput,
    PrivacyPreference,
    ReflectionInput,
    Scope,
    TieringInput,
    TierPolicy,
    TimeWindow,
    VersionFence,
)
from .embedding import render_record
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .storage import Account
from .tables import DerivedView, FactVersion, VectorEntry


class MaintenanceCursor(Contract):
    revision: int
    privacy_epoch: int
    profile_id: str
    after_memory_id: str | None
    completed: bool
    next_audit_at: AwareDatetime


class SchedulerMixin:
    async def apply_tier_moves(self, request, moves, store):
        """Apply location metadata; cold changes first enqueue verified archival."""
        from .http import meta

        cold = [move.ref for move in moves if move.to_tier == "cold"]
        if cold and store:
            metadata = meta(self, lane="maintenance", budget_ms=30000).model_copy(
                update={
                    "operation_id": hashlib.sha256(
                        (request.meta.operation_id + ":archive").encode()
                    ).hexdigest()
                }
            )
            await self.enqueue_maintenance(
                "archive",
                ArchiveInput(
                    meta=metadata,
                    scope=request.scope,
                    fence=request.fence,
                    operation="write",
                    record_refs=cold,
                    archive=None,
                ),
            )
        targets = [(move.ref, move.to_tier) for move in moves if move.to_tier != "cold"]
        await self._set_tiers(request, targets)

    async def _set_tiers(self, request, targets):
        if not targets:
            return
        async with self.database.sessions.begin() as db:
            account = await self._account(db, request.scope, lock=True)
            self._privacy(account, request)
            changed = False
            for ref, tier in targets:
                row = await db.scalar(
                    select(FactVersion).where(
                        *fact_scope(request.scope),
                        FactVersion.memory_id == ref.object_id,
                        FactVersion.version == ref.version,
                        FactVersion.status == "active",
                        FactVersion.system_to.is_(None),
                    )
                )
                if row is None:
                    raise MemoryFault("version_conflict", "Tier target was superseded")
                record = load_record(row)
                if record.tier != tier:
                    row.record_json = record.model_copy(update={"tier": tier}).model_dump_json()
                    changed = True
            if changed:
                account.memory_revision += 1

    async def schedule_incremental(
        self, embedding_gateway=None, store=None, *, model_available=False
    ):
        """Inspect one user and at most ten current facts per tick, outside voice.

        Cursors are durable per user/companion. A changed revision/profile restarts
        its bounded sweep; ordinary turns never trigger a ten-year scan.
        """
        from .http import meta

        async with self.database.sessions() as db:
            after = getattr(self, "_scheduler_after_account", "")
            account = await db.scalar(
                select(Account)
                .where(Account.workspace_id == self.workspace_id, Account.id > after)
                .order_by(Account.id)
                .limit(1)
            )
            if account is None:
                self._scheduler_after_account = ""
                return 0
            self._scheduler_after_account = account.id
            account_id = account.id
        created = 0
        for companion in sorted(self.companions):
            scope = Scope(
                workspace_id=self.workspace_id, user_id=account_id, companion_id=companion
            )
            try:
                async with self.database.sessions() as db:
                    account = await self._account(db, scope)
                    prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                    if not prefs.long_term_memory_enabled:
                        continue
                    key = (
                        "maintenance:"
                        + hashlib.sha256((account.id + ":" + companion).encode()).hexdigest()
                    )
                    row = await db.get(DerivedView, (key, 1))
                    cursor = (
                        MaintenanceCursor.model_validate_json(row.document_json) if row else None
                    )
                    profile = embedding_gateway.profile if embedding_gateway else None
                    profile_key = profile.model_dump_json() if profile else "disabled"
                    revision, epoch = account.memory_revision, account.privacy_epoch
                    restart = (
                        cursor is None
                        or cursor.revision != revision
                        or cursor.privacy_epoch != epoch
                        or cursor.profile_id != profile_key
                        or cursor.next_audit_at <= self.clock()
                    )
                    if not restart and cursor.completed:
                        continue
                    query = select(FactVersion).where(
                        *fact_scope(scope),
                        FactVersion.status == "active",
                        FactVersion.system_to.is_(None),
                        FactVersion.record_json.is_not(None),
                    )
                    if not restart and cursor.after_memory_id:
                        query = query.where(FactVersion.memory_id > cursor.after_memory_id)
                    rows = list(await db.scalars(query.order_by(FactVersion.memory_id).limit(11)))
                    records = [load_record(r) for r in rows[:10]]
                    existing = set()
                    if profile and records:
                        existing = set(
                            (
                                await db.execute(
                                    select(VectorEntry.memory_id, VectorEntry.version).where(
                                        VectorEntry.account_id == account.id,
                                        VectorEntry.companion_id == companion,
                                        VectorEntry.profile_id == profile.profile_id,
                                        VectorEntry.index_generation == "index:" + profile.revision,
                                        VectorEntry.profile_json == profile.model_dump_json(),
                                        tuple_(VectorEntry.memory_id, VectorEntry.version).in_(
                                            [(r.ref.object_id, r.ref.version) for r in records]
                                        ),
                                    )
                                )
                            ).all()
                        )
                fence = VersionFence(privacy_epoch=epoch, memory_revision=revision, owner_epoch=0)

                def task_meta(
                    kind,
                    values,
                    scope=scope,
                    epoch=epoch,
                    profile_key=profile_key,
                    revision=revision,
                ):
                    operation_id = hashlib.sha256(
                        (
                            kind
                            + scope.model_dump_json()
                            + str(epoch)
                            + str(revision)
                            + "".join(r.ref.model_dump_json() for r in values)
                            + (profile_key if kind == "embed" else "v1")
                        ).encode()
                    ).hexdigest()
                    return meta(self, lane="maintenance", budget_ms=30000).model_copy(
                        update={"operation_id": operation_id}
                    )

                missing = [r for r in records if (r.ref.object_id, r.ref.version) not in existing]
                if profile and missing:
                    await self.enqueue_maintenance(
                        "embed",
                        EmbeddingInput(
                            meta=task_meta("embed", missing),
                            scope=scope,
                            fence=fence,
                            profile=profile,
                            purpose="index",
                            items=[(r.ref, render_record(r)) for r in missing],
                        ),
                    )
                    created += 1
                text_bytes = sum(len(r.display_text.encode()) for r in records)
                if records and text_bytes >= 1600:
                    await self.enqueue_maintenance(
                        "summarize",
                        CompressionInput(
                            meta=task_meta("summary", records),
                            scope=scope,
                            fence=fence,
                            source_records=records,
                            previous_summaries=[],
                            level="life_chapter",
                            period=TimeWindow(
                                start=min(r.recorded_at for r in records),
                                end=max(r.recorded_at for r in records),
                                precision="range",
                            ),
                            mandatory_refs=[r.ref for r in records],
                            policy=CompressionPolicy(
                                target_tokens=4096,
                                max_leaf_records=10,
                                verifier_version="extractive-summary.v1",
                            ),
                        ),
                    )
                    created += 1
                if model_available and len(records) >= 3:
                    from .storage import SourceRevision

                    source_ids = {p.event_id for r in records for p in r.sources}
                    async with self.database.sessions() as db:
                        provenance = list(
                            (
                                await db.execute(
                                    select(
                                        SourceRevision.session_id, SourceRevision.occurred_at
                                    ).where(
                                        SourceRevision.account_id == account_id,
                                        SourceRevision.companion_id == companion,
                                        SourceRevision.event_id.in_(source_ids),
                                        SourceRevision.revoked.is_(False),
                                    )
                                )
                            ).all()
                        )
                    if (
                        len({row.session_id for row in provenance}) >= 3
                        and len({row.occurred_at.date() for row in provenance}) >= 2
                    ):
                        await self.enqueue_maintenance(
                            "reflect",
                            ReflectionInput(
                                meta=task_meta("reflection", records),
                                scope=scope,
                                fence=fence,
                                records=records,
                            ),
                        )
                        created += 1
                old = [
                    r
                    for r in records
                    if (self.clock() - r.mentioned_at).days > 30
                    or (r.user_pinned and r.tier != "hot")
                ]
                if old:
                    await self.enqueue_maintenance(
                        "tier",
                        TieringInput(
                            meta=task_meta("tier", old),
                            scope=scope,
                            fence=fence,
                            records=old,
                            policy=TierPolicy(),
                            confirmed_recall_refs=[],
                        ),
                    )
                    created += 1
                # Bounded daily sample verifies the actual ciphertext/key/checksum,
                # independently from optional online-copy eviction.
                if (
                    store
                    and records
                    and restart
                    and cursor
                    and cursor.next_audit_at <= self.clock()
                ):
                    from .contracts import ArchiveManifest
                    from .tables import ObjectManifest

                    async with self.database.sessions() as db:
                        manifest = await db.scalar(
                            select(ObjectManifest)
                            .where(
                                ObjectManifest.account_id == account_id,
                                ObjectManifest.companion_id == companion,
                                ObjectManifest.kind == "archive",
                                ObjectManifest.state == "active",
                            )
                            .order_by(ObjectManifest.id)
                            .limit(1)
                        )
                        snapshot = (
                            ArchiveManifest.model_validate_json(manifest.manifest_json)
                            if manifest
                            else None
                        )
                    if snapshot:
                        audit_meta = task_meta(
                            "archive-audit:" + self.clock().date().isoformat(), records
                        )
                        await self.enqueue_maintenance(
                            "archive",
                            ArchiveInput(
                                meta=audit_meta,
                                scope=scope,
                                fence=fence,
                                operation="verify",
                                record_refs=[],
                                archive=snapshot,
                            ),
                        )
                        created += 1
                async with self.database.sessions.begin() as db:
                    current = await self._account(db, scope, lock=True)
                    if current.memory_revision != revision or current.privacy_epoch != epoch:
                        continue
                    value = MaintenanceCursor(
                        revision=revision,
                        privacy_epoch=epoch,
                        profile_id=profile_key,
                        after_memory_id=records[-1].ref.object_id if records else None,
                        completed=len(rows) <= 10,
                        next_audit_at=self.clock() + timedelta(days=1),
                    )
                    row = await db.get(DerivedView, (key, 1))
                    if row is None:
                        row = DerivedView(
                            id=key,
                            version=1,
                            account_id=account_id,
                            companion_id=companion,
                            kind="maint_cursor",
                            state="valid",
                        )
                        db.add(row)
                    row.document_json = value.model_dump_json()
                    if profile:
                        await self.settle_index_sources(db, scope, profile)
                        await db.flush()
                        await self.advance_watermarks(db, current)
            except MemoryFault as error:
                if error.info.code not in {
                    "overloaded",
                    "privacy_changed",
                    "not_found",
                    "version_conflict",
                }:
                    raise
        return created
