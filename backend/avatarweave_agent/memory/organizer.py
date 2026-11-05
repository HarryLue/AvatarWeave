"""User pins and open-loop state, with immutable revisions and explicit controls."""

from datetime import timedelta
from typing import Annotated

from pydantic import AwareDatetime, Field
from sqlalchemy import func, select

from .contracts import (
    Contract,
    EvidencePointer,
    OpenLoop,
    PrivacyPreference,
    PublicOpenLoopPage,
    PublicOpenLoopView,
    VersionRef,
)
from .execution import MemoryFault
from .facts import fact_scope, invalidate, load_record
from .tables import ControlOperation, Dependency, DerivedView, FactVersion


class PinMemoryBody(Contract):
    expected_version: Annotated[int, Field(ge=1)]
    pinned: bool
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]


class FollowupReservation(Contract):
    generation_id: str
    reserved_at: AwareDatetime
    # This is an attempt reservation, never evidence that a question was heard.
    expires_at: AwareDatetime


def loop_view(loop, now):
    due = loop.event_time.end or loop.event_time.start
    state = "pending_outcome" if loop.status == "planned" and due and due < now else loop.status
    return PublicOpenLoopView(
        loop_id=loop.ref.object_id,
        version=loop.ref.version,
        title=loop.title,
        event_time=loop.event_time,
        status=state,
        user_allows_followup=loop.user_allows_followup,
    )


