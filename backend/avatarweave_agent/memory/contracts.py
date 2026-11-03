"""Memory v0.3 contracts, transcribed from the approved design attachments.

Schema availability does not imply that the corresponding module is implemented.
"""

# Source: docs/voice-memory-design/contracts/00-common.md
from __future__ import annotations

from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
ShortText = Annotated[str, Field(min_length=1, max_length=2048)]
BodyText = Annotated[str, Field(min_length=1, max_length=16000)]
UnitScore = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class Scope(Contract):
    workspace_id: Identifier
    user_id: Identifier
    companion_id: Identifier


class VersionFence(Contract):
    privacy_epoch: NonNegativeInt
    memory_revision: NonNegativeInt
    owner_epoch: NonNegativeInt


class VisibilityRequirement(Contract):
    required_level: Literal["source", "session_overlay", "structured", "indexed"]
    minimum_source_watermark: NonNegativeInt
    target_event_ids: Annotated[list[Identifier], Field(max_length=32)]
    allow_provisional: bool


class ExecutionControl(Contract):
    lane: Literal["control", "dialogue", "deep_recall", "maintenance"]
    budget_profile_id: Identifier
    cancellation_token_id: Identifier
    budget_ms: Annotated[int, Field(ge=1, le=600000)]
    max_queue_wait_ms: Annotated[int, Field(ge=0, le=60000)]
    max_parallel_branches: Annotated[int, Field(ge=1, le=16)] = 4
    allow_optional_model_calls: bool = False
    visibility: VisibilityRequirement | None = None

    @model_validator(mode="after")
    def coherent_execution(self):
        if self.max_queue_wait_ms > self.budget_ms:
            raise ValueError("queue wait exceeds total budget")
        if self.lane == "dialogue" and self.allow_optional_model_calls:
            raise ValueError("normal dialogue forbids optional model calls")
        return self


class TargetEventState(Contract):
    event_id: Identifier
    revision: PositiveInt
    state: Literal[
        "persisted",
        "provisional",
        "structured",
        "indexed",
        "rejected",
        "needs_confirmation",
        "failed",
    ]
    updated_at: AwareDatetime


class MemoryVisibility(Contract):
    source_watermark: NonNegativeInt
    structured_settled_watermark: NonNegativeInt
    index_settled_watermark: NonNegativeInt
    overlay_session_id: Identifier | None
    overlay_event_ids: Annotated[list[Identifier], Field(max_length=32)]
    target_states: Annotated[list[TargetEventState], Field(max_length=32)]
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def watermarks_are_prefixes(self):
        if not (
            self.index_settled_watermark
            <= self.structured_settled_watermark
            <= self.source_watermark
        ):
            raise ValueError("invalid processed-prefix watermarks")
        if self.overlay_event_ids and self.overlay_session_id is None:
            raise ValueError("overlay requires a session")
        return self


class RequestMeta(Contract):
    operation_id: Identifier
    trace_id: Identifier
    now: AwareDatetime
    deadline_at: AwareDatetime
    algorithm_version: Identifier
    execution: ExecutionControl
    schema_version: Literal[2] = 2

    @model_validator(mode="after")
    def ordered_deadline(self):
        if self.deadline_at <= self.now:
            raise ValueError("deadline_at must be after now")
        interval_ms = (self.deadline_at - self.now).total_seconds() * 1000
        if interval_ms > self.execution.budget_ms + 0.001:
            raise ValueError("audit deadline exceeds execution budget")
        return self


class ScopedRequest(Contract):
    meta: RequestMeta
    scope: Scope
    fence: VersionFence


class ErrorInfo(Contract):
    code: Literal[
        "unauthorized",
        "scope_mismatch",
        "version_conflict",
        "deadline_exceeded",
        "provider_unavailable",
        "invalid_model_output",
        "stale_generation",
        "privacy_changed",
        "evidence_missing",
        "budget_exceeded",
        "storage_unavailable",
        "unsupported_capability",
        "invalid_transition",
        "not_found",
        "manual_review_required",
        "overloaded",
        "quota_exceeded",
        "cancelled",
        "memory_pending",
    ]
    retryable: bool
    message: Annotated[str, Field(min_length=1, max_length=512)]
    retry_after_ms: NonNegativeInt | None = None


class StageTiming(Contract):
    stage: Identifier
    elapsed_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    queue_wait_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    outcome: Literal["completed", "partial", "timed_out", "cancelled", "failed"]


class ExecutionReport(Contract):
    operation_id: Identifier
    lane: Literal["control", "dialogue", "deep_recall", "maintenance"]
    budget_profile_id: Identifier
    elapsed_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    queue_wait_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    timings: Annotated[list[StageTiming], Field(max_length=64)]
    cancel_reason: Literal[
        "none",
        "user_interrupt",
        "input_revision",
        "deadline",
        "owner_changed",
        "privacy_changed",
        "overload",
    ]
    cleanup: Literal["running", "released", "detached_bounded"]
    remaining_work_handles: Annotated[list[Identifier], Field(max_length=16)]
    degraded_routes: Annotated[list[Identifier], Field(max_length=16)]

    @model_validator(mode="after")
    def cleanup_matches_work(self):
        if self.cleanup == "detached_bounded" and not self.remaining_work_handles:
            raise ValueError("detached work needs a bounded tracked handle")
        if self.cleanup == "released" and self.remaining_work_handles:
            raise ValueError("released work cannot retain active handles")
        return self


class MetricCount(Contract):
    metric: Literal[
        "write_precision",
        "important_write_recall",
        "write_recall",
        "required_evidence_coverage",
        "temporal_update_accuracy",
        "grounded_answer_accuracy",
        "unsupported_assertion_rate",
        "correct_abstention",
        "over_abstention",
        "mandatory_compression_coverage",
        "summary_precision",
        "cold_recovery_accuracy",
    ]
    numerator: NonNegativeInt
    denominator: NonNegativeInt

    @model_validator(mode="after")
    def valid_counts(self):
        if self.numerator > self.denominator:
            raise ValueError("numerator exceeds denominator")
        return self


T = TypeVar("T")


