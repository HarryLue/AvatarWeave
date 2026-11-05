"""M13/M14/M15 budgeted context, stable persona and evidence-bounded follow-up."""

from datetime import timedelta
from uuid import uuid4

from .contracts import (
    ContextBlock,
    ContextOutput,
    ManifestEntry,
    OpenLoopOutput,
    PersonaOutput,
    ToneState,
)
from .execution import MemoryFault


def token_bound(text):
    # A safe upper bound for byte-level model tokenizers; no network tokenizer
    # download or unbounded parser runs on the realtime loop.
    return len(text.encode("utf-8"))


def build_context(request, *, loops=()):
    budget = request.budget
    current_cost = token_bound(request.latest_event.content)
    system = (
        request.persona.authored_identity + "\n" + "\n".join(request.persona.behavior_boundaries)
    )
    system_cost = token_bound(system)
    if current_cost + system_cost > budget.max_input_tokens:
        raise MemoryFault(
            "budget_exceeded", "Current input and required persona exceed the model budget"
        )
    blocks = [
        ContextBlock(
            kind="system",
            text=system,
            source_refs=[],
            token_count=system_cost,
            trusted_instruction=True,
        )
    ]
    used, memory_used, manifest = system_cost + current_cost, 0, []
    omitted = []
    guidance = request.tone.text_guidance
    if (
        request.tone.expires_at > request.meta.now
        and used + token_bound(guidance) <= budget.max_input_tokens
    ):
        blocks.append(
            ContextBlock(
                kind="tone",
                text=guidance,
                source_refs=[],
                token_count=token_bound(guidance),
                trusted_instruction=True,
            )
        )
        used += token_bound(guidance)
    seen = set()
    for hit in request.retrieved:
        record = hit.record
        ref_key = (record.ref.object_id, record.ref.version)
        window = record.statement.valid_time
        text = (
            f"[{record.assertion_kind}; polarity={record.statement.polarity}; "
            f"subject={'user_self' if record.statement.subject_id == request.scope.user_id else record.statement.subject_id}; "
            f"predicate={record.statement.predicate}; "
            f"valid={window.start.isoformat() if window.start else 'unknown'}/"
            f"{window.end.isoformat() if window.end else 'open'}; evidence={hit.evidence_level}; use={hit.permitted_use}]\n"
            + record.display_text
        )
        cost = token_bound(text)
        allowed = (
            record.scope == request.scope
            and record.status == "active"
            and record.system_to is None
            and record.namespace != "character_lore"
            and record.sensitivity != "restricted"
        )
        reason = "Evidence-backed memory"
        if not allowed or ref_key in seen:
            decision, reason = "excluded", "Invalid scope, state, sensitivity or duplicate"
        elif (
            len(seen) >= budget.max_selected
            or memory_used + cost > budget.max_memory_tokens
            or used + cost > budget.max_input_tokens
        ):
            decision, reason = "excluded", "Memory/context token budget exhausted"
            if reason not in omitted:
                omitted.append(reason)
        else:
            seen.add(ref_key)
            decision = hit.permitted_use if hit.permitted_use != "normal" else "included"
            # Untrusted memory is a data block; it never becomes persona instructions.
            blocks.append(
                ContextBlock(
                    kind="memory",
                    text=text,
                    source_refs=[record.ref],
                    token_count=cost,
                    trusted_instruction=False,
                )
            )
            memory_used, used = memory_used + cost, used + cost
        manifest.append(
            ManifestEntry(
                ref=record.ref,
                decision=decision,
                reason=reason,
                used_tokens=cost if decision != "excluded" else 0,
            )
        )
    for loop in loops:
        text = (
            "可选跟进事项（只询问结果，不推断完成；先回应本轮问题，用户不愿继续则跳过）："
            + loop.title
            + "；约定时间："
            + (loop.event_time.end or loop.event_time.start).isoformat()
        )
        cost = token_bound(text)
        included = (
            used + cost <= budget.max_input_tokens
            and memory_used + cost <= budget.max_memory_tokens
            and len(blocks) < 30
        )
        manifest.append(
            ManifestEntry(
                ref=loop.ref,
                decision="clarify_only" if included else "excluded",
                reason="Explicitly permitted pending outcome"
                if included
                else "Follow-up context budget exhausted",
                used_tokens=cost if included else 0,
            )
        )
        if included:
            blocks.append(
                ContextBlock(
                    kind="open_loops",
                    text=text,
                    source_refs=[loop.ref],
                    token_count=cost,
                    trusted_instruction=False,
                )
            )
            used, memory_used = used + cost, memory_used + cost
    # Add only complete groups, newest first. Interruption/unknown output is
    # explicitly labeled, never converted into evidence of heard speech.
    groups, group_order = {}, []
    for message in request.recent_messages:
        group = message.tool_group_id or message.message_id
        if group not in groups:
            groups[group] = []
            group_order.append(group)
        groups[group].append(message)
    recent = []
    for key in reversed(group_order):
        group = groups[key]
        if any(
            m.message_id == request.latest_event.event_id
            or m.source_event_id == request.latest_event.event_id
            for m in group
        ):
            continue
        text = "\n".join(f"{m.role} [{m.output_evidence}]: {m.text}" for m in group)
        cost = token_bound(text)
        if used + cost <= budget.max_input_tokens and len(blocks) + len(recent) < 31:
            recent.append(
                ContextBlock(
                    kind="recent",
                    text=text,
                    source_refs=[],
                    token_count=cost,
                    trusted_instruction=False,
                )
            )
            used += cost
    blocks.extend(reversed(recent))
    blocks.append(
        ContextBlock(
            kind="current_input",
            text=request.latest_event.content,
            source_refs=[],
            token_count=current_cost,
            trusted_instruction=False,
        )
    )
    if len(blocks) > 32:
        raise MemoryFault("budget_exceeded", "Context block count exceeds the contract")
    return ContextOutput(
        generation_id=request.generation_id,
        blocks=blocks,
        manifest=manifest,
        manifest_id=uuid4().hex,
        token_count=used,
        memory_revision=request.fence.memory_revision,
        privacy_epoch=request.fence.privacy_epoch,
        omitted_reasons=omitted,
        needs_clarification=bool(omitted),
    )


