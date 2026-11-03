# 并发资源、记忆新鲜度与联合验收契约

版本：0.3。依赖 00—04，全部仅为 Markdown 中的 Pydantic 定义。执行规则与指标的唯一规范来源是 [联合验收规范](../engineering-slo.md)，主方案的性能表只是摘要。

RequestMeta.execution、ModuleResult.execution/visibility、MetricCount 已在 [共享类型](00-common.md) 定义；本页定义部署清单、负载和验收结果，不引入新的记忆算法模块。

```python
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
    database_safety_fraction: Annotated[
        float, Field(gt=0, le=1, allow_inf_nan=False)
    ] = 0.70
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
        "user_interrupt", "input_revision", "deadline",
        "owner_changed", "privacy_changed", "overload"
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
            self.completed + self.timed_out + self.rejected
            + self.system_failed + self.user_cancelled + self.system_cancelled
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
                "G01_ordinary_latency", "G02_memory_preparation",
                "G03_recall_completion", "G04_request_outcomes",
                "G05_interference", "G06_visibility", "G07_bounded_resources",
                "G08_quality_overall", "G09_quality_subgroups", "G10_critical_zero",
                "G11_repeatability", "G12_recovery", "G13_evidence_manifest"
            }
            if not required.issubset({g.gate_id for g in self.gates}):
                raise ValueError("missing mandatory acceptance gate")
            if len(self.run_ids) < 3 or not self.quality:
                raise ValueError("passing report requires repeated runs and quality data")
        return self
```

## 语义及执行约束

1. ResourceAdmission 是执行器内部入口，不是浏览器可以自选优先级的接口。资源 lease 在取消/失败后释放，仍在运行的不可强制终止任务保留有限槽，禁止重复释放超发。
2. 数据库池预算按所有实例/Agent 子进程真实上界统计，动态伸缩先更新清单；实例重启不重置共享供应商总额度。独立池也不能消除数据库 CPU/I/O 竞争。
3. MemoryVisibility 不把“来源可读”冒充“已结构化/已索引”；目标事件确认与全用户前缀水位分别使用。跨会话新原话应提供来源增量或 processing 状态，不默认等所有旧任务完成。
4. ExecutionReport.cleanup=detached_bounded 时必须有实际在运行 work handle 与有限槽计数。HTTP 已超时不等于底层 CPU 或模型已停；最终测试报告结束前需回收或明确失败。
5. QualityObservation 的分母为 0 必须不可评估；关键门槛不得通过从报告中删掉该指标来绕过。GateResult 的计算由固定比较器检查，不能相信测试模型自报 passed。
6. RequestOutcomeCounts 的六个 terminal outcome 互斥，degraded 是独立标签，可与 completed/timed_out 等重叠。超载引发客户端断开归 system_cancelled，不归 user_cancelled。
7. JointAcceptanceReport.passed 还需服务端对照 engineering-slo 中完整必需 gate ID、分组、最小样本、三次运行及后台开/关配对检查。Pydantic 的形状验证不能证明所有 gate 已提供、阈值真实或性能实测通过。
8. 输入数据不足、缺浏览器真实播放、只有 mock、只运行 L1 或缺背景干扰时，报告为 incomplete 或限定通过的档位，不宣称全产品已达标。
9. 指标、延迟的样本过滤与直方图边界版本化；Percentiles 仅成功样本分布必须配合 outcomes 和全请求时限报告，不允许排除慢请求后独立发布。
10. 本页的两个调度适配与可见性查询复用 M17/M23/M26，不新增一套匿名裸 JSON 调度服务。模块原有 IO 与 ModuleResult 共同构成契约。