class ModuleResult(Contract, Generic[T]):
    status: Literal["ok", "partial", "error"]
    data: T | None = None
    error: ErrorInfo | None = None
    execution: ExecutionReport | None = None
    visibility: MemoryVisibility | None = None
    warnings: Annotated[list[ShortText], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def result_shape(self):
        if self.status == "ok" and (self.data is None or self.error is not None):
            raise ValueError("ok requires data and no error")
        if self.status == "error" and (self.error is None or self.data is not None):
            raise ValueError("error requires error and no data")
        if self.status == "partial" and (self.data is None or self.error is None):
            raise ValueError("partial requires data and error")
        return self


class VersionRef(Contract):
    object_id: Identifier
    version: PositiveInt


class TimeWindow(Contract):
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    precision: Literal["instant", "day", "month", "year", "range", "unknown"]
    original_expression: ShortText | None = None

    @model_validator(mode="after")
    def ordered_range(self):
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("time range reversed")
        return self


class EvidenceRef(Contract):
    event_id: Identifier
    source_revision: PositiveInt
    start_char: NonNegativeInt
    end_char: PositiveInt
    quote: ShortText
    origin: Literal[
        "user_text",
        "user_transcript",
        "user_ui",
        "tool_result",
        "output_observation",
        "authored_character",
    ]

    @model_validator(mode="after")
    def valid_span(self):
        if self.end_char <= self.start_char:
            raise ValueError("empty or reversed span")
        if len(self.quote) != self.end_char - self.start_char:
            raise ValueError("span length must match quote")
        return self


class Qualifier(Contract):
    key: Identifier
    value: ShortText


class EvidencePointer(Contract):
    event_id: Identifier
    source_revision: PositiveInt
    level: Literal["raw", "evidence_capsule", "metadata_only"]
    capsule_id: Identifier | None = None
    start_char: NonNegativeInt | None = None
    end_char: PositiveInt | None = None

    @model_validator(mode="after")
    def pointer_shape(self):
        if self.level == "evidence_capsule" and self.capsule_id is None:
            raise ValueError("capsule evidence requires capsule_id")
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError("span endpoints must appear together")
        if self.start_char is not None and self.end_char <= self.start_char:
            raise ValueError("invalid evidence pointer span")
        return self


class Statement(Contract):
    subject_id: Identifier
    predicate: Identifier
    value_type: Literal["text", "entity", "number", "date", "boolean"]
    canonical_value: ShortText
    qualifiers: Annotated[list[Qualifier], Field(max_length=16)] = Field(default_factory=list)
    polarity: Literal["positive", "negative"]
    valid_time: TimeWindow


class MemoryRecord(Contract):
    ref: VersionRef
    scope: Scope
    namespace: Literal["life", "relationship", "character_lore"]
    content_kind: Literal[
        "fact",
        "preference",
        "episode",
        "relationship",
        "future_intent",
        "interaction_preference",
        "reflection",
    ]
    assertion_kind: Literal[
        "user_report", "user_confirmed", "tool_observed", "model_hypothesis", "authored_fiction"
    ]
    statement: Statement
    display_text: BodyText
    sources: Annotated[list[EvidencePointer], Field(min_length=1, max_length=64)]
    source_dependencies: Annotated[list[VersionRef], Field(max_length=256)] = Field(
        default_factory=list
    )
    recorded_at: AwareDatetime
    system_from: AwareDatetime
    system_to: AwareDatetime | None = None
    status: Literal["candidate", "active", "uncertain", "superseded", "expired", "deleted"]
    tier: Literal["hot", "warm", "cold"]
    sensitivity: Literal["normal", "private", "restricted"]
    importance: UnitScore
    user_pinned: bool = False
    mentioned_at: AwareDatetime
    last_user_confirmed_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def memory_invariants(self):
        if self.system_to is not None and self.system_to < self.system_from:
            raise ValueError("system interval reversed")
        if self.content_kind == "reflection" and self.assertion_kind != "model_hypothesis":
            raise ValueError("reflection must remain hypothesis")
        if self.namespace == "character_lore":
            if self.assertion_kind != "authored_fiction":
                raise ValueError("character lore must be labeled fiction")
        elif self.assertion_kind == "authored_fiction":
            raise ValueError("fiction cannot be a life assertion")
        return self


class SourceEvent(Contract):
    event_id: Identifier
    revision: PositiveInt
    session_id: Identifier
    turn_id: Identifier
    scope: Scope
    origin: Literal[
        "user_text",
        "user_transcript",
        "user_ui",
        "tool_result",
        "output_observation",
        "authored_character",
    ]
    content: BodyText
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    is_final: bool
    supersedes: VersionRef | None = None
    fictional: bool = False


class EvidenceCapsule(Contract):
    capsule_id: Identifier
    scope: Scope
    source_session_id: Identifier
    source_event_id: Identifier
    source_revision: PositiveInt
    evidence: EvidenceRef
    captured_at: AwareDatetime
    retention_authorization_ref: Identifier
    original_source_available: bool
    integrity_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    privacy_epoch: NonNegativeInt


class DerivedDependency(Contract):
    parent_ref: VersionRef
    child_ref: VersionRef
    relation: Literal["summary_of", "inferred_from", "profile_of", "indexed_from"]


class SourceDescriptor(Contract):
    event_id: Identifier
    revision: PositiveInt
    scope: Scope
    session_id: Identifier
    origin: Literal[
        "user_text",
        "user_transcript",
        "user_ui",
        "tool_result",
        "output_observation",
        "authored_character",
    ]
    occurred_at: AwareDatetime
    raw_available: bool
    capsule_ids: Annotated[list[Identifier], Field(max_length=128)]
    revoked: bool


class PredicateRule(Contract):
    predicate: Identifier
    cardinality: Literal["single", "set", "event"]
    value_type: Literal["text", "entity", "number", "date", "boolean"]
    mutable: bool
    compare_qualifier_keys: Annotated[list[Identifier], Field(max_length=16)]
    ttl_days: Annotated[int, Field(ge=1)] | None = None
    default_half_life_days: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None


class Budget(Contract):
    max_input_tokens: Annotated[int, Field(ge=256, le=65536)] = 6000
    max_memory_tokens: Annotated[int, Field(ge=0, le=16384)] = 1200
    max_candidates: Annotated[int, Field(ge=1, le=1000)] = 60
    max_selected: Annotated[int, Field(ge=1, le=32)] = 8
    max_queries: Annotated[int, Field(ge=1, le=5)] = 3
    max_online_ms: Annotated[int, Field(ge=50, le=5000)] = 300

    @model_validator(mode="after")
    def coherent_budget(self):
        if self.max_memory_tokens > self.max_input_tokens:
            raise ValueError("memory budget exceeds input budget")
        if self.max_selected > self.max_candidates:
            raise ValueError("selected count exceeds candidate count")
        return self


# Source: docs/voice-memory-design/contracts/01-lifecycle.md
class CandidateMemory(Contract):
    candidate_id: Identifier
    namespace: Literal["life", "relationship", "character_lore"]
    content_kind: Literal[
        "fact",
        "preference",
        "episode",
        "relationship",
        "future_intent",
        "interaction_preference",
        "reflection",
    ]
    assertion_kind: Literal[
        "user_report", "user_confirmed", "tool_observed", "model_hypothesis", "authored_fiction"
    ]
    statement: Statement
    display_text: BodyText
    sources: Annotated[list[EvidenceRef], Field(min_length=1, max_length=32)]
    interpretation: Literal[
        "asserted", "hypothetical", "quoted_other", "negated", "joke_or_unclear", "fictional"
    ]
    sensitivity: Literal["normal", "private", "restricted"]
    proposed_importance: UnitScore


class AdmissionInput(ScopedRequest):
    events: Annotated[list[SourceEvent], Field(min_length=1, max_length=32)]
    memory_enabled: bool
    extraction_prompt_version: Identifier
    max_candidates: Annotated[int, Field(ge=1, le=64)] = 24


class AdmissionDecision(Contract):
    candidate_id: Identifier
    decision: Literal["accept", "defer", "reject"]
    reason: Literal[
        "supported",
        "transient",
        "hypothetical",
        "fiction",
        "ambiguous_speaker",
        "unreliable_transcript",
        "insufficient_evidence",
        "privacy_disabled",
        "out_of_scope",
    ]


class AdmissionOutput(Contract):
    candidates: Annotated[list[CandidateMemory], Field(max_length=64)]
    decisions: Annotated[list[AdmissionDecision], Field(max_length=64)]
    input_watermark: NonNegativeInt


class EntityCandidate(Contract):
    entity_id: Identifier
    entity_type: Literal["person", "pet", "place", "organization", "object", "event"]
    canonical_name: ShortText
    aliases: Annotated[list[ShortText], Field(max_length=32)]
    context_refs: Annotated[list[VersionRef], Field(max_length=32)]


class EntityInput(ScopedRequest):
    candidates: Annotated[list[CandidateMemory], Field(min_length=1, max_length=64)]
    known_entities: Annotated[list[EntityCandidate], Field(max_length=128)]


class EntityBinding(Contract):
    candidate_id: Identifier
    mention: ShortText
    resolved_entity_id: Identifier | None
    status: Literal["existing", "new_candidate", "ambiguous"]
    evidence_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]


class EntityOutput(Contract):
    candidates: Annotated[list[CandidateMemory], Field(max_length=64)]
    bindings: Annotated[list[EntityBinding], Field(max_length=256)]
    new_entities: Annotated[list[EntityCandidate], Field(max_length=64)]
    clarification: ShortText | None = None


class TemporalInput(ScopedRequest):
    candidates: Annotated[list[CandidateMemory], Field(min_length=1, max_length=64)]
    user_timezone: Identifier
    anchor_event: SourceEvent


class TemporalResolution(Contract):
    candidate_id: Identifier
    valid_time: TimeWindow
    event_time: TimeWindow
    confidence: Literal["explicit", "derived", "ambiguous", "unknown"]
    clarification: ShortText | None = None


class TemporalOutput(Contract):
    candidates: Annotated[list[CandidateMemory], Field(max_length=64)]
    resolutions: Annotated[list[TemporalResolution], Field(max_length=64)]


class ConflictInput(ScopedRequest):
    candidate: CandidateMemory
    predicate_rule: PredicateRule
    comparable_records: Annotated[list[MemoryRecord], Field(max_length=128)]