class ContextMixin:
    async def context(self, request):
        async def operation():
            from .contracts import ModuleResult

            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                # The revision is an observation watermark. An unrelated new
                # fact must not cancel this turn; validate the actual read set.
                for hit in request.retrieved:
                    from sqlalchemy import select

                    from .facts import fact_scope
                    from .tables import FactVersion

                    row = await db.scalar(
                        select(FactVersion).where(
                            *fact_scope(request.scope),
                            FactVersion.memory_id == hit.record.ref.object_id,
                            FactVersion.version == hit.record.ref.version,
                            FactVersion.system_to.is_(None),
                            FactVersion.status == "active",
                        )
                    )
                    if row is None:
                        raise MemoryFault("version_conflict", "Context memory was invalidated")
                loops = []
                if request.active_loop_refs:
                    from .contracts import PrivacyPreference

                    prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                    if not prefs.long_term_memory_enabled or not prefs.allow_proactive_followup:
                        raise MemoryFault("privacy_changed", "Follow-up permission was withdrawn")
                    if len(request.active_loop_refs) > 1:
                        raise MemoryFault(
                            "budget_exceeded", "At most one follow-up per ordinary turn"
                        )
                    for ref in request.active_loop_refs:
                        loop = await self._eligible_loop(db, request.scope, ref, request.meta.now)
                        if loop is None:
                            raise MemoryFault("version_conflict", "Follow-up is no longer eligible")
                        loops.append(loop)
                return ModuleResult(
                    status="ok",
                    data=build_context(request, loops=loops),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def persona(self, request, card, permitted_actions):
        async def operation():
            from .contracts import ModuleResult

            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                if (
                    card.persona_id != request.persona_id
                    or card.version != request.requested_version
                ):
                    raise MemoryFault("not_found", "Approved persona version not found")
                text = request.latest_event.content
                label, guidance = "neutral", "用简短自然的语气回应，不捏造共同经历或用户已经听懂。"
                if any(word in text for word in ("给我建议", "直接说", "别安慰")):
                    label, guidance = (
                        "focused",
                        "直接回应用户问题，先给可行建议，遵守明确互动边界。",
                    )
                elif any(word in text for word in ("难过", "伤心", "难受", "失去")):
                    label, guidance = (
                        "concerned",
                        "温和倾听，允许用户决定是否继续，不作诊断，不主动提起私密旧事。",
                    )
                elif request.current_tone and request.current_tone.expires_at > request.meta.now:
                    label, guidance = request.current_tone.label, request.current_tone.text_guidance
                tone = ToneState(
                    label=label,
                    confidence=0.8,
                    expires_at=request.meta.now + timedelta(minutes=5),
                    text_guidance=guidance,
                    evidence_event_ids=[request.latest_event.event_id],
                )
                return ModuleResult(
                    status="ok",
                    data=PersonaOutput(
                        persona=card,
                        tone=tone,
                        permitted_actions=permitted_actions,
                        excluded_memory_refs=[],
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)

    async def open_loops(self, request):
        async def operation():
            from .contracts import ModuleResult

            async with self.database.sessions() as db:
                account = await self._account(db, request.scope)
                self._privacy(account, request)
                updated, candidates = [], []
                from .contracts import PrivacyPreference
                from .tables import DerivedView

                prefs = PrivacyPreference.model_validate_json(account.preferences_json)
                for loop in request.current:
                    row = await db.get(DerivedView, (loop.ref.object_id, loop.ref.version))
                    if (
                        row is None
                        or row.account_id != account.id
                        or row.companion_id != request.scope.companion_id
                        or row.kind != "open_loop"
                        or row.state != "valid"
                        or row.document_json != loop.model_dump_json()
                    ):
                        raise MemoryFault("version_conflict", "Open loop snapshot changed")
                    for pointer in loop.sources:
                        await self._source(
                            db, request.scope, pointer.event_id, pointer.source_revision
                        )
                    due = loop.event_time.end or loop.event_time.start
                    state = (
                        "pending_outcome"
                        if loop.status == "planned" and due and due < request.meta.now
                        else loop.status
                    )
                    value = loop.model_copy(update={"status": state})
                    updated.append(value)
                    if (
                        prefs.allow_proactive_followup
                        and value.user_allows_followup
                        and state == "pending_outcome"
                        and (
                            value.last_followup_at is None
                            or request.meta.now - value.last_followup_at
                            >= timedelta(hours=request.followup_cooldown_hours)
                        )
                    ):
                        candidates.append(value.ref)
                return ModuleResult(
                    status="ok",
                    data=OpenLoopOutput(
                        updates=updated,
                        followup_candidates=candidates[: request.max_followups],
                        clarification=None,
                    ),
                    visibility=self._visibility(account, None, [], []),
                )

        return await self.executor.run(request, operation)
