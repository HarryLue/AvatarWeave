"""Trusted LiveKit adaptation: final sources, current context and generation fences."""

import asyncio
from datetime import timedelta
from time import monotonic
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select

from .contracts import (
    Budget,
    ContextInput,
    EmbeddingProfile,
    InputEventInput,
    PersonaInput,
    QueryInput,
    RetrievalInput,
    SourceEvent,
    VersionFence,
)
from .execution import MemoryFault
from .http import meta
from .storage import Account, MemorySession, SourceRevision


class PrivateConversation:
    def __init__(self, service, session, scope, owner_epoch, *, persona, embedding_gateway=None):
        self.service, self.session, self.scope, self.owner_epoch = (
            service,
            session,
            scope,
            owner_epoch,
        )
        self.persona = persona
        self.embedding_gateway = embedding_gateway
        self.current_event = None
        self.current_context = None
        self.generation_id = None
        self.fence = None
        self.tone = None
        self.started = monotonic()
        self.recent_events = []
        self.recent_replies = []
        self.recent_reply_fence = None
        self.output_plan = None
        self.output_published = False
        self.last_persisted = False
        self.turn_memory_enabled = False
        self._turn_lock = asyncio.Lock()
        self.preferences = None
        self.profile = (
            embedding_gateway.profile
            if embedding_gateway
            else EmbeddingProfile(
                profile_id="lexical-only",
                model_id="disabled",
                revision="v1",
                dimensions=1,
                metric="cosine",
                normalized=True,
            )
        )

    @classmethod
    async def bind(cls, service, session_ref, room, worker_id, *, persona, embedding_gateway=None):
        from .contracts import Scope

        async with service.database.sessions() as db:
            session = await db.scalar(
                select(MemorySession).where(
                    MemorySession.id == session_ref, MemorySession.room == room
                )
            )
            if session is None:
                raise MemoryFault("not_found", "Private session binding is invalid")
            account = await db.get(Account, session.account_id)
            scope = Scope(
                workspace_id=account.workspace_id,
                user_id=account.id,
                companion_id=session.companion_id,
            )
        owner = await service.claim_owner(scope, session.id, room, worker_id)
        if session.memory_enabled:
            await service.register_persona(scope, persona)
        return cls(
            service, session, scope, owner, persona=persona, embedding_gateway=embedding_gateway
        )

    async def current_fence(self):
        from .contracts import PrivacyPreference

        async with self.service.database.sessions() as db:
            account = await self.service._account(db, self.scope)
            self.preferences = PrivacyPreference.model_validate_json(account.preferences_json)
            return VersionFence(
                privacy_epoch=account.privacy_epoch,
                memory_revision=account.memory_revision,
                owner_epoch=self.owner_epoch,
            )

    async def admit(self, turn_id, text, *, origin="user_transcript", occurred_at=None):
        async with asyncio.timeout(0.3), self._turn_lock:
            return await self._admit(turn_id, text, origin=origin, occurred_at=occurred_at)

    async def _admit(self, turn_id, text, *, origin="user_transcript", occurred_at=None):
        if origin not in {"user_text", "user_transcript"}:
            raise MemoryFault("invalid_transition", "Unsupported private input origin")
        current_fence = await self.current_fence()
        if self.recent_reply_fence and (
            self.recent_reply_fence.privacy_epoch != current_fence.privacy_epoch
            or self.recent_reply_fence.memory_revision != current_fence.memory_revision
        ):
            self.recent_replies = []
        if (
            self.current_event
            and self.current_event.event_id == turn_id
            and self.current_event.content == text
        ):
            self.turn_memory_enabled = (
                self.last_persisted
                and self.session.memory_enabled
                and self.preferences.long_term_memory_enabled
            )
            return self.current_event
        self.started = monotonic()
        if self.fence and self.fence.privacy_epoch != current_fence.privacy_epoch:
            self.recent_events = []
        now = self.service.clock()
        event = SourceEvent(
            event_id=turn_id,
            revision=1,
            session_id=self.session.id,
            turn_id=turn_id,
            scope=self.scope,
            origin=origin,
            content=text,
            occurred_at=occurred_at or now,
            received_at=now,
            is_final=True,
        )
        # A commit acknowledgement can be lost at a deadline. A replay of the
        # same RTC turn reuses its persisted timestamps rather than inventing a
        # different event with the same idempotency identity.
        async with self.service.database.sessions() as db:
            saved = await db.scalar(
                select(SourceRevision).where(
                    SourceRevision.event_id == turn_id,
                    SourceRevision.revision == 1,
                    SourceRevision.account_id == self.scope.user_id,
                    SourceRevision.companion_id == self.scope.companion_id,
                    SourceRevision.session_id == self.session.id,
                    SourceRevision.revoked.is_(False),
                )
            )
            if saved and saved.source_json:
                previous = SourceEvent.model_validate_json(saved.source_json)
                if previous.content != text:
                    raise MemoryFault(
                        "version_conflict",
                        "Final input already exists; use source revision controls",
                    )
                event = previous
        result = await self.service.input_event(
            InputEventInput(
                meta=meta(self.service, lane="dialogue"),
                scope=self.scope,
                fence=current_fence,
                session_id=self.session.id,
                event=event,
                memory_enabled=self.session.memory_enabled,
                expected_turn_revision=0,
            )
        )
        if result.error:
            raise MemoryFault(
                result.error.code, result.error.message, retryable=result.error.retryable
            )
        self.last_persisted = result.data.persisted
        self.turn_memory_enabled = result.data.persisted and self.session.memory_enabled
        self.current_event, self.current_context = event, None
        self.recent_events = (self.recent_events + [event])[-8:]
        return event

    async def prepare(
        self, turn_id, text, *, greeting=False, origin="user_transcript", occurred_at=None
    ):
        async with asyncio.timeout(0.3), self._turn_lock:
            return await self._prepare(
                turn_id, text, greeting=greeting, origin=origin, occurred_at=occurred_at
            )

    async def _prepare(
        self, turn_id, text, *, greeting=False, origin="user_transcript", occurred_at=None
    ):
        if self.current_context and self.current_event and self.current_event.event_id == turn_id:
            await self.check()
            return self.current_context
        if not self.current_event or self.current_event.event_id != turn_id:
            self.started = monotonic()
        async with asyncio.timeout(0.3):
            if greeting:
                now = self.service.clock()
                event = SourceEvent(
                    event_id=turn_id,
                    revision=1,
                    session_id=self.session.id,
                    turn_id=turn_id,
                    scope=self.scope,
                    origin="output_observation",
                    content=text,
                    occurred_at=now,
                    received_at=now,
                    is_final=True,
                )
                self.current_event = event
                self.last_persisted, self.turn_memory_enabled = False, False
            else:
                event = await self._admit(turn_id, text, origin=origin, occurred_at=occurred_at)
            self.generation_id, self.fence = await self.service.begin_generation(
                self.scope, self.session.id, self.owner_epoch
            )
            refreshed = await self.current_fence()
            if refreshed.privacy_epoch != self.fence.privacy_epoch:
                raise MemoryFault("privacy_changed", "Memory preference changed during preparation")
            self.turn_memory_enabled = (
                self.turn_memory_enabled and self.preferences.long_term_memory_enabled
            )
            budget = Budget()
            tone_result = await self.service.persona(
                PersonaInput(
                    meta=meta(self.service),
                    scope=self.scope,
                    fence=self.fence,
                    persona_id=self.persona.persona_id,
                    requested_version=self.persona.version,
                    current_tone=self.tone,
                    latest_event=event,
                    interaction_preferences=[],
                ),
                self.persona,
                [],
            )
            if tone_result.error:
                raise MemoryFault(tone_result.error.code, tone_result.error.message)
            self.tone = tone_result.data.tone
            hits = []
            if self.turn_memory_enabled and not greeting:
                query_result = await self.service.query(
                    QueryInput(
                        meta=meta(self.service, lane="dialogue"),
                        scope=self.scope,
                        fence=self.fence,
                        latest_event=event,
                        recent_topic=None,
                        unresolved_refs=[],
                        known_entity_ids=[],
                        budget=budget,
                    )
                )
                if query_result.error:
                    raise MemoryFault(query_result.error.code, query_result.error.message)
                recalled = await self.service.retrieve(
                    RetrievalInput(
                        meta=meta(self.service, lane="dialogue"),
                        scope=self.scope,
                        fence=self.fence,
                        queries=query_result.data.queries,
                        profile=self.profile,
                        budget=budget,
                        include_historical=any(q.time_filter for q in query_result.data.queries),
                    ),
                    self.embedding_gateway,
                )
                if recalled.status == "error":
                    raise MemoryFault(recalled.error.code, recalled.error.message)
                hits = recalled.data.candidates
            loop_refs = (
                await self.service.reserve_followup(self.scope, self.fence, self.generation_id)
                if self.turn_memory_enabled
                and not greeting
                and self.preferences.allow_proactive_followup
                else []
            )
            result = await self.service.context(
                ContextInput(
                    meta=meta(self.service, lane="dialogue"),
                    scope=self.scope,
                    fence=self.fence,
                    persona=self.persona,
                    tone=self.tone,
                    latest_event=event,
                    recent_messages=list(self.recent_replies),
                    summaries=[],
                    retrieved=hits,
                    active_loop_refs=loop_refs,
                    generation_id=self.generation_id,
                    budget=budget,
                )
            )
            if result.error:
                raise MemoryFault(result.error.code, result.error.message)
            from .contracts import ClientCapabilities, OutputInput, OutputPlan

            self.output_plan = OutputPlan(
                output_id=uuid4().hex,
                session_id=self.session.id,
                turn_id=turn_id,
                generation_id=self.generation_id,
                speech_id=None,
                spoken_text=None,
                action_ids=[],
                sequence=0,
                expires_at=self.service.clock() + timedelta(seconds=45),
            )
            self.output_published = False
            planned = await self.service.output(
                OutputInput(
                    meta=meta(self.service),
                    scope=self.scope,
                    fence=self.fence,
                    operation="plan",
                    plan=self.output_plan,
                    capabilities=ClientCapabilities.model_validate_json(
                        self.session.capabilities_json
                    ),
                    observations=[],
                )
            )
            if planned.error:
                raise MemoryFault(planned.error.code, planned.error.message)
            from .contracts import VersionRef

            await self.service.track_generation(
                self.scope,
                self.session.id,
                self.generation_id,
                [
                    m.ref
                    for m in result.data.manifest
                    if m.decision != "excluded" and m.ref not in loop_refs
                ],
            )
            if not greeting and self.last_persisted:
                await self.service.track_generation(
                    self.scope,
                    self.session.id,
                    self.generation_id,
                    [VersionRef(object_id=event.event_id, version=event.revision)],
                    kind="source",
                )
            if loop_refs:
                await self.service.track_generation(
                    self.scope,
                    self.session.id,
                    self.generation_id,
                    [
                        entry.ref
                        for entry in result.data.manifest
                        if entry.ref in loop_refs and entry.decision != "excluded"
                    ],
                    kind="open_loop",
                )
            self.current_context = result.data
            return result.data

    def record_generated_reply(self, generation, fence, turn_id, text, output_plan):
        from .contracts import RecentMessage

        if not text or len(text) > 4000 or generation != self.generation_id or fence != self.fence:
            return
        self.recent_replies = (
            self.recent_replies
            + [
                RecentMessage(
                    message_id=output_plan.output_id,
                    role="assistant",
                    text=text,
                    output_evidence="published"
                    if output_plan.output_id in getattr(self, "published_outputs", {})
                    else "generated",
                    source_event_id=turn_id,
                )
            ]
        )[-6:]
        self.recent_reply_fence = fence

    async def source_overlay(self):
        if not self.turn_memory_enabled:
            remaining, selected = 1200, []
            for event in reversed(self.recent_events):
                cost = len(event.content.encode())
                if event.event_id != self.current_event.event_id and cost <= remaining:
                    selected.append(event)
                    remaining -= cost
            return list(reversed(selected))
        async with self.service.database.sessions() as db:
            account = await self.service._account(db, self.scope)
            if self.fence.privacy_epoch != account.privacy_epoch:
                raise MemoryFault("privacy_changed", "Memory authorization changed")
            from .tables import ControlOperation, EvidenceSuppression

            suppressed = exists(
                select(EvidenceSuppression.event_id).where(
                    EvidenceSuppression.account_id == account.id,
                    EvidenceSuppression.event_id == SourceRevision.event_id,
                    EvidenceSuppression.source_revision == SourceRevision.revision,
                )
            )
            correction_sources = select(ControlOperation.event_id).where(
                ControlOperation.account_id == account.id,
                ControlOperation.operation == "correct_memory",
            )
            rows = list(
                await db.scalars(
                    select(SourceRevision)
                    .where(
                        SourceRevision.account_id == self.scope.user_id,
                        SourceRevision.companion_id == self.scope.companion_id,
                        SourceRevision.source_json.is_not(None),
                        ~suppressed,
                        SourceRevision.revoked.is_(False),
                        SourceRevision.expires_at > self.service.clock(),
                        or_(
                            SourceRevision.origin.in_(["user_text", "user_transcript"]),
                            and_(
                                SourceRevision.origin == "user_ui",
                                or_(
                                    SourceRevision.revision > 1,
                                    SourceRevision.event_id.in_(correction_sources),
                                ),
                            ),
                        ),
                    )
                    .order_by(SourceRevision.sequence.desc())
                    .limit(8)
                )
            )
            remaining, result = 1200, []
            for row in rows:
                event = SourceEvent.model_validate_json(row.source_json)
                if self.current_event and event.event_id == self.current_event.event_id:
                    continue
                cost = len(event.content.encode())
                if cost <= remaining:
                    result.append(event)
                    remaining -= cost
            result.reverse()
        from .contracts import VersionRef

        await self.service.track_generation(
            self.scope,
            self.session.id,
            self.generation_id,
            [VersionRef(object_id=e.event_id, version=e.revision) for e in result],
            kind="source",
        )
        return result

    async def check(self, generation_id=None, fence=None):
        if not (generation_id or self.generation_id) or not await self.service.generation_valid(
            self.scope, self.session.id, generation_id or self.generation_id, fence or self.fence
        ):
            raise MemoryFault("stale_generation", "Conversation generation was invalidated")

    async def cancel(self):
        if self.generation_id:
            await self.service.cancel_generation(self.scope, self.session.id, self.generation_id)

    async def publish_output(self, generation_id, fence, *, output_plan=None):
        plan = output_plan or self.output_plan
        published = getattr(self, "published_outputs", {})
        if plan is None or plan.generation_id != generation_id:
            raise MemoryFault("stale_generation", "Output does not belong to this speech")
        if plan.output_id in published:
            return
        from .contracts import ClientCapabilities, OutputInput

        result = await self.service.output(
            OutputInput(
                meta=meta(self.service),
                scope=self.scope,
                fence=fence,
                operation="publish",
                plan=plan,
                capabilities=ClientCapabilities.model_validate_json(self.session.capabilities_json),
                observations=[],
            )
        )
        if result.error:
            raise MemoryFault(result.error.code, result.error.message)
        self.output_published = True
        published[plan.output_id] = True
        if len(published) > 32:
            published.pop(next(iter(published)))
        self.published_outputs = published