class ConflictPair(Contract):
    candidate_id: Identifier
    existing_ref: VersionRef
    relation: Literal[
        "duplicate",
        "support",
        "temporal_change",
        "conditional_coexist",
        "contradiction",
        "independent",
        "uncertain",
    ]
    reason: ShortText
    overlapping_time: TimeWindow | None = None


class MutationProposal(Contract):
    action: Literal["create", "add_evidence", "replace_interval", "retract", "coexist", "defer"]
    candidate_id: Identifier
    target_refs: Annotated[list[VersionRef], Field(max_length=128)]
    new_statement: Statement | None = None
    reason: ShortText
    requires_user_confirmation: bool


class ConflictOutput(Contract):
    comparisons: Annotated[list[ConflictPair], Field(max_length=128)]
    proposals: Annotated[list[MutationProposal], Field(min_length=1, max_length=128)]
    clarification: ShortText | None = None


class CommitInput(ScopedRequest):
    candidate: CandidateMemory
    proposals: Annotated[list[MutationProposal], Field(min_length=1, max_length=128)]
    expected_refs: Annotated[list[VersionRef], Field(max_length=128)]
    user_confirmation_event: Identifier | None = None


class CommitOutput(Contract):
    committed: Annotated[list[VersionRef], Field(max_length=128)]
    superseded: Annotated[list[VersionRef], Field(max_length=128)]
    memory_revision: NonNegativeInt
    outbox_ids: Annotated[list[Identifier], Field(max_length=128)]
    session_overlay_revision: NonNegativeInt
    visibility: MemoryVisibility


class TierPolicy(Contract):
    hot_days: Annotated[int, Field(ge=1, le=365)] = 30
    warm_days: Annotated[int, Field(ge=1, le=3653)] = 365
    hot_soft_limit: Annotated[int, Field(ge=1)] = 2000
    online_embedding_soft_limit: Annotated[int, Field(ge=1)] = 20000
    max_moves_per_job: Annotated[int, Field(ge=1, le=10000)] = 500
    max_promotions_per_day: Annotated[int, Field(ge=0, le=10000)] = 200


class TieringInput(ScopedRequest):
    records: Annotated[list[MemoryRecord], Field(max_length=1000)]
    policy: TierPolicy
    confirmed_recall_refs: Annotated[list[VersionRef], Field(max_length=1000)]


class TierMove(Contract):
    ref: VersionRef
    from_tier: Literal["hot", "warm", "cold"]
    to_tier: Literal["hot", "warm", "cold"]
    reason: Literal["age", "capacity", "user_pin", "verified_recall", "open_loop"]
    requires_verified_archive: bool


class TieringOutput(Contract):
    moves: Annotated[list[TierMove], Field(max_length=1000)]
    retained_refs: Annotated[list[VersionRef], Field(max_length=1000)]
    next_cursor: Identifier | None = None


class CompressionPolicy(Contract):
    target_tokens: Annotated[int, Field(ge=128, le=8192)] = 800
    min_input_tokens: Annotated[int, Field(ge=128)] = 1600
    max_leaf_records: Annotated[int, Field(ge=1, le=512)] = 128
    minimum_mandatory_coverage: UnitScore = 1.0
    max_retries: Annotated[int, Field(ge=0, le=3)] = 1
    verifier_version: Identifier


class SummaryClaim(Contract):
    text: ShortText
    supported_by: Annotated[list[VersionRef], Field(min_length=1, max_length=64)]
    evidence_refs: Annotated[list[EvidencePointer], Field(min_length=1, max_length=64)]
    preserves_negation: bool
    valid_time: TimeWindow


class SummaryNode(Contract):
    ref: VersionRef
    scope: Scope
    level: Literal["session", "month", "year", "life_chapter"]
    period: TimeWindow
    claims: Annotated[list[SummaryClaim], Field(min_length=1, max_length=256)]
    child_refs: Annotated[list[VersionRef], Field(max_length=256)]
    leaf_manifest_ref: Identifier
    input_watermark: NonNegativeInt
    prompt_version: Identifier
    token_count: PositiveInt
    state: Literal["valid", "invalidated", "pending_review"]


class CompressionInput(ScopedRequest):
    source_records: Annotated[list[MemoryRecord], Field(min_length=1, max_length=512)]
    evidence_capsules: Annotated[list[EvidenceCapsule], Field(max_length=512)] = Field(
        default_factory=list
    )
    source_events: Annotated[list[SourceEvent], Field(max_length=128)] = Field(default_factory=list)
    previous_summaries: Annotated[list[SummaryNode], Field(max_length=32)]
    level: Literal["session", "month", "year", "life_chapter"]
    period: TimeWindow
    mandatory_refs: Annotated[list[VersionRef], Field(max_length=512)]
    policy: CompressionPolicy


class CompressionOutput(Contract):
    summary: SummaryNode | None
    accepted: bool
    mandatory_coverage: UnitScore
    unsupported_claims: Annotated[list[ShortText], Field(max_length=256)]
    preserved_refs: Annotated[list[VersionRef], Field(max_length=512)]
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    rejection_reason: ShortText | None = None


class ReflectionInput(ScopedRequest):
    records: Annotated[list[MemoryRecord], Field(min_length=1, max_length=128)]
    minimum_distinct_sessions: Annotated[int, Field(ge=2, le=20)] = 3
    max_hypotheses: Annotated[int, Field(ge=1, le=16)] = 3


class Hypothesis(Contract):
    text: ShortText
    supports: Annotated[list[VersionRef], Field(min_length=2, max_length=64)]
    counters: Annotated[list[VersionRef], Field(max_length=64)]
    scope_conditions: Annotated[list[Qualifier], Field(max_length=16)]
    review_after: AwareDatetime
    requires_confirmation: bool
    status: Literal["candidate", "rejected", "needs_confirmation"]


class ReflectionOutput(Contract):
    hypotheses: Annotated[list[Hypothesis], Field(max_length=16)]
    rejected_reasons: Annotated[list[ShortText], Field(max_length=16)]


class DecayPolicy(Contract):
    half_life_days: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    floor: UnitScore
    reinforcement_cap: Annotated[int, Field(ge=0, le=100)] = 5
    reinforcement_gain: Annotated[float, Field(ge=0, le=1)] = 0.25


class DecayInput(ScopedRequest):
    records: Annotated[list[MemoryRecord], Field(max_length=1000)]
    policy: DecayPolicy
    distinct_user_confirmation_counts: Annotated[
        list[tuple[VersionRef, NonNegativeInt]], Field(max_length=1000)
    ]


class DecayScore(Contract):
    ref: VersionRef
    age_days: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    effective_half_life_days: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    freshness: UnitScore
    eligible: bool
    invalid_reason: ShortText | None = None


class DecayOutput(Contract):
    scores: Annotated[list[DecayScore], Field(max_length=1000)]


class ForgetPolicy(Contract):
    raw_transcript_days: Annotated[int, Field(ge=1, le=3653)] = 90
    rejected_candidate_days: Annotated[int, Field(ge=1, le=365)] = 7
    inactive_hypothesis_days: Annotated[int, Field(ge=1, le=365)] = 30
    permit_life_memory_physical_delete: Literal[False] = False


class ForgetInput(ScopedRequest):
    records: Annotated[list[MemoryRecord], Field(max_length=1000)]
    policy: ForgetPolicy
    explicit_delete_refs: Annotated[list[VersionRef], Field(max_length=1000)]
    expired_source_ids: Annotated[list[Identifier], Field(max_length=1000)]


class ForgetAction(Contract):
    ref: VersionRef
    action: Literal[
        "retain",
        "lower_priority",
        "archive",
        "expire",
        "invalidate_derivative",
        "delete_by_user",
        "delete_by_retention",
    ]
    reason: ShortText
    legal_or_user_basis_ref: Identifier | None = None
    source_granularity_after: Literal["raw", "evidence_capsule", "summary_only", "none"]


class ForgetOutput(Contract):
    actions: Annotated[list[ForgetAction], Field(max_length=1000)]
    requires_deletion_job: bool
    next_cursor: Identifier | None = None


# Source: docs/voice-memory-design/contracts/02-recall-runtime.md
class QuerySpec(Contract):
    query_id: Identifier
    text: ShortText
    route: Literal["semantic", "lexical", "entity", "temporal", "open_loop", "archive"]
    entity_ids: Annotated[list[Identifier], Field(max_length=16)]
    time_filter: TimeWindow | None = None
    purpose: Literal["answer", "clarify", "continuity", "reminisce"]


