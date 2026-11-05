"""Bind a time clarification to one evidenced event, preserving its stable ID."""

import re
from datetime import timedelta

from sqlalchemy import select

from .contracts import CandidateMemory, Contract, EvidenceRef, SourceEvent
from .execution import MemoryFault
from .facts import fact_scope, load_record
from .lifecycle import contains, normalized, resolve_time
from .storage import SourceRevision
from .tables import FactSource, FactVersion
from .time_expressions import event_expression, meeting_topic
from .utils import utc


class EventUpdateResult(Contract):
    candidate: CandidateMemory
    requires_confirmation: bool = False


def comparison_context(statement):
    return sorted(
        (q.key, normalized(q.value))
        for q in statement.qualifiers
        if not (q.key == "context" and event_expression(q.value))
    )


class EventUpdateMixin:
    async def link_event_update(self, scope, event, candidate):
        result = EventUpdateResult(candidate=candidate)
        if (
            candidate.statement.predicate not in {"plan", "important_event"}
            or candidate.statement.polarity != "positive"
            or candidate.statement.valid_time.start is None
            or not re.search(r"会议|开会", event.content)
            or re.search(r"另外|另一个|另一场|还有|再开|第二场|第二个|新会议", event.content)
        ):
            return result
        # A definite time fragment ("下午三点的会议") is a refinement; a new
        # "有个会议" assertion without a clock must not merge independent events.
        expression = event_expression(event.content)
        if not expression or candidate.statement.valid_time.precision != "instant":
            return result
        explicit_move = bool(re.search(r"改到|改成|改为|挪到|推迟到|提前到|更正", event.content))
        if re.search(r"新增|新安排", event.content) and not explicit_move:
            return result
        matches = []
        async with self.database.sessions() as db:
            await self._account(db, scope)
            rows = list(
                await db.scalars(
                    select(FactVersion)
                    .join(
                        FactSource,
                        (FactSource.memory_id == FactVersion.memory_id)
                        & (FactSource.version == FactVersion.version),
                    )
                    .join(
                        SourceRevision,
                        (SourceRevision.event_id == FactSource.event_id)
                        & (SourceRevision.revision == FactSource.source_revision),
                    )
                    .where(
                        *fact_scope(scope),
                        FactVersion.subject_id == candidate.statement.subject_id,
                        FactVersion.predicate.in_(["plan", "important_event"]),
                        FactVersion.status == "active",
                        FactVersion.system_to.is_(None),
                        FactVersion.record_json.is_not(None),
                        SourceRevision.account_id == scope.user_id,
                        SourceRevision.companion_id == scope.companion_id,
                        SourceRevision.session_id == event.session_id,
                        SourceRevision.revoked.is_(False),
                        SourceRevision.source_json.is_not(None),
                        SourceRevision.expires_at > self.clock(),
                        SourceRevision.occurred_at >= event.occurred_at - timedelta(minutes=30),
                        SourceRevision.occurred_at <= event.occurred_at + timedelta(minutes=30),
                    )
                    .distinct()
                    .limit(21)
                )
            )
            if len(rows) > 20:
                return result.model_copy(update={"requires_confirmation": True})
            for row in rows:
                old = load_record(row)
                # A prior partial attempt may already have created a standalone
                # precise record for this exact source; the commit consolidates
                # it with the original event once their evidence is linked.
                if all(
                    p.event_id == event.event_id and p.source_revision == event.revision
                    for p in old.sources
                ):
                    continue
                if comparison_context(old.statement) != comparison_context(candidate.statement):
                    continue
                if not re.search(r"会议|开会", old.statement.canonical_value + old.display_text):
                    continue
                old_topic = meeting_topic(old.statement.canonical_value)
                new_topic = meeting_topic(candidate.statement.canonical_value)
                if old_topic and new_topic and old_topic != new_topic:
                    continue
                old_evidence = None
                old_window = old.statement.valid_time
                newest_support = None
                for pointer in old.sources:
                    try:
                        source = await self._source(
                            db, scope, pointer.event_id, pointer.source_revision
                        )
                    except MemoryFault:
                        continue
                    if not source.source_json or utc(source.expires_at) <= self.clock():
                        continue
                    source_event = SourceEvent.model_validate_json(source.source_json)
                    if source_event.session_id != event.session_id:
                        continue
                    quote = source_event.content[pointer.start_char : pointer.end_char]
                    if not quote or not re.search(r"会议|开会", quote):
                        continue
                    newest_support = max(
                        newest_support or source_event.occurred_at, source_event.occurred_at
                    )
                    if normalized(old.statement.canonical_value) in normalized(quote):
                        old_evidence = EvidenceRef(
                            event_id=pointer.event_id,
                            source_revision=pointer.source_revision,
                            start_char=pointer.start_char,
                            end_char=pointer.end_char,
                            quote=quote,
                            origin=source_event.origin,
                        )
                    if old_window.start is None:
                        old_expression = event_expression(quote)
                        if old_expression:
                            old_window, _, _ = resolve_time(
                                old_expression, source_event.occurred_at, "Asia/Shanghai"
                            )
                if old_evidence is None:
                    continue
                point = candidate.statement.valid_time.start
                if old_window.precision == "instant" and old_window.start != point:
                    if not explicit_move or newest_support > event.occurred_at:
                        matches.append((old, old_evidence, True))
                        continue
                elif (
                    old_window.start is not None
                    and not contains(old_window, point)
                    and not explicit_move
                ):
                    continue
                matches.append((old, old_evidence, False))
        if not matches:
            return result
        if len(matches) != 1 or matches[0][2]:
            return result.model_copy(update={"requires_confirmation": True})
        old, evidence, _ = matches[0]
        sources = {s.model_dump_json(): s for s in [evidence] + candidate.sources}
        linked = candidate.model_copy(
            update={
                "content_kind": old.content_kind,
                "statement": candidate.statement.model_copy(
                    update={
                        "predicate": old.statement.predicate,
                        "canonical_value": old.statement.canonical_value,
                        "qualifiers": old.statement.qualifiers,
                    }
                ),
                "sources": list(sources.values()),
            }
        )
        return EventUpdateResult(candidate=linked)
