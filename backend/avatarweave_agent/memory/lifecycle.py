"""Deterministic lifecycle rules; language models can propose, never authorize writes."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .contracts import (
    ConflictInput,
    ConflictOutput,
    ConflictPair,
    MutationProposal,
    PredicateRule,
    TemporalInput,
    TemporalOutput,
    TemporalResolution,
    TimeWindow,
)
from .execution import MemoryFault

REGISTRY_VERSION = "life-predicates.v1"
PREDICATES = {
    name: PredicateRule(
        predicate=name,
        cardinality=cardinality,
        value_type="text",
        mutable=True,
        compare_qualifier_keys=["context", "target", "relationship_type"],
        default_half_life_days=365.0,
    )
    for name, cardinality in [
        ("name", "single"),
        ("current_city", "single"),
        ("occupation", "single"),
        ("preference", "set"),
        ("interaction_boundary", "set"),
        ("important_event", "event"),
        ("plan", "event"),
        ("relationship", "set"),
    ]
}


def normalized(value):
    return unicodedata.normalize("NFKC", value).strip()


def enclosing_clause(text, start, end):
    boundaries = list(re.finditer(r"[，,。！？!?；;\n]|但是|不过|但", text))
    left = max((m.end() for m in boundaries if m.end() <= start), default=0)
    right = min((m.start() for m in boundaries if m.start() >= end), default=len(text))
    return left, right


def display_statement(candidate, user_id):
    statement = candidate.statement
    if candidate.display_text == statement.canonical_value:
        text = statement.canonical_value
        expression = statement.valid_time.original_expression
        if (
            statement.predicate in {"plan", "important_event"}
            and expression
            and expression not in text
        ):
            text += "；时间：" + expression
        return text
    labels = {
        "name": "姓名",
        "current_city": "居住城市",
        "occupation": "职业",
        "preference": "偏好",
        "interaction_boundary": "互动约定",
        "important_event": "经历",
        "plan": "计划",
        "relationship": "关系",
    }
    subject = "你" if statement.subject_id == user_id else "提到的人"
    text = f"{subject}的{labels[statement.predicate]}：{statement.canonical_value}"
    if statement.polarity == "negative":
        text = "明确否定：" + text
    if statement.qualifiers:
        names = {"context": "情境", "target": "对象", "relationship_type": "关系类型"}
        text += "（" + "；".join(names[q.key] + "：" + q.value for q in statement.qualifiers) + "）"
    expression = statement.valid_time.original_expression
    if statement.predicate in {"plan", "important_event"} and expression and expression not in text:
        text += "；时间：" + expression
    return text


def rule_for(candidate):
    rule = PREDICATES.get(candidate.statement.predicate)
    if rule is None or candidate.statement.value_type != rule.value_type:
        raise MemoryFault(
            "manual_review_required", "Predicate is not registered for this value type"
        )
    qualifiers = candidate.statement.qualifiers
    if len({q.key for q in qualifiers}) != len(qualifiers):
        raise MemoryFault("invalid_model_output", "Duplicate qualifier keys")
    if any(q.key not in rule.compare_qualifier_keys for q in qualifiers):
        raise MemoryFault("manual_review_required", "Unregistered comparison qualifier")
    return rule


def fact_key(scope, candidate):
    rule = rule_for(candidate)
    statement = candidate.statement
    values = [
        scope.model_dump(),
        candidate.namespace,
        statement.subject_id,
        statement.predicate,
        sorted(
            (q.key, normalized(q.value))
            for q in statement.qualifiers
            if q.key in rule.compare_qualifier_keys
        ),
    ]
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def overlaps(a, b):
    # Equal endpoints are point events; all other intervals are left-closed/right-open.
    if a.start is not None and a.start == a.end:
        return contains(b, a.start)
    if b.start is not None and b.start == b.end:
        return contains(a, b.start)
    return not (
        (a.end is not None and b.start is not None and a.end <= b.start)
        or (b.end is not None and a.start is not None and b.end <= a.start)
    )


def contains(window, instant):
    if window.start is not None and window.start == window.end:
        return instant == window.start
    return (window.start is None or window.start <= instant) and (
        window.end is None or instant < window.end
    )


def same_value(a, b):
    return (
        normalized(a.canonical_value) == normalized(b.canonical_value) and a.polarity == b.polarity
    )


def supports_event_time(candidate, record):
    old, new = record.statement, candidate.statement
    if new.valid_time.precision != "instant" or new.polarity != old.polarity:
        return False
    keys = {(p.event_id, p.source_revision, p.start_char, p.end_char) for p in record.sources}
    if not any(
        (e.event_id, e.source_revision, e.start_char, e.end_char) in keys for e in candidate.sources
    ):
        return False
    if not same_value(old, new):
        from .time_expressions import meeting_topic

        if len({(s.event_id, s.source_revision) for s in candidate.sources}) < 2 or not all(
            re.search(r"会议|开会", s.canonical_value) for s in (old, new)
        ):
            return False
        a, b = meeting_topic(old.canonical_value), meeting_topic(new.canonical_value)
        if a and b and a != b:
            return False
    if old.valid_time.start is None or contains(old.valid_time, new.valid_time.start):
        return True
    return any(
        re.search(r"改到|改成|改为|挪到|推迟到|提前到|更正", e.quote)
        for e in candidate.sources
        if (e.event_id, e.source_revision, e.start_char, e.end_char) not in keys
    )


def resolve_conflict(request: ConflictInput) -> ConflictOutput:
    candidate, rule = request.candidate, rule_for(request.candidate)
    if request.predicate_rule != rule:
        raise MemoryFault(
            "invalid_model_output", "Predicate policy must come from the server registry"
        )
    comparisons, proposals = [], []
    new = candidate.statement
    if candidate.interpretation not in {"asserted", "negated"}:
        return ConflictOutput(
            comparisons=[],
            proposals=[
                MutationProposal(
                    action="defer",
                    candidate_id=candidate.candidate_id,
                    target_refs=[],
                    reason="Statement needs clarification",
                    requires_user_confirmation=True,
                )
            ],
            clarification="请确认这句话是否描述真实经历。",
        )
    for record in request.comparable_records:
        if record.scope != request.scope:
            raise MemoryFault("scope_mismatch", "Conflict records cross the authorized scope")
        if record.status not in {"active", "uncertain"} or record.system_to is not None:
            continue
        old = record.statement
        if (
            record.namespace != candidate.namespace
            or old.subject_id != new.subject_id
            or old.predicate != new.predicate
        ):
            continue
        old_q = sorted((q.key, normalized(q.value)) for q in old.qualifiers)
        new_q = sorted((q.key, normalized(q.value)) for q in new.qualifiers)
        action, confirmation = None, False
        if old_q != new_q:
            relation = "conditional_coexist"
        elif rule.cardinality == "event" and supports_event_time(candidate, record):
            relation, action = "support", "add_evidence"
        elif not overlaps(old.valid_time, new.valid_time):
            relation = "temporal_change"
        elif same_value(old, new) and (
            rule.cardinality != "event"
            or (
                old.valid_time == new.valid_time
                and any(
                    p.event_id == e.event_id and p.source_revision == e.source_revision
                    for p in record.sources
                    for e in candidate.sources
                )
            )
        ):
            relation, action = "support", "add_evidence"
        elif rule.cardinality in {"set", "event"} and old.polarity == new.polarity:
            relation = "independent"
        elif (
            new.polarity == "negative"
            and old.polarity == "positive"
            and normalized(old.canonical_value) == normalized(new.canonical_value)
            and any(
                re.search(r"从来没|从没|从未|转写错误|记错", e.quote) for e in candidate.sources
            )
        ):
            relation, action = "contradiction", "retract"
        elif (
            new.valid_time.start is not None
            and old.valid_time.end is None
            and (old.valid_time.start is None or new.valid_time.start > old.valid_time.start)
            and rule.mutable
        ):
            relation, action = "temporal_change", "replace_interval"
        else:
            relation, action, confirmation = "contradiction", "defer", True
        comparisons.append(
            ConflictPair(
                candidate_id=candidate.candidate_id,
                existing_ref=record.ref,
                relation=relation,
                reason=relation,
                overlapping_time=new.valid_time
                if overlaps(old.valid_time, new.valid_time)
                else None,
            )
        )
        if action:
            proposals.append(
                MutationProposal(
                    action=action,
                    candidate_id=candidate.candidate_id,
                    target_refs=[record.ref],
                    new_statement=new if action != "defer" else None,
                    reason=relation,
                    requires_user_confirmation=confirmation,
                )
            )
    if any(p.action == "defer" for p in proposals):
        proposals = [p for p in proposals if p.action == "defer"]
    elif (
        rule.cardinality == "event"
        and len(proposals) > 1
        and all(p.action == "add_evidence" for p in proposals)
    ):
        # Reconcile a prior partial extraction without deleting either history.
        targets = {r.ref.model_dump_json(): r for r in request.comparable_records}
        refs = sorted(
            [ref for p in proposals for ref in p.target_refs],
            key=lambda ref: (
                not same_value(targets[ref.model_dump_json()].statement, new),
                targets[ref.model_dump_json()].recorded_at,
                ref.object_id,
            ),
        )
        proposals = [proposals[0].model_copy(update={"target_refs": refs})]
    elif not proposals:
        proposals = [
            MutationProposal(
                action="create",
                candidate_id=candidate.candidate_id,
                target_refs=[],
                new_statement=new,
                reason="Independent supported statement",
                requires_user_confirmation=False,
            )
        ]
    return ConflictOutput(
        comparisons=comparisons,
        proposals=proposals,
        clarification="新旧信息冲突，请确认是生活变化还是纠正旧信息。"
        if any(p.requires_user_confirmation for p in proposals)
        else None,
    )


def resolve_time(expression, anchor, timezone):
    if not expression:
        return TimeWindow(precision="unknown"), "unknown", None
    from .time_expressions import resolve_clock

    if clock := resolve_clock(expression, anchor, timezone):
        return clock
    zone = ZoneInfo(timezone)
    local = anchor.astimezone(zone)
    start, end, precision, confidence = None, None, "unknown", "unknown"
    try:
        day = re.search(r"(?<!\d)(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?", expression)
        month = re.fullmatch(r"(\d{4})年(\d{1,2})月", expression)
        year = re.fullmatch(r"(\d{4})年", expression)
        years_ago = re.fullmatch(r"(\d{1,3}|[一二两三四五六七八九十]{1,3})年前", expression)
        year_range = re.fullmatch(r"(\d{4})年(?:到|至|—|-)(\d{4})年", expression)
        if year_range:
            a, b = map(int, year_range.groups())
            if a > b:
                raise ValueError("Reversed period")
            start, end = datetime(a, 1, 1, tzinfo=zone), datetime(b + 1, 1, 1, tzinfo=zone)
            precision, confidence = "range", "explicit"
        elif years_ago:
            from .time_expressions import number

            n = number(years_ago[1])
            y = local.year - n
            start, end = datetime(y, 1, 1, tzinfo=zone), datetime(y + 1, 1, 1, tzinfo=zone)
            precision, confidence = "year", "derived"
        elif day:
            start = datetime(*(int(x) for x in day.groups()), tzinfo=zone)
            precision, confidence = "day", "explicit"
        elif month:
            y, m = (int(x) for x in month.groups())
            start = datetime(y, m, 1, tzinfo=zone)
            end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=zone)
            precision, confidence = "month", "explicit"
        elif match := re.fullmatch(r"(去年|前年|今年)(?:(\d{1,2})月)?", expression):
            y = local.year - {"去年": 1, "前年": 2, "今年": 0}[match[1]]
            if match[2]:
                m = int(match[2])
                start = datetime(y, m, 1, tzinfo=zone)
                end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=zone)
                precision, confidence = "month", "derived"
            else:
                start, end = datetime(y, 1, 1, tzinfo=zone), datetime(y + 1, 1, 1, tzinfo=zone)
                precision, confidence = "year", "derived"
        elif expression in {"上个月", "这个月", "本月", "下个月"}:
            month_index = (
                local.year * 12 + local.month - 1 + {"上个月": -1, "下个月": 1}.get(expression, 0)
            )
            y, m = month_index // 12, month_index % 12 + 1
            start = datetime(y, m, 1, tzinfo=zone)
            end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=zone)
            precision, confidence = "month", "derived"
        elif year:
            y = int(year[1])
            start, end = datetime(y, 1, 1, tzinfo=zone), datetime(y + 1, 1, 1, tzinfo=zone)
            precision, confidence = "year", "explicit" if year else "derived"
        elif expression in {"今天", "昨天", "前天", "明天", "后天"}:
            shift = {"今天": 0, "昨天": -1, "前天": -2, "明天": 1, "后天": 2}[expression]
            date = local.date() + timedelta(days=shift)
            start, precision, confidence = (
                datetime.combine(date, datetime.min.time(), zone),
                "day",
                "derived",
            )
        elif expression == "现在":
            start, precision, confidence = anchor, "instant", "derived"
        elif match := re.fullmatch(r"(上周|下周|本周)([一二三四五六日天])", expression):
            weekday = "一二三四五六日".index(match[2].replace("天", "日"))
            date = local.date() + timedelta(
                days=weekday - local.weekday() + {"下周": 7, "上周": -7}.get(match[1], 0)
            )
            start, precision, confidence = (
                datetime.combine(date, datetime.min.time(), zone),
                "day",
                "derived",
            )
        elif expression in {"上周", "下周", "本周"}:
            date = local.date() + timedelta(
                days=-local.weekday() + {"下周": 7, "上周": -7}.get(expression, 0)
            )
            start = datetime.combine(date, datetime.min.time(), zone)
            end, precision, confidence = start + timedelta(days=7), "range", "derived"
        if precision == "day":
            end = start + timedelta(days=1)
    except ValueError:
        return (
            TimeWindow(precision="unknown", original_expression=expression),
            "ambiguous",
            "请确认有效日期。",
        )
    return (
        TimeWindow(
            start=start.astimezone(UTC) if start else None,
            end=end.astimezone(UTC) if end else None,
            precision=precision,
            original_expression=expression,
        ),
        confidence,
        None,
    )


def temporal(request: TemporalInput):
    candidates, resolutions = [], []
    if request.anchor_event.scope != request.scope:
        raise MemoryFault("scope_mismatch", "Temporal anchor crosses scope")
    for candidate in request.candidates:
        expression = candidate.statement.valid_time.original_expression
        if rule_for(candidate).cardinality == "event":
            from .time_expressions import event_expression

            # Prefer the user's concrete clock over a model's coarse date label.
            expressions = {
                value
                for evidence in candidate.sources
                if (value := event_expression(evidence.quote))
            }
            if len(expressions) == 1:
                expression = expressions.pop()
        grounded = not expression or (
            expression in request.anchor_event.content
            and any(expression in evidence.quote for evidence in candidate.sources)
        )
        window, confidence, clarification = resolve_time(
            expression if grounded else None,
            request.anchor_event.occurred_at,
            request.user_timezone,
        )
        if not grounded:
            confidence, clarification = "ambiguous", "请确认原话中未出现的时间。"
        # A direct self-introduction is a current state at the reporting time.
        # This gives explicit name updates temporal order instead of permanent conflicts.
        if (
            not expression
            and candidate.statement.predicate == "name"
            and candidate.statement.subject_id == request.scope.user_id
            and candidate.statement.polarity == "positive"
        ):
            window = TimeWindow(start=request.anchor_event.occurred_at, precision="instant")
        event_window = window
        if (
            window.start
            and (
                rule_for(candidate).cardinality == "single"
                or (rule_for(candidate).cardinality == "set" and expression == "现在")
            )
            and re.search(r"搬|从|开始|起|现在", request.anchor_event.content)
        ):
            window = window.model_copy(update={"end": None})
        candidates.append(
            candidate.model_copy(
                update={"statement": candidate.statement.model_copy(update={"valid_time": window})}
            )
        )
        resolutions.append(
            TemporalResolution(
                candidate_id=candidate.candidate_id,
                valid_time=window,
                event_time=event_window,
                confidence=confidence,
                clarification=clarification,
            )
        )
    return TemporalOutput(candidates=candidates, resolutions=resolutions)