class QueryInput(ScopedRequest):
    latest_event: SourceEvent
    recent_topic: ShortText | None
    unresolved_refs: Annotated[list[VersionRef], Field(max_length=32)]
    known_entity_ids: Annotated[list[Identifier], Field(max_length=32)]
    budget: Budget
    allow_model_expansion: bool = False


class QueryOutput(Contract):
    queries: Annotated[list[QuerySpec], Field(max_length=5)]
    ambiguous: bool
    clarification: ShortText | None
    planner_version: Identifier


class EmbeddingProfile(Contract):
    profile_id: Identifier
    model_id: Identifier
    revision: Identifier
    dimensions: Annotated[int, Field(ge=1, le=65536)]
    metric: Literal["cosine"]
    normalized: bool


class EmbeddingInput(ScopedRequest):
    profile: EmbeddingProfile
    items: Annotated[list[tuple[VersionRef, BodyText]], Field(min_length=1, max_length=128)]
    purpose: Literal["index", "query", "migration"]


class EmbeddedItem(Contract):
    ref: VersionRef
    values: Annotated[
        list[Annotated[float, Field(allow_inf_nan=False)]], Field(min_length=1, max_length=65536)
    ]


class EmbeddingOutput(Contract):
    profile: EmbeddingProfile
    items: Annotated[list[EmbeddedItem], Field(max_length=128)]
    input_tokens: NonNegativeInt
    index_generation: Identifier


class RetrievalInput(ScopedRequest):
    queries: Annotated[list[QuerySpec], Field(min_length=1, max_length=5)]
    profile: EmbeddingProfile
    budget: Budget
    include_historical: bool
    read_system_as_of: AwareDatetime | None = None
    source_cursor: Identifier | None = None
    archive_cursor: Identifier | None = None


class CandidateRank(Contract):
    ref: VersionRef
    route: Literal["semantic", "lexical", "entity", "temporal", "open_loop", "archive"]
    query_id: Identifier
    rank: PositiveInt
    raw_score: Annotated[float, Field(allow_inf_nan=False)] | None
    summary_path: Annotated[list[VersionRef], Field(max_length=8)]


class RetrievedMemory(Contract):
    record: MemoryRecord
    ranks: Annotated[list[CandidateRank], Field(min_length=1, max_length=32)]
    fused_score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    decay_freshness: UnitScore
    final_score: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    evidence_level: Literal["raw", "evidence_capsule", "summary_only"]
    permitted_use: Literal["normal", "implicit_only", "clarify_only"]


class RetrievalOutput(Contract):
    candidates: Annotated[list[RetrievedMemory], Field(max_length=1000)]
    selected_refs: Annotated[list[VersionRef], Field(max_length=32)]
    cold_status: Literal["not_needed", "complete", "pending", "unavailable"]
    next_cursor: Identifier | None
    archive_cursor: Identifier | None
    missing_reason: ShortText | None
    memory_revision: NonNegativeInt
    visibility: MemoryVisibility


class RecentMessage(Contract):
    message_id: Identifier
    role: Literal["user", "assistant", "tool"]
    text: BodyText
    tool_group_id: Identifier | None = None
    output_evidence: Literal[
        "not_applicable",
        "generated",
        "published",
        "observed_partial",
        "observed_completed",
        "unknown",
    ]
    source_event_id: Identifier


class PersonaCard(Contract):
    persona_id: Identifier
    version: PositiveInt
    authored_identity: BodyText
    behavior_boundaries: Annotated[list[ShortText], Field(max_length=32)]
    preferred_style: Annotated[list[ShortText], Field(max_length=32)]
    lore_refs: Annotated[list[VersionRef], Field(max_length=64)]
    approved_at: AwareDatetime


class ToneState(Contract):
    label: Literal["neutral", "playful", "calm", "concerned", "focused", "unknown"]
    confidence: UnitScore
    expires_at: AwareDatetime
    text_guidance: ShortText
    evidence_event_ids: Annotated[list[Identifier], Field(max_length=16)]


class PersonaInput(ScopedRequest):
    persona_id: Identifier
    requested_version: PositiveInt
    current_tone: ToneState | None
    latest_event: SourceEvent
    interaction_preferences: Annotated[list[MemoryRecord], Field(max_length=32)]


class PersonaOutput(Contract):
    persona: PersonaCard
    tone: ToneState
    permitted_actions: Annotated[list[Identifier], Field(max_length=128)]
    excluded_memory_refs: Annotated[list[VersionRef], Field(max_length=128)]


class ContextBlock(Contract):
    kind: Literal[
        "system",
        "persona",
        "tone",
        "profile",
        "recent",
        "memory",
        "open_loops",
        "runtime",
        "current_input",
    ]
    text: BodyText
    source_refs: Annotated[list[VersionRef], Field(max_length=256)]
    token_count: NonNegativeInt
    trusted_instruction: bool


class ManifestEntry(Contract):
    ref: VersionRef
    decision: Literal["included", "implicit_only", "clarify_only", "excluded"]
    reason: ShortText
    used_tokens: NonNegativeInt


class ContextInput(ScopedRequest):
    persona: PersonaCard
    tone: ToneState
    latest_event: SourceEvent
    recent_messages: Annotated[list[RecentMessage], Field(max_length=128)]
    summaries: Annotated[list[SummaryNode], Field(max_length=16)]
    retrieved: Annotated[list[RetrievedMemory], Field(max_length=128)]
    active_loop_refs: Annotated[list[VersionRef], Field(max_length=32)]
    generation_id: Identifier
    budget: Budget


class ContextOutput(Contract):
    generation_id: Identifier
    blocks: Annotated[list[ContextBlock], Field(min_length=1, max_length=32)]
    manifest: Annotated[list[ManifestEntry], Field(max_length=256)]
    manifest_id: Identifier
    token_count: NonNegativeInt
    memory_revision: NonNegativeInt
    privacy_epoch: NonNegativeInt
    omitted_reasons: Annotated[list[ShortText], Field(max_length=32)]
    needs_clarification: bool


class OpenLoop(Contract):
    ref: VersionRef
    title: ShortText
    event_time: TimeWindow
    timezone: Identifier
    status: Literal["planned", "pending_outcome", "completed", "cancelled", "rescheduled"]
    sources: Annotated[list[EvidencePointer], Field(min_length=1, max_length=32)]
    linked_memory_refs: Annotated[list[VersionRef], Field(max_length=32)]
    user_allows_followup: bool
    last_followup_at: AwareDatetime | None = None


class OpenLoopInput(ScopedRequest):
    current: Annotated[list[OpenLoop], Field(max_length=128)]
    events: Annotated[list[SourceEvent], Field(max_length=32)]
    max_followups: Annotated[int, Field(ge=0, le=3)] = 1
    followup_cooldown_hours: Annotated[int, Field(ge=1, le=720)] = 72


class OpenLoopOutput(Contract):
    updates: Annotated[list[OpenLoop], Field(max_length=128)]
    followup_candidates: Annotated[list[VersionRef], Field(max_length=3)]
    clarification: ShortText | None
    external_notifications_created: Literal[False] = False


class ClientCapabilities(Contract):
    command_versions: Annotated[list[Literal[1, 2]], Field(min_length=1, max_length=2)]
    action_cancel: bool
    action_completed_receipt: bool
    playback_granularity: Literal["none", "output", "segment", "time"]
    media_clock_supported: bool


class OutputPlan(Contract):
    output_id: Identifier
    session_id: Identifier
    turn_id: Identifier
    generation_id: Identifier
    speech_id: Identifier | None
    spoken_text: BodyText | None
    action_ids: Annotated[list[Identifier], Field(max_length=16)]
    media_asset_id: Identifier | None = None
    sequence: NonNegativeInt
    expires_at: AwareDatetime


class OutputObservation(Contract):
    observation_id: Identifier
    output_id: Identifier
    generation_id: Identifier
    observed_at: AwareDatetime
    received_at: AwareDatetime
    client_sequence: NonNegativeInt
    state: Literal["accepted", "started", "partial", "completed", "cancelled", "failed", "unknown"]
    source: Literal["client", "renderer", "livekit", "server"]
    segment_id: Identifier | None
    position_ms: NonNegativeInt | None
    media_clock_id: Identifier | None


