# 记忆形成与生命周期模块契约

依赖 [共享类型](00-common.md)。输入均继承 ScopedRequest；返回 ModuleResult[对应 Output]。本页定义 M01—M10，步骤及公式见 [算法说明](../algorithms.md)。模型生成的 Candidate、Plan 与真实提交结果分离。

```python
class CandidateMemory(Contract):
    candidate_id: Identifier
    namespace: Literal["life", "relationship", "character_lore"]
    content_kind: Literal[
        "fact", "preference", "episode", "relationship",
        "future_intent", "interaction_preference", "reflection"
    ]
    assertion_kind: Literal[
        "user_report", "user_confirmed", "tool_observed",
        "model_hypothesis", "authored_fiction"
    ]
    statement: Statement
    display_text: BodyText
    sources: Annotated[list[EvidenceRef], Field(min_length=1, max_length=32)]
    interpretation: Literal[
        "asserted", "hypothetical", "quoted_other", "negated",
        "joke_or_unclear", "fictional"
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
        "supported", "transient", "hypothetical", "fiction",
        "ambiguous_speaker", "unreliable_transcript",
        "insufficient_evidence", "privacy_disabled", "out_of_scope"
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
        "duplicate", "support", "temporal_change", "conditional_coexist",
        "contradiction", "independent", "uncertain"
    ]
    reason: ShortText
    overlapping_time: TimeWindow | None = None

class MutationProposal(Contract):
    action: Literal[
        "create", "add_evidence", "replace_interval",
        "retract", "coexist", "defer"
    ]
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
    source_events: Annotated[list[SourceEvent], Field(max_length=128)] = Field(
        default_factory=list
    )
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
        "retain", "lower_priority", "archive", "expire",
        "invalidate_derivative", "delete_by_user", "delete_by_retention"
    ]
    reason: ShortText
    legal_or_user_basis_ref: Identifier | None = None
    source_granularity_after: Literal["raw", "evidence_capsule", "summary_only", "none"]

class ForgetOutput(Contract):
    actions: Annotated[list[ForgetAction], Field(max_length=1000)]
    requires_deletion_job: bool
    next_cursor: Identifier | None = None
```

## 关键约束

1. AdmissionOutput.decisions 对每个 candidate_id 恰好一条，候选不重复；隐私关闭时 candidates 为空、没有含正文的异步载荷。
2. EntityOutput 的现有实体必须属于相同用户授权域；低置信匹配产生 ambiguous，不按相同名字自动合并。
3. TemporalInput 使用 anchor_event 的发生时间和用户时区解析相对日期；无法确定则 precision=unknown，禁止补造具体日期。
4. ConflictInput 的 comparable_records 是完整比较集合的一页；若同一事实键尚有未加载候选，不得宣布“无冲突”，需继续分页或返回 partial。单值 current 唯一性仍由提交事务维护。
5. CommitInput 不接受未经确认且 requires_user_confirmation 的提案。候选、规则、来源、目标版本和 privacy_epoch 在事务内重验；不把模型的 proposals 当数据库命令直接执行。
6. TieringOutput 是迁移计划，不等于数据已搬迁。需要 M18 完成校验后才能撤掉热层副本；迁移不能触发语义失效。
7. CompressionOutput.accepted=True 需要 summary 非空、mandatory_coverage=1、unsupported_claims 为空；失败保留源记录，不能用无摘要假装压缩成功。coverage 按受保护事实项计算，不按生成文本长度计算。
8. ReflectionOutput 中所有内容都是假设；确认后新增有用户确认来源的事实版本，而不是就地把无证据推断改成事实。
9. DecayInput 的确认计数与 record ref 一一对应，缺失按 0；只计算独立用户确认，不把系统自己检索、复述当作新确认。
10. ForgetPolicy 不允许仅因低权重物理删除生活记忆。原始转写 TTL 与生活记忆保留分别管理；用户删除与保留期到期均须进入可审计删除流程。
11. 数据跨字段约束由后续实现中的 model_validator 与服务校验落实；本页未用字段校验器表达的来源真实性、覆盖率、数据库权限与原子性，不得视为可省略。

