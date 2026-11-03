# 共享数据类型与契约规则

状态：v0.3 设计契约，全部定义只存在于 Markdown，不是已部署模块。按本目录 00—05 的顺序读取 Python 代码块，可组合为一组 Pydantic v2 定义。定义中的类均继承 BaseModel；模块结果统一使用 ModuleResult[具体输出类]。输入也必须使用表中指定类，不使用裸 dict。

本文件对应 [主方案](../../voice-memory-implementation-plan.md) 与 [算法说明](../algorithms.md)。所有时间均带时区；服务端统一到 UTC。字段校验不等于权限校验、数据库引用完整性或事实真实性验证。

## 1 公共定义

```python
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, model_validator
)

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
        "persisted", "provisional", "structured", "indexed",
        "rejected", "needs_confirmation", "failed"
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
        "unauthorized", "scope_mismatch", "version_conflict",
        "deadline_exceeded", "provider_unavailable",
        "invalid_model_output", "stale_generation",
        "privacy_changed", "evidence_missing",
        "budget_exceeded", "storage_unavailable",
        "unsupported_capability", "invalid_transition",
        "not_found", "manual_review_required", "overloaded",
        "quota_exceeded", "cancelled", "memory_pending"
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
        "none", "user_interrupt", "input_revision", "deadline",
        "owner_changed", "privacy_changed", "overload"
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
        "write_precision", "important_write_recall", "write_recall",
        "required_evidence_coverage", "temporal_update_accuracy",
        "grounded_answer_accuracy", "unsupported_assertion_rate",
        "correct_abstention", "over_abstention",
        "mandatory_compression_coverage", "summary_precision",
        "cold_recovery_accuracy"
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
    warnings: Annotated[list[ShortText], Field(max_length=16)] = Field(
        default_factory=list
    )

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
        if self.start is not None and self.end is not None:
            if self.end < self.start:
                raise ValueError("time range reversed")
        return self

class EvidenceRef(Contract):
    event_id: Identifier
    source_revision: PositiveInt
    start_char: NonNegativeInt
    end_char: PositiveInt
    quote: ShortText
    origin: Literal[
        "user_text", "user_transcript", "user_ui",
        "tool_result", "output_observation", "authored_character"
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
    qualifiers: Annotated[list[Qualifier], Field(max_length=16)] = Field(
        default_factory=list
    )
    polarity: Literal["positive", "negative"]
    valid_time: TimeWindow

class MemoryRecord(Contract):
    ref: VersionRef
    scope: Scope
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
    sources: Annotated[list[EvidencePointer], Field(min_length=1, max_length=64)]
    source_dependencies: Annotated[list[VersionRef], Field(max_length=256)] = Field(
        default_factory=list
    )
    recorded_at: AwareDatetime
    system_from: AwareDatetime
    system_to: AwareDatetime | None = None
    status: Literal[
        "candidate", "active", "uncertain", "superseded", "expired", "deleted"
    ]
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
        if self.content_kind == "reflection":
            if self.assertion_kind != "model_hypothesis":
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
        "user_text", "user_transcript", "user_ui",
        "tool_result", "output_observation", "authored_character"
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
        "user_text", "user_transcript", "user_ui",
        "tool_result", "output_observation", "authored_character"
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
    default_half_life_days: Annotated[
        float, Field(gt=0, allow_inf_nan=False)
    ] | None = None

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
```

## 2 语义约定与服务端验证

- ID 是 opaque 标识，不表示权限。Scope 只能由认证层构造，不能因形状合法就信任客户端或模型提交的用户 ID。
- 时间区间使用左闭右开；TimeWindow.end 等于 start 只表示精确瞬间的点事件，比较时用点事件规则，不把它当作空事实。
- valid_time 是现实生效时间，system_from/system_to 是系统知道该版本的时间。另存事件时间与记录时间，不将其混为一个时间戳。
- EvidenceRef 使用 Python Unicode 字符索引，不是 UTF-8 字节或 JavaScript UTF-16 code unit。服务端验证原始片段完全一致；前端若传位置须显式转换。
- EvidenceRef 含原话，仅用于受授权的原始输入、候选验证或证据胶囊；持久 MemoryRecord、SummaryClaim、OpenLoop 使用不含原话的 EvidencePointer。关闭胶囊保留后，原文到期须将对应 pointer 降为 metadata_only，不能借嵌套 quote 偷留原话。来源元数据也受用户删除控制。
- SourceDescriptor 保存不含正文的来源与会话关系，使原文到期后仍能判断证据独立性和版本。Repository 可分别返回合法原文、胶囊和元数据，不构造 content 为空的伪 SourceEvent。
- canonical_value 是类型标记后的规范字符串：数字使用十进制字符串并以 qualifier 保存单位；布尔仅 true/false；实体为已解析 ID；日期需附精度。必须由 predicate registry 校验值域，不能仅靠 value_type 相信模型。
- 列表默认上限是单次调用限制，不是一个人一生只能有这么多记录。较大来源集合使用批次与依赖 DAG；不得悄悄截断来源。
- 记忆来源失效或已删除时，即使 MemoryRecord 能通过 Pydantic，也不得使用。引用校验、同域检查、时间精度推理和事务并发校验属于算法与 repository 的责任。
- privacy_epoch 是删除及授权栅栏；memory_revision 是用户记忆的提交水位；owner_epoch 是实时会话所有者代次。它们含义不同，不能用一个版本号替代。
- 实时输入/输出提交校验 owner_epoch 与 generation；已获准的后台记忆任务不因原会话 owner 更替自动失效，它按来源版本、相关事实版本与 privacy_epoch 校验。memory_revision 变化也不要求所有不相关任务重跑，只有读集合冲突时重算。
- RequestMeta.now 使用注入的服务端时钟，便于十年时间推进回放；不得在算法内部各自读取不受控当前时间。
- v0.3 将内部 RequestMeta.schema_version 升至 2，并要求 ExecutionControl。它不改变 RTC、动作或记忆来源的独立版本；未知内部 schema 明确拒绝，不静默构造空预算。公开调用者不能自行指定 lane、优先级、取消令牌或放大预算。
- ExecutionControl 是可序列化执行约束，不是实际信号量或取消器；运行时必须把它绑定到受控资源与单调时钟。子调用不得延长父请求剩余预算，事务内不等待模型或对象 I/O。
- MemoryVisibility 的水位是同一用户来源序列的连续 settled 前缀，不是最大完成 ID；rejected 也可 settled，但 pending/needs_confirmation 必须有明确目标状态。overlay 只证明指定会话可见，不是跨设备全局完成。
- 所有真实执行链返回 execution；含记忆处理/读取的链路还返回 visibility。为纯 schema 构造允许空值，不代表生产可以省略观测。MetricCount 分母为 0 表示不可评估，不自动当 100% 通过。
- Python 模型使用严格校验；JSON 入口应使用 model_validate_json，避免先粗暴转换类型。JSON 与 Python 的日期等严格校验细节以 Pydantic 官方说明及锁定版本为准。
- ModuleResult 的 partial 仅用于可说明的部分结果，例如冷热检索中冷层未就绪；身份拒绝、删除失败或越权不得转成“有结果就算成功”。

参考：[Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)、[Strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)。