class OutputInput(ScopedRequest):
    operation: Literal["plan", "publish", "observe", "interrupt", "resume"]
    plan: OutputPlan
    capabilities: ClientCapabilities
    observations: Annotated[list[OutputObservation], Field(max_length=128)]


class OutputOutput(Contract):
    output_id: Identifier
    generation_id: Identifier
    state: Literal[
        "planned",
        "generated",
        "published",
        "started",
        "partial",
        "completed",
        "interrupted",
        "cancelled",
        "failed",
        "unknown",
    ]
    safe_spoken_prefix: ShortText | None
    evidence_granularity: Literal["none", "output", "segment", "time"]
    cancelled_action_ids: Annotated[list[Identifier], Field(max_length=16)]
    durable_event_id: Identifier


# Source: docs/voice-memory-design/contracts/03-operations.md
class JobSpec(Contract):
    job_id: Identifier
    kind: Literal[
        "extract",
        "embed",
        "summarize",
        "reflect",
        "tier",
        "archive",
        "delete",
        "reindex",
        "restore",
        "export",
    ]
    payload_ref: Identifier
    payload_schema: Identifier
    source_refs: Annotated[list[VersionRef], Field(max_length=256)]
    input_watermark: NonNegativeInt
    privacy_epoch: NonNegativeInt
    idempotency_key: Identifier
    available_at: AwareDatetime
    max_attempts: Annotated[int, Field(ge=1, le=10)] = 5


class JobInput(ScopedRequest):
    operation: Literal["enqueue", "claim", "renew", "complete", "fail"]
    job: JobSpec
    worker_id: Identifier
    lease_token: Identifier | None
    expected_attempt: NonNegativeInt
    error: ErrorInfo | None = None


class JobOutput(Contract):
    job_id: Identifier
    state: Literal["queued", "running", "retry_wait", "done", "obsolete", "dead"]
    attempt: NonNegativeInt
    lease_token: Identifier | None
    lease_until: AwareDatetime | None
    next_attempt_at: AwareDatetime | None


class ArchiveManifest(Contract):
    archive_id: Identifier
    scope: Scope
    time_range: TimeWindow
    object_ref: Identifier
    content_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    schema_version: PositiveInt
    encryption_key_version: Identifier
    record_count: NonNegativeInt
    memory_revision: NonNegativeInt
    privacy_epoch: NonNegativeInt
    state: Literal["staged", "verified", "active", "invalidated"]


class ArchiveInput(ScopedRequest):
    operation: Literal["write", "verify", "read", "promote"]
    record_refs: Annotated[list[VersionRef], Field(max_length=1000)]
    archive: ArchiveManifest | None
    max_read_bytes: Annotated[int, Field(ge=1, le=104857600)] = 8388608
    cursor: Identifier | None = None


class ArchiveOutput(Contract):
    manifest: ArchiveManifest | None
    restored_records: Annotated[list[MemoryRecord], Field(max_length=1000)]
    restored_capsules: Annotated[list[EvidenceCapsule], Field(max_length=1000)]
    source_metadata: Annotated[list[SourceDescriptor], Field(max_length=1000)]
    verified: bool
    next_cursor: Identifier | None
    bytes_processed: NonNegativeInt
    may_remove_online_copy: bool


class PrivacyPreference(Contract):
    long_term_memory_enabled: bool
    raw_transcript_days: Annotated[int, Field(ge=1, le=3653)] = 90
    retain_evidence_capsules: bool = True
    allow_proactive_followup: bool = False
    allow_anniversary_recall: bool = False
    cross_companion_share: Literal["none", "explicit_allowlist"] = "none"
    permitted_shared_predicates: Annotated[list[Identifier], Field(max_length=64)]
    preference_version: PositiveInt


class PrivacyInput(ScopedRequest):
    operation: Literal[
        "update_preferences",
        "delete_memory",
        "delete_sources",
        "delete_range",
        "delete_account",
        "query_deletion",
    ]
    preferences: PrivacyPreference | None
    target_refs: Annotated[list[VersionRef], Field(max_length=1000)]
    target_source_ids: Annotated[list[Identifier], Field(max_length=1000)]
    target_time: TimeWindow | None
    expected_preference_version: PositiveInt
    confirmed_control_event_id: Identifier
    deletion_job_id: Identifier | None


class PrivacyOutput(Contract):
    privacy_epoch: NonNegativeInt
    logically_revoked_refs: Annotated[list[VersionRef], Field(max_length=1000)]
    deletion_job_id: Identifier | None
    state: Literal["applied", "revoked_cleaning", "completed", "awaiting_confirmation"]
    cleaned_classes: Annotated[
        list[
            Literal[
                "source",
                "memory",
                "summary",
                "profile",
                "embedding",
                "archive",
                "cache",
                "export",
                "backup_manifest",
            ]
        ],
        Field(max_length=10),
    ]
    backup_expiry_at: AwareDatetime | None


class MigrationSpec(Contract):
    migration_id: Identifier
    kind: Literal["schema", "embedding", "encryption_key", "archive_format", "restore"]
    source_version: Identifier
    target_version: Identifier
    batch_size: Annotated[int, Field(ge=1, le=10000)] = 500
    dry_run: bool = True
    expected_deleted_watermark: NonNegativeInt
    target_embedding_profile: EmbeddingProfile | None = None


class MigrationInput(ScopedRequest):
    spec: MigrationSpec
    cursor: Identifier | None
    manifest_ids: Annotated[list[Identifier], Field(max_length=1000)]
    validation_dataset_version: Identifier


class MigrationOutput(Contract):
    migration_id: Identifier
    state: Literal["planned", "copying", "verifying", "ready_to_switch", "switched", "failed"]
    copied_count: NonNegativeInt
    verified_count: NonNegativeInt
    deleted_records_reintroduced: NonNegativeInt
    checksum_failures: NonNegativeInt
    next_cursor: Identifier | None
    rollback_manifest_ref: Identifier | None
    approval_required_for_switch: Literal[True] = True


class EvaluationCase(Contract):
    case_id: Identifier
    scenario: Literal[
        "fact",
        "time",
        "update",
        "abstention",
        "identity",
        "privacy",
        "compression",
        "decay",
        "archive",
        "relationship",
        "interruption",
        "migration",
    ]
    simulated_now: AwareDatetime
    event_refs: Annotated[list[Identifier], Field(max_length=1024)]
    expected_refs: Annotated[list[VersionRef], Field(max_length=128)]
    forbidden_refs: Annotated[list[VersionRef], Field(max_length=128)]
    expected_behavior: ShortText
    requires_human_review: bool


class EvaluationInput(Contract):
    meta: RequestMeta
    run_id: Identifier
    dataset_version: Identifier
    implementation_version: Identifier
    model_profile_version: Identifier
    cases: Annotated[list[EvaluationCase], Field(min_length=1, max_length=1000)]
    clock_mode: Literal["simulated", "wall_clock"]
    capacity_manifest_ref: Identifier | None = None
    load_profile_id: Identifier | None = None
    acceptance_spec_version: Literal["0.3"] = "0.3"


class CaseResult(Contract):
    case_id: Identifier
    passed: bool
    retrieved_refs: Annotated[list[VersionRef], Field(max_length=128)]
    forbidden_reference_count: NonNegativeInt
    unsupported_claim_count: NonNegativeInt
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    tokens_used: NonNegativeInt
    failure_stage: Identifier | None
    adjudication_ref: Identifier | None
    metric_counts: Annotated[list[MetricCount], Field(max_length=32)] = Field(default_factory=list)
    answer_result: Literal[
        "answered",
        "clarified",
        "abstained",
        "timed_out",
        "rejected",
        "system_failed",
        "not_applicable",
    ] = "not_applicable"
    degraded: bool = False


class EvaluationOutput(Contract):
    run_id: Identifier
    results: Annotated[list[CaseResult], Field(max_length=1000)]
    human_review_pending: NonNegativeInt
    critical_failures: NonNegativeInt
    report_ref: Identifier
    joint_acceptance_report_ref: Identifier | None = None


class IdentityInput(Contract):
    meta: RequestMeta
    credential_kind: Literal["anonymous_cookie", "oidc_callback", "server_dispatch"]
    credential: Annotated[str, Field(min_length=1, max_length=16384, repr=False)]
    requested_companion_id: Identifier
    origin: Identifier
    expected_room: Identifier | None