class OrganizerMixin:
    async def _eligible_loop(self, db, scope, ref, now):
        row = await db.get(DerivedView, (ref.object_id, ref.version))
        if (
            row is None
            or row.account_id != scope.user_id
            or row.companion_id != scope.companion_id
            or row.kind != "open_loop"
            or row.state != "valid"
        ):
            raise MemoryFault("version_conflict", "Open loop snapshot changed")
        value = OpenLoop.model_validate_json(row.document_json)
        due = value.event_time.end or value.event_time.start
        if (
            not value.user_allows_followup
            or value.status not in {"planned", "rescheduled", "pending_outcome"}
            or due is None
            or due >= now
            or (
                value.last_followup_at is not None
                and now - value.last_followup_at < timedelta(hours=72)
            )
        ):
            return None
        for source in value.sources:
            await self._source(db, scope, source.event_id, source.source_revision)
        if not value.linked_memory_refs:
            return None
        for linked in value.linked_memory_refs:
            fact = await db.scalar(
                select(FactVersion).where(
                    *fact_scope(scope),
                    FactVersion.memory_id == linked.object_id,
                    FactVersion.version == linked.version,
                    FactVersion.system_to.is_(None),
                    FactVersion.status == "active",
                )
            )
            if fact is None or load_record(fact).sensitivity != "normal":
                return None
        return value

    async def reserve_followup(self, scope, fence, generation_id):
        """Reserve at most one due, explicitly permitted question for an active turn.

        Reserve before model I/O so parallel sessions cannot repeatedly propose the
        same question. Failed/interrupted attempts also cool down conservatively.
        The reservation stores only IDs/times and does not assert a spoken outcome.
        """
        from .contracts import ScopedRequest
        from .http import meta

        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            self._privacy(account, ScopedRequest(meta=meta(self), scope=scope, fence=fence))
            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            if not prefs.long_term_memory_enabled or not prefs.allow_proactive_followup:
                return []
            now = self.clock()
            rows = list(
                await db.scalars(
                    select(DerivedView)
                    .where(
                        DerivedView.account_id == account.id,
                        DerivedView.companion_id == scope.companion_id,
                        DerivedView.kind == "open_loop",
                        DerivedView.state == "valid",
                    )
                    .order_by(DerivedView.id)
                    .limit(32)
                )
            )
            for row in rows:
                ref = VersionRef(object_id=row.id, version=row.version)
                try:
                    value = await self._eligible_loop(db, scope, ref, now)
                except MemoryFault as error:
                    if error.info.code in {"evidence_missing", "version_conflict"}:
                        continue
                    raise
                if value is None:
                    continue
                import hashlib

                key = hashlib.sha256(
                    (account.id + scope.companion_id + row.id).encode()
                ).hexdigest()
                attempt = await db.get(DerivedView, ("followup:" + key, 1))
                if attempt is not None:
                    previous = FollowupReservation.model_validate_json(attempt.document_json)
                    if previous.generation_id == generation_id:
                        return [ref]
                    if previous.expires_at > now:
                        continue
                else:
                    attempt = DerivedView(
                        id="followup:" + key,
                        version=1,
                        account_id=account.id,
                        companion_id=scope.companion_id,
                        kind="followup_attempt",
                        state="valid",
                    )
                    db.add(attempt)
                attempt.document_json = FollowupReservation(
                    generation_id=generation_id,
                    reserved_at=now,
                    expires_at=now + timedelta(hours=72),
                ).model_dump_json()
                return [ref]
        return []

    async def pin_memory(self, scope, memory_id, body):
        _event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="pin_memory",
            body=body,
            text="pin:" + memory_id,
            target=memory_id,
        )
        if receipt:
            ref = VersionRef.model_validate_json(receipt)
            return await self.public_memory(scope, ref.object_id, version=ref.version)
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            row = await db.scalar(
                select(FactVersion).where(
                    *fact_scope(scope),
                    FactVersion.memory_id == memory_id,
                    FactVersion.version == body.expected_version,
                    FactVersion.system_to.is_(None),
                    FactVersion.status == "active",
                )
            )
            if row is None:
                raise MemoryFault("version_conflict", "Pin target changed")
            old = load_record(row)
            record = old.model_copy(
                update={
                    "ref": VersionRef(object_id=memory_id, version=old.ref.version + 1),
                    "user_pinned": body.pinned,
                    "system_from": self.clock(),
                    "system_to": None,
                }
            )
            row.status, row.system_to = "superseded", self.clock()
            await invalidate(db, account.id, [old.ref])
            await self._insert_version(db, record, row.fact_key)
            await self._sync_open_loop(db, record)
            account.memory_revision += 1
            control = await db.get(ControlOperation, (account.id, body.idempotency_key))
            control.result_json = record.ref.model_dump_json()
        return await self.public_memory(scope, memory_id)

    async def _sync_open_loop(self, db, record):
        if record.content_kind != "future_intent":
            return
        loop_id = record.ref.object_id + "-loop"
        previous = await db.scalar(
            select(DerivedView)
            .where(
                DerivedView.id == loop_id,
                DerivedView.account_id == record.scope.user_id,
                DerivedView.kind == "open_loop",
            )
            .order_by(DerivedView.version.desc())
            .limit(1)
        )
        old = (
            OpenLoop.model_validate_json(previous.document_json)
            if previous and previous.document_json
            else None
        )
        version = previous.version + 1 if previous else 1
        value = OpenLoop(
            ref=VersionRef(object_id=loop_id, version=version),
            title=record.statement.canonical_value,
            event_time=record.statement.valid_time,
            timezone="Asia/Shanghai",
            status=old.status if old else "planned",
            sources=record.sources[:32],
            linked_memory_refs=[record.ref],
            user_allows_followup=old.user_allows_followup if old else False,
            last_followup_at=old.last_followup_at if old else None,
        )
        if previous:
            previous.state = "superseded"
        db.add(
            DerivedView(
                id=loop_id,
                version=version,
                account_id=record.scope.user_id,
                companion_id=record.scope.companion_id,
                kind="open_loop",
                state="valid",
                document_json=value.model_dump_json(),
            )
        )
        db.add(
            Dependency(
                account_id=record.scope.user_id,
                parent_id=record.ref.object_id,
                parent_version=record.ref.version,
                child_id=loop_id,
                child_version=version,
                relation="open_loop_of",
            )
        )

    async def list_open_loops(self, scope, page):
        async with self.database.sessions() as db:
            await self._account(db, scope)
            query = select(DerivedView).where(
                DerivedView.account_id == scope.user_id,
                DerivedView.companion_id == scope.companion_id,
                DerivedView.kind == "open_loop",
                DerivedView.state == "valid",
            )
            if page.cursor:
                query = query.where(DerivedView.id > page.cursor)
            rows = list(await db.scalars(query.order_by(DerivedView.id).limit(page.limit + 1)))
            return PublicOpenLoopPage(
                items=[
                    loop_view(OpenLoop.model_validate_json(row.document_json), self.clock())
                    for row in rows[: page.limit]
                ],
                next_cursor=rows[page.limit - 1].id if len(rows) > page.limit else None,
            )

    async def update_open_loop(self, scope, loop_id, body):
        event, receipt = await self.control_event(
            scope,
            key=body.idempotency_key,
            operation="update_open_loop",
            body=body,
            text="update_open_loop:" + loop_id,
            target=loop_id,
        )
        if receipt:
            return PublicOpenLoopView.model_validate_json(receipt)
        async with self.database.sessions.begin() as db:
            account = await self._account(db, scope, lock=True)
            row = await db.get(DerivedView, (loop_id, body.expected_version))
            if (
                row is None
                or row.account_id != account.id
                or row.companion_id != scope.companion_id
                or row.kind != "open_loop"
                or row.state != "valid"
            ):
                raise MemoryFault("version_conflict", "Open loop version changed")
            old = OpenLoop.model_validate_json(row.document_json)
            for pointer in old.sources:
                await self._source(db, scope, pointer.event_id, pointer.source_revision)
            prefs = PrivacyPreference.model_validate_json(account.preferences_json)
            if body.user_allows_followup and not prefs.allow_proactive_followup:
                raise MemoryFault(
                    "invalid_transition", "Enable proactive follow-up in preferences first"
                )
            if body.status == "rescheduled" and body.event_time is None:
                raise MemoryFault("invalid_transition", "Rescheduling requires an explicit time")
            version = await db.scalar(
                select(func.max(DerivedView.version)).where(
                    DerivedView.id == loop_id, DerivedView.account_id == account.id
                )
            )
            value = old.model_copy(
                update={
                    "ref": VersionRef(object_id=loop_id, version=version + 1),
                    "status": body.status,
                    "event_time": body.event_time or old.event_time,
                    "user_allows_followup": body.user_allows_followup,
                    "sources": old.sources[-31:]
                    + [
                        EvidencePointer(
                            event_id=event.event_id,
                            source_revision=1,
                            level="raw",
                            capsule_id=None,
                            start_char=0,
                            end_char=len(event.content),
                        )
                    ],
                }
            )
            row.state = "superseded"
            db.add(
                DerivedView(
                    id=loop_id,
                    version=version + 1,
                    account_id=account.id,
                    companion_id=scope.companion_id,
                    kind="open_loop",
                    state="valid",
                    document_json=value.model_dump_json(),
                )
            )
            for ref in value.linked_memory_refs:
                db.add(
                    Dependency(
                        account_id=account.id,
                        parent_id=ref.object_id,
                        parent_version=ref.version,
                        child_id=loop_id,
                        child_version=version + 1,
                        relation="open_loop_of",
                    )
                )
            account.memory_revision += 1
            view = loop_view(value, self.clock())
            control = await db.get(ControlOperation, (account.id, body.idempotency_key))
            control.result_json = view.model_dump_json()
            return view
