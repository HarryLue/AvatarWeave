# 身份、长年存储、任务、删除、迁移与评估契约

依赖 [00](00-common.md)、[01](01-lifecycle.md)、[02](02-recall-runtime.md)，对应 M17—M23、M25—M26。全部为文档定义；不创建任何运行中的认证、归档或迁移服务。

```python
class JobSpec(Contract):
    job_id: Identifier
    kind: Literal[
        "extract", "embed", "summarize", "reflect", "tier",
        "archive", "delete", "reindex", "restore", "export"
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
        "update_preferences", "delete_memory", "delete_sources",
        "delete_range", "delete_account", "query_deletion"
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
    cleaned_classes: Annotated[list[Literal[
        "source", "memory", "summary", "profile",
        "embedding", "archive", "cache", "export", "backup_manifest"
    ]], Field(max_length=10)]
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
        "fact", "time", "update", "abstention", "identity",
        "privacy", "compression", "decay", "archive",
        "relationship", "interruption", "migration"
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
    metric_counts: Annotated[list[MetricCount], Field(max_length=32)] = Field(
        default_factory=list
    )
    answer_result: Literal[
        "answered", "clarified", "abstained", "timed_out",
        "rejected", "system_failed", "not_applicable"
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
        "admission.v1", "query.v1", "compression.v1",
        "reflection.v1", "persona.v1", "dialogue.text.v1"
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
    payload: AdmissionOutput | QueryOutput | CompressionOutput | ReflectionOutput | PersonaOutput | DialogueDraft
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    provider_request_ref: Identifier
    latency_ms: Annotated[float, Field(ge=0, allow_inf_nan=False)]

class ExportInput(ScopedRequest):
    operation: Literal["create", "verify", "import_dry_run", "import_apply"]
    export_id: Identifier | None
    selected_namespaces: Annotated[
        list[Literal["life", "relationship", "character_lore"]],
        Field(min_length=1, max_length=3)
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
```

## 跨字段与事务约束

- JobInput 的 claim 不接受调用者自选租约；claim 输出的 lease_token 后续 renew/complete/fail 必须校验。payload_ref 指向单一已冻结输入 schema 的存储对象，不能用任意 JSON 逃避契约。
- ArchiveInput.write 必须有 record_refs，read/verify/promote 必须有 archive；May_remove_online_copy 仅校验和、计数、权限、版本与删除状态全部通过后为真。对外 API 不暴露 object_ref 或密钥。
- PrivacyInput 按 operation 验证目标形状；delete_account 只接受当前用户确认，不能通过空 target_refs 猜测“删全部”。删除范围由独立授权决定。
- MigrationOutput.deleted_records_reintroduced 必须为 0 才能切换；恢复先应用删除水位，再开放读取。dry_run 不执行任何存储切换。
- EvaluationInput 无生产用户 Scope，运行于隔离测试域；用 fixture 映射 ID，不允许把任意生产正文导入测试报表。
- IdentityInput.credential 只在内存处理，不进入 trace、repr 或异常正文。字段 repr=False 不能代替 model_dump 的脱敏，日志层必须显式排除。
- IdentityOutput.long_term_save_allowed 仅在 account 或经过确认的升级流程中为真；匿名 cookie 不承诺十年可恢复，不能默默成为永久身份。
- SessionOutput.rtc_credential_ref 是服务端短期凭据句柄，实际 HTTP 交换由已认证同源响应发给当前用户；不把原始 JWT 持久存入业务记录。
- ModelTaskInput.output_schema 与 task、ModelTaskOutput.payload 类型固定对应，必须验证，不能让 Union 自动解析结果决定实际任务。模型只能提供候选内容；不能由模型自行声明已校验数据库来源、已归档或已播放。
- Export 的可下载包必须有单次或短期授权；服务端导入逐条重映射 ID、校验来源、适用域、时间、版本和删除状态。不能把另一用户的 ID 直接恢复为本人的证据。
- Repository 对任何 scan 都使用作用域与游标，scan_fact_key 返回 next_cursor 非空时不是完整比较集合。不得以 Python 侧过滤替代 SQL 侧用户隔离。
- 包含原文的内存对象、任务对象和序列化结果均受相同隐私规则约束；MD 中的声明不等于允许在所有日志、导出和队列复制数据。