class IdentityOutput(Contract):
    principal_id: Identifier
    scope: Scope
    assurance: Literal["anonymous", "account", "trusted_worker"]
    credential_expires_at: AwareDatetime
    long_term_save_allowed: bool
    authorized_session_id: Identifier | None


class SessionInput(ScopedRequest):
    operation: Literal["create", "resume", "end", "renew_owner"]
    session_id: Identifier | None
    capabilities: ClientCapabilities
    memory_enabled: bool
    idempotency_key: Identifier


class SessionOutput(Contract):
    session_id: Identifier
    room_name: Identifier
    participant_identity: Identifier
    owner_epoch: NonNegativeInt
    status: Literal["ready", "active", "reconnecting", "ended"]
    rtc_credential_ref: Identifier | None
    resume_watermark: NonNegativeInt


class InputEventInput(ScopedRequest):
    session_id: Identifier
    event: SourceEvent
    memory_enabled: bool
    expected_turn_revision: NonNegativeInt


class InputEventOutput(Contract):
    turn_id: Identifier
    accepted_revision: PositiveInt
    persisted: bool
    input_watermark: NonNegativeInt
    outbox_ids: Annotated[list[Identifier], Field(max_length=32)]
    cancelled_generation_ids: Annotated[list[Identifier], Field(max_length=32)]
    visibility: MemoryVisibility


class ModelMessage(Contract):
    role: Literal["system", "user", "assistant"]
    content: BodyText


class ModelTaskInput(ScopedRequest):
    task: Literal["extract", "query", "compress", "reflect", "tone", "dialogue"]
    profile_id: Identifier
    prompt_version: Identifier
    output_schema: Literal[
        "admission.v1",
        "query.v1",
        "compression.v1",
        "reflection.v1",
        "persona.v1",
        "dialogue.text.v1",
    ]
    messages: Annotated[list[ModelMessage], Field(min_length=1, max_length=128)]
    max_output_tokens: Annotated[int, Field(ge=1, le=16384)]


class DialogueDraft(Contract):
    text: BodyText
    proposed_action_ids: Annotated[list[Identifier], Field(max_length=16)]
    used_memory_refs: Annotated[list[VersionRef], Field(max_length=32)]


class ModelTaskOutput(Contract):
    task: Literal["extract", "query", "compress", "reflect", "tone", "dialogue"]
    profile_id: Identifier
    payload: (
        AdmissionOutput
        | QueryOutput
        | CompressionOutput
        | ReflectionOutput
        | PersonaOutput
        | DialogueDraft
    )
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    provider_request_ref: Identifier
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ExportInput(ScopedRequest):
    operation: Literal["create", "verify", "import_dry_run", "import_apply"]
    export_id: Identifier | None
    selected_namespaces: Annotated[
        list[Literal["life", "relationship", "character_lore"]], Field(min_length=1, max_length=3)
    ]
    include_source_capsules: bool
    include_raw_transcript: bool
    explicit_user_confirmation_event: Identifier


class ExportOutput(Contract):
    export_id: Identifier
    state: Literal["queued", "ready", "verified", "needs_review", "imported", "revoked"]
    manifest_ref: Identifier
    record_count: NonNegativeInt
    schema_version: PositiveInt
    conflicts: Annotated[list[ShortText], Field(max_length=128)]
    delivery_handle: Identifier | None


class RepositoryInput(ScopedRequest):
    operation: Literal["read_versions", "scan_fact_key", "scan_period", "read_sources"]
    refs: Annotated[list[VersionRef], Field(max_length=1000)]
    subject_id: Identifier | None = None
    predicate: Identifier | None = None
    period: TimeWindow | None = None
    cursor: Identifier | None = None
    page_size: Annotated[int, Field(ge=1, le=1000)] = 128


class RepositoryOutput(Contract):
    records: Annotated[list[MemoryRecord], Field(max_length=1000)]
    sources: Annotated[list[SourceEvent], Field(max_length=1000)]
    capsules: Annotated[list[EvidenceCapsule], Field(max_length=1000)]
    source_metadata: Annotated[list[SourceDescriptor], Field(max_length=1000)]
    next_cursor: Identifier | None
    read_watermark: NonNegativeInt


# Source: docs/voice-memory-design/contracts/04-http-boundary.md
class PageRequest(Contract):
    cursor: Identifier | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 30


class CreateSessionBody(Contract):
    companion_id: Identifier
    memory_enabled: bool
    capabilities: ClientCapabilities
    idempotency_key: Identifier


class ResumeSessionBody(Contract):
    last_event_sequence: NonNegativeInt
    capabilities: ClientCapabilities
    idempotency_key: Identifier


class EndSessionBody(Contract):
    reason: Literal["user_end", "disconnect", "timeout"]
    idempotency_key: Identifier


class RtcCredentials(Contract):
    server_url: Annotated[str, Field(min_length=1, max_length=2048)]
    participant_token: Annotated[str, Field(min_length=1, max_length=16384, repr=False)]
    expires_at: AwareDatetime


class PublicEventProcessing(Contract):
    event_id: Identifier
    source_revision: PositiveInt
    source_saved: bool
    usable_in_this_session: bool
    cross_session_source_visible: bool
    structured_state: Literal["pending", "completed", "rejected", "needs_confirmation", "failed"]
    index_state: Literal["pending", "completed", "skipped", "failed"]
    provisional: bool
    observed_at: AwareDatetime
    retry_after_ms: NonNegativeInt | None

    @model_validator(mode="after")
    def honest_processing_state(self):
        if self.cross_session_source_visible and not self.source_saved:
            raise ValueError("cross-session source must be durable")
        if self.structured_state == "completed" and not self.source_saved:
            raise ValueError("structured completion needs durable source")
        if self.index_state == "completed" and self.structured_state != "completed":
            raise ValueError("index completion needs structured completion")
        return self


class ProcessingStatusQuery(Contract):
    event_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]


class PublicProcessingStatus(Contract):
    items: Annotated[list[PublicEventProcessing], Field(max_length=32)]


class PublicSessionView(Contract):
    session_id: Identifier
    companion_id: Identifier
    status: Literal["ready", "active", "reconnecting", "ended"]
    memory_enabled: bool
    account_required_for_save: bool
    rtc: RtcCredentials | None
    resume_watermark: NonNegativeInt
    processing: Annotated[list[PublicEventProcessing], Field(max_length=32)] = Field(
        default_factory=list
    )


class CorrectTranscriptBody(Contract):
    source_event_id: Identifier
    expected_source_revision: PositiveInt
    corrected_text: BodyText
    idempotency_key: Identifier


class CorrectMemoryBody(Contract):
    expected_version: PositiveInt
    correction_text: BodyText
    idempotency_key: Identifier


class DeleteMemoryBody(Contract):
    expected_version: PositiveInt
    delete_related_derivatives: Literal[True] = True
    confirmed: Literal[True]
    idempotency_key: Identifier


class EvidenceView(Contract):
    source_label: ShortText
    occurred_at: TimeWindow
    granularity: Literal["raw", "evidence_capsule", "metadata_only"]
    quote: ShortText | None = None


class PublicMemoryView(Contract):
    memory_id: Identifier
    version: PositiveInt
    kind: Literal[
        "fact",
        "preference",
        "episode",
        "relationship",
        "future_intent",
        "interaction_preference",
        "reflection",
    ]
    text: BodyText
    valid_time: TimeWindow
    label: Literal["user_report", "confirmed", "hypothesis", "fiction"]
    pinned: bool
    evidence: Annotated[list[EvidenceView], Field(max_length=16)]
    state: Literal["current", "historical", "uncertain"]


class PublicMemoryPage(Contract):
    items: Annotated[list[PublicMemoryView], Field(max_length=100)]
    next_cursor: Identifier | None
    revision: NonNegativeInt


class PublicTimelineItem(Contract):
    memory_id: Identifier
    version: PositiveInt
    title: ShortText
    period: TimeWindow
    related_names: Annotated[list[ShortText], Field(max_length=16)]
    status: Literal["confirmed", "reported", "uncertain"]
    protected_from_proactive_recall: bool


class PublicTimelinePage(Contract):
    items: Annotated[list[PublicTimelineItem], Field(max_length=100)]
    next_cursor: Identifier | None


