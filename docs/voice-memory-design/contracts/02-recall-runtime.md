# 检索、上下文、角色与语音执行契约

依赖 [00 共享类型](00-common.md)、[01 生命周期](01-lifecycle.md)。对应 M11—M16 与 M24。返回类型统一为 ModuleResult[具体输出]。候选召回、使用决策和输出播放为不同对象，禁止混用。

```python
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
        list[Annotated[float, Field(allow_inf_nan=False)]],
        Field(min_length=1, max_length=65536)
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
        "not_applicable", "generated", "published",
        "observed_partial", "observed_completed", "unknown"
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
        "system", "persona", "tone", "profile",
        "recent", "memory", "open_loops", "runtime", "current_input"
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
    state: Literal[
        "accepted", "started", "partial", "completed", "cancelled", "failed", "unknown"
    ]
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
        "planned", "generated", "published", "started", "partial",
        "completed", "interrupted", "cancelled", "failed", "unknown"
    ]
    safe_spoken_prefix: ShortText | None
    evidence_granularity: Literal["none", "output", "segment", "time"]
    cancelled_action_ids: Annotated[list[Identifier], Field(max_length=16)]
    durable_event_id: Identifier
```

## 服务规则

- M11 QueryOutput 最多返回 Budget.max_queries 个查询；查询时间过滤是解释，不得强制把不确定表达解析成确定日期。
- M24 EmbeddingOutput 每个向量维度等于 profile.dimensions，值有限、余弦范数非零；index_generation 与模型、预处理版本匹配。调用者根据目的保存或仅用于查询，不把输入正文复制到普通日志。
- M12 不同路的原始分数不可直接相加。CandidateRank.rank 从 1 开始；final_score 的公式见算法页。未完成冷层检索返回 partial，不返回“没有这件事”的确定结论。
- read_system_as_of 指系统当时知道的版本，查询的现实时间范围在 QuerySpec.time_filter，两者不能互换。除用户明确回顾当时认知，否则默认读取当前系统知识。
- M13 ContextBlock.trusted_instruction 仅 system/persona 中由受信任配置提供的指令可为真；记忆、摘要、当前输入与检索文本始终作为数据，不提升指令级别。
- current_input 恰好一块且 event_id 对应 latest_event；保留工具调用组完整性。ContextOutput.token_count 不能超过 Budget.max_input_tokens。
- M14 只有批准的角色版本可加载。动态 tone 过期退回 neutral；不永久保存瞬时情绪，不凭声学特征诊断人格。
- M15 到期只从 planned 转 pending_outcome；用户明确完成/取消才改变结果状态。再次安排同一事件生成版本变化，不自动创建外部通知。
- M16 OutputInput 各 operation 的允许字段组合必须在服务端显式验证；plan/publish 不能接受伪造 observations，observe 不能隐式重放动作。客户端身份从实际房间会话得到，不能信任 observation.source 的自称。
- safe_spoken_prefix 只有在已验证文字与语音片段对齐时才填写；无对齐证据时为 None，不能按 position_ms/总时长线性猜字。
- 时间位置只对同一媒体时钟可比；无 media_clock_id 时不得报告毫秒精确同步。completed 回执只证明设备观测，不证明用户理解或主观记住。