class UpdatePreferencesBody(Contract):
    expected_version: PositiveInt
    long_term_memory_enabled: bool
    raw_transcript_days: Annotated[int, Field(ge=1, le=3653)]
    retain_evidence_capsules: bool
    allow_proactive_followup: bool
    allow_anniversary_recall: bool
    cross_companion_share: Literal["none", "explicit_allowlist"]
    permitted_shared_predicates: Annotated[list[Identifier], Field(max_length=64)]
    idempotency_key: Identifier


class PublicDeletionView(Contract):
    deletion_job_id: Identifier
    state: Literal["revoked_cleaning", "completed", "failed"]
    logical_revoked_at: AwareDatetime
    physical_cleanup_complete: bool
    backup_expiry_at: AwareDatetime | None
    retryable: bool


class UpdateOpenLoopBody(Contract):
    expected_version: PositiveInt
    status: Literal["planned", "completed", "cancelled", "rescheduled"]
    event_time: TimeWindow | None
    user_allows_followup: bool
    idempotency_key: Identifier


class PublicOpenLoopView(Contract):
    loop_id: Identifier
    version: PositiveInt
    title: ShortText
    event_time: TimeWindow
    status: Literal["planned", "pending_outcome", "completed", "cancelled", "rescheduled"]
    user_allows_followup: bool


class PublicOpenLoopPage(Contract):
    items: Annotated[list[PublicOpenLoopView], Field(max_length=100)]
    next_cursor: Identifier | None


class CreateExportBody(Contract):
    include_source_capsules: bool
    include_raw_transcript: bool
    confirmed: Literal[True]
    idempotency_key: Identifier


class ImportBody(Contract):
    uploaded_package_handle: Identifier
    dry_run: bool = True
    confirmed: bool = False
    idempotency_key: Identifier


class PublicExportView(Contract):
    export_id: Identifier
    state: Literal["queued", "ready", "verified", "needs_review", "imported", "revoked"]
    download_handle: Identifier | None
    expires_at: AwareDatetime | None
    conflicts: Annotated[list[ShortText], Field(max_length=128)]


class PublicIdentityView(Contract):
    state: Literal["anonymous", "account"]
    companion_ids: Annotated[list[Identifier], Field(max_length=128)]
    long_term_save_allowed: bool
    preferences_version: PositiveInt


class PublicApiResult(Contract, Generic[T]):
    status: Literal["ok", "partial", "error"]
    data: T | None = None
    error: ErrorInfo | None = None
    trace_id: Identifier

    @model_validator(mode="after")
    def public_result_shape(self):
        if self.status == "ok" and (self.data is None or self.error is not None):
            raise ValueError("ok requires data and no error")
        if self.status == "error" and (self.error is None or self.data is not None):
            raise ValueError("error requires error and no data")
        if self.status == "partial" and (self.data is None or self.error is None):
            raise ValueError("partial requires data and error")
        return self


# Source: docs/voice-memory-design/contracts/05-engineering.md
class DatabasePoolBudget(Contract):
    pool_id: Identifier
    lane: Literal["control", "dialogue", "deep_recall", "maintenance"]
    role: Literal["api", "agent", "job", "migration"]
    process_count: Annotated[int, Field(ge=1, le=10000)]
    pool_size_per_process: Annotated[int, Field(ge=1, le=1000)]
    max_overflow_per_process: Annotated[int, Field(ge=0, le=1000)]
    acquire_timeout_ms: Annotated[int, Field(ge=1, le=60000)]


class LaneQuota(Contract):
    lane: Literal["control", "dialogue", "deep_recall", "maintenance"]
    max_running: PositiveInt
    max_queued: NonNegativeInt
    max_queue_wait_ms: NonNegativeInt
    max_pending_payload_bytes: PositiveInt
    max_oldest_age_seconds: PositiveInt
    per_user_running: PositiveInt
    per_user_queued: NonNegativeInt
    reserved: bool


class ProviderQuota(Contract):
    quota_group_id: Identifier
    kind: Literal["llm", "embedding", "asr", "tts", "object_storage"]
    provider_profile_id: Identifier
    max_inflight: PositiveInt
    requests_per_minute: PositiveInt
    tokens_per_minute: PositiveInt | None
    reserved_control_inflight: NonNegativeInt
    reserved_dialogue_inflight: NonNegativeInt
    max_maintenance_inflight: NonNegativeInt

    @model_validator(mode="after")
    def allocation_fits(self):
        reserved = self.reserved_control_inflight + self.reserved_dialogue_inflight
        if reserved + self.max_maintenance_inflight > self.max_inflight:
            raise ValueError("provider allocations exceed shared inflight quota")
        return self


class CapacityManifest(Contract):
    manifest_id: Identifier
    revision: PositiveInt
    app_commit: Identifier
    dependency_lock_hash: Identifier
    algorithm_version: Identifier
    prompt_version: Identifier
    model_profile_ids: Annotated[list[Identifier], Field(min_length=1, max_length=64)]
    cpu_cores: PositiveInt
    ram_gib: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    gpu_description: Annotated[str, Field(max_length=1024)]
    database_max_connections: PositiveInt
    database_safety_fraction: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] = 0.70
    operations_reserved_connections: NonNegativeInt
    pools: Annotated[list[DatabasePoolBudget], Field(min_length=1, max_length=128)]
    lane_quotas: Annotated[list[LaneQuota], Field(min_length=4, max_length=4)]
    provider_quotas: Annotated[list[ProviderQuota], Field(min_length=1, max_length=64)]
    max_agent_processes: PositiveInt
    max_active_sessions: PositiveInt
    object_storage_profile_id: Identifier
    network_profile_id: Identifier
    browser_profile_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]
    dataset_version: Identifier
    frozen_at: AwareDatetime

    @model_validator(mode="after")
    def capacity_is_explicit(self):
        total = self.operations_reserved_connections + sum(
            p.process_count * (p.pool_size_per_process + p.max_overflow_per_process)
            for p in self.pools
        )
        if total > int(self.database_max_connections * self.database_safety_fraction):
            raise ValueError("database connection budget exceeded")
        expected = {"control", "dialogue", "deep_recall", "maintenance"}
        if {q.lane for q in self.lane_quotas} != expected:
            raise ValueError("exactly one quota per lane required")
        if len({p.pool_id for p in self.pools}) != len(self.pools):
            raise ValueError("duplicate database pool id")
        return self


class BudgetProfile(Contract):
    profile_id: Identifier
    path: Literal["normal", "warm_recall", "cold_recall", "control", "maintenance"]
    budget: Budget
    max_extra_model_calls: NonNegativeInt
    max_query_embedding_batches: NonNegativeInt
    max_retries_online: NonNegativeInt
    first_audio_hard_deadline_ms: PositiveInt | None = None
    max_reply_stream_ms: PositiveInt = 30000
    stream_idle_timeout_ms: PositiveInt = 5000
    final_answer_p95_ms: Annotated[int, Field(ge=1)] | None = None
    memory_preparation_p95_ms: Annotated[int, Field(ge=1)] | None = None
    max_archive_chunks: Annotated[int, Field(ge=0, le=32)]
    max_archive_bytes: NonNegativeInt

    @model_validator(mode="after")
    def phase_deadlines_are_coherent(self):
        audio_fields = (
            self.first_audio_hard_deadline_ms,
            self.final_answer_p95_ms,
            self.memory_preparation_p95_ms,
        )
        if self.path in {"normal", "warm_recall", "cold_recall"}:
            if any(v is None for v in audio_fields):
                raise ValueError("voice profile needs audio and preparation deadlines")
            if self.final_answer_p95_ms > self.first_audio_hard_deadline_ms:
                raise ValueError("P95 goal exceeds first-audio hard deadline")
            if self.budget.max_online_ms > self.first_audio_hard_deadline_ms:
                raise ValueError("memory child budget exceeds root deadline")
            if self.memory_preparation_p95_ms > self.budget.max_online_ms:
                raise ValueError("preparation P95 goal exceeds child hard budget")
        elif any(v is not None for v in audio_fields):
            raise ValueError("control and maintenance have no audio deadline")
        if self.stream_idle_timeout_ms > self.max_reply_stream_ms:
            raise ValueError("stream idle timeout exceeds stream lifetime")
        if self.path == "normal" and self.max_extra_model_calls != 0:
            raise ValueError("normal path has zero optional model calls")
        return self


class VisibilityReadInput(ScopedRequest):
    session_id: Identifier
    target_event_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]


class VisibilityReadOutput(Contract):
    visibility: MemoryVisibility
    source_readable_event_ids: Annotated[list[Identifier], Field(max_length=32)]
    structured_ready_event_ids: Annotated[list[Identifier], Field(max_length=32)]
    indexed_event_ids: Annotated[list[Identifier], Field(max_length=32)]
    unresolved_event_ids: Annotated[list[Identifier], Field(max_length=32)]
    retry_after_ms: NonNegativeInt | None


class ResourceAdmissionInput(ScopedRequest):
    capacity_manifest_id: Identifier
    resource_ids: Annotated[list[Identifier], Field(min_length=1, max_length=16)]
    estimated_tokens: NonNegativeInt
    estimated_payload_bytes: NonNegativeInt


class ResourceAdmissionOutput(Contract):
    admitted: bool
    lease_handles: Annotated[list[Identifier], Field(max_length=16)]
    decision: Literal["accepted", "partial_optional_routes", "overloaded", "quota_exceeded"]
    admitted_at: AwareDatetime | None
    retry_after_ms: NonNegativeInt | None
    rejected_resource_ids: Annotated[list[Identifier], Field(max_length=16)]


class CancelInput(ScopedRequest):
    session_id: Identifier
    generation_id: Identifier
    cancellation_token_id: Identifier
    reason: Literal[
        "user_interrupt",
        "input_revision",
        "deadline",
        "owner_changed",
        "privacy_changed",
        "overload",
    ]
    work_handles: Annotated[list[Identifier], Field(max_length=64)]


class CancelOutput(Contract):
    generation_id: Identifier
    invalidated: bool
    released_handles: Annotated[list[Identifier], Field(max_length=64)]
    detached_bounded_handles: Annotated[list[Identifier], Field(max_length=64)]
    remaining_slots_consumed: NonNegativeInt
    followup_check_at: AwareDatetime | None


class LoadProfile(Contract):
    profile_id: Identifier
    active_sessions: PositiveInt
    final_turns_per_second: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    ordinary_fraction: UnitScore
    recall_fraction: UnitScore
    control_fraction: UnitScore
    warmup_minutes: PositiveInt = 10
    steady_minutes: PositiveInt = 30
    burst_multiplier: Annotated[float, Field(ge=1, allow_inf_nan=False)] = 2.0
    burst_minutes: PositiveInt = 5
    recovery_minutes: PositiveInt = 10
    independent_runs: Annotated[int, Field(ge=1, le=100)] = 3
    open_loop_arrivals: Literal[True] = True
    background_maintenance_enabled: bool
    background_backlog_minutes: NonNegativeInt
    records_per_user: PositiveInt
    cold_fraction: UnitScore

    @model_validator(mode="after")
    def traffic_mix_sums_to_one(self):
        total = self.ordinary_fraction + self.recall_fraction + self.control_fraction
        if abs(total - 1.0) > 1e-9:
            raise ValueError("traffic fractions must sum to 1")
        return self


class Percentiles(Contract):
    count: NonNegativeInt
    p50: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    p95: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    p99: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None

    @model_validator(mode="after")
    def percentiles_are_ordered(self):
        values = (self.p50, self.p95, self.p99)
        if self.count == 0:
            if any(v is not None for v in values):
                raise ValueError("empty sample must not invent percentiles")
        elif any(v is None for v in values):
            raise ValueError("nonempty report needs all percentiles")
        elif not (self.p50 <= self.p95 <= self.p99):
            raise ValueError("unordered percentiles")
        return self


class QualityObservation(Contract):
    counts: MetricCount
    confidence_level: Annotated[float, Field(gt=0, lt=1)] = 0.95
    interval_low: UnitScore | None
    interval_high: UnitScore | None
    adjudication_version: Identifier
    subgroup: Identifier

    @model_validator(mode="after")
    def interval_matches_sample(self):
        if self.counts.denominator == 0:
            if self.interval_low is not None or self.interval_high is not None:
                raise ValueError("zero denominator has no confidence interval")
        elif self.interval_low is None or self.interval_high is None:
            raise ValueError("evaluated metric needs confidence interval")
        elif self.interval_low > self.interval_high:
            raise ValueError("invalid interval ordering")
        return self


class RequestOutcomeCounts(Contract):
    offered: NonNegativeInt
    admitted: NonNegativeInt
    completed: NonNegativeInt
    timed_out: NonNegativeInt
    rejected: NonNegativeInt
    system_failed: NonNegativeInt
    user_cancelled: NonNegativeInt
    system_cancelled: NonNegativeInt
    degraded: NonNegativeInt

    @model_validator(mode="after")
    def outcome_accounting(self):
        terminal = (
            self.completed
            + self.timed_out
            + self.rejected
            + self.system_failed
            + self.user_cancelled
            + self.system_cancelled
        )
        if terminal != self.offered:
            raise ValueError("every offered request needs one terminal outcome")
        if self.admitted + self.rejected != self.offered:
            raise ValueError("admission counts do not cover offered requests")
        if self.degraded > self.admitted:
            raise ValueError("degradation count exceeds admitted requests")
        return self


class GateResult(Contract):
    gate_id: Identifier
    observed_value: Annotated[float, Field(allow_inf_nan=False)] | None
    threshold: Annotated[float, Field(allow_inf_nan=False)]
    comparison: Literal["le", "ge", "eq"]
    state: Literal["passed", "failed", "not_measured", "insufficient_sample"]
    sample_count: NonNegativeInt
    evidence_ref: Identifier

    @model_validator(mode="after")
    def passed_gate_has_measurement(self):
        if self.state == "passed":
            if self.observed_value is None or self.sample_count == 0:
                raise ValueError("passing gate needs a measurement")
            satisfied = (
                (self.comparison == "le" and self.observed_value <= self.threshold)
                or (self.comparison == "ge" and self.observed_value >= self.threshold)
                or (self.comparison == "eq" and self.observed_value == self.threshold)
            )
            if not satisfied:
                raise ValueError("gate measurement does not meet threshold")
        return self


class JointAcceptanceReport(Contract):
    report_id: Identifier
    spec_version: Literal["0.3"] = "0.3"
    capacity_manifest_id: Identifier
    load_profile_id: Identifier
    dataset_version: Identifier
    run_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    outcomes: RequestOutcomeCounts
    ordinary_audio_latency_ms: Percentiles
    warm_recall_answer_ms: Percentiles
    cold_recall_answer_ms: Percentiles
    memory_preparation_ms: Percentiles
    source_persistence_ms: Percentiles
    structured_visibility_ms: Percentiles
    index_visibility_ms: Percentiles
    event_loop_lag_ms: Percentiles
    quality: Annotated[list[QualityObservation], Field(max_length=256)]
    gates: Annotated[list[GateResult], Field(min_length=1, max_length=256)]
    critical_violation_count: NonNegativeInt
    detached_work_outstanding: NonNegativeInt
    raw_measurement_manifest_ref: Identifier
    verdict: Literal["passed", "failed", "incomplete"]

    @model_validator(mode="after")
    def passed_needs_all_evidence(self):
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("duplicate run ids")
        if len({g.gate_id for g in self.gates}) != len(self.gates):
            raise ValueError("duplicate gate ids")
        if self.verdict == "passed":
            if self.critical_violation_count != 0:
                raise ValueError("critical violation blocks release")
            if any(g.state != "passed" for g in self.gates):
                raise ValueError("unmeasured or failed gate blocks release")
            if self.detached_work_outstanding != 0:
                raise ValueError("unreleased work blocks final report")
            required = {
                "G01_ordinary_latency",
                "G02_memory_preparation",
                "G03_recall_completion",
                "G04_request_outcomes",
                "G05_interference",
                "G06_visibility",
                "G07_bounded_resources",
                "G08_quality_overall",
                "G09_quality_subgroups",
                "G10_critical_zero",
                "G11_repeatability",
                "G12_recovery",
                "G13_evidence_manifest",
            }
            if not required.issubset({g.gate_id for g in self.gates}):
                raise ValueError("missing mandatory acceptance gate")
            if len(self.run_ids) < 3 or not self.quality:
                raise ValueError("passing report requires repeated runs and quality data")
        return self
