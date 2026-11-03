# HTTP 边界的公开 Pydantic 模型

依赖 00—03；本页补足浏览器 API 的公开字段，不新增算法模块。API adapter 将下列公开输入与认证结果、服务端时钟、版本栅栏组合为内部请求。浏览器不得提交 Scope、owner_epoch、privacy_epoch、服务端 algorithm_version 或自称的 user_id。

```python
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
        "fact", "preference", "episode", "relationship",
        "future_intent", "interaction_preference", "reflection"
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
```

## API 映射

| API | 公开输入 | 公开响应 data |
| --- | --- | --- |
| GET /api/v2/me | 无 body，认证 cookie | PublicIdentityView |
| POST /api/v2/sessions | CreateSessionBody | PublicSessionView |
| POST /api/v2/sessions/{id}/resume | ResumeSessionBody | PublicSessionView |
| POST /api/v2/sessions/{id}/end | EndSessionBody | PublicSessionView |
| PATCH /api/v2/sessions/{id}/transcript | CorrectTranscriptBody | PublicSessionView，修订细节通过会话事件恢复 |
| GET /api/v2/sessions/{id}/memory-status | ProcessingStatusQuery query | PublicProcessingStatus，仅当前用户可读来源状态 |
| GET /api/v2/memories | PageRequest query | PublicMemoryPage |
| PATCH /api/v2/memories/{id} | CorrectMemoryBody | PublicMemoryView |
| DELETE /api/v2/memories/{id} | DeleteMemoryBody，或等价显式控制 POST | PublicDeletionView |
| GET /api/v2/timeline | PageRequest query，时间窗口由专用 TimeWindow 过滤 | PublicTimelinePage |
| PUT /api/v2/memory-preferences | UpdatePreferencesBody | PrivacyPreference 的公开字段 |
| GET /api/v2/open-loops | PageRequest query | PublicOpenLoopPage |
| PATCH /api/v2/open-loops/{id} | UpdateOpenLoopBody | PublicOpenLoopView |
| POST /api/v2/exports | CreateExportBody | PublicExportView |
| POST /api/v2/imports/dry-run 或 apply | ImportBody | PublicExportView |
| GET /api/v2/deletion-jobs/{id} | 无 body，认证 cookie | PublicDeletionView |

公开结果使用 PublicApiResult 包装，内部仍为 ModuleResult；adapter 不透传 ExecutionReport、内部水位、资源 handle 或归档位置。公开可见性使用 PublicEventProcessing，不能把 source_saved=True 显示为已结构化/已索引。HTTP 状态码与 ErrorInfo 一致：未认证 401、无权/不可披露目标采用统一 404、版本冲突 409、类型错误 422、overloaded/quota_exceeded 为 429 并带 retry_after、依赖不可用 503、异步接受 202。memory_pending 可返回 202/partial，明确具体待处理状态。权限失败不能泄露用户正文。

浏览器 query string 中的整数/日期由专用 adapter 显式转换和验证后构造严格模型，不关闭所有严格类型以兼容查询参数。目标时间过滤通过允许的字段解析成 TimeWindow，禁止自由 SQL 查询。

EvidenceView.quote 只在原文或胶囊仍合法存在且用户当前明确查看来源时提供；普通列表默认 None。PublicMemoryView 不返回内部对象路径、密钥、Scope 或完整 SourceEvent。

ImportBody 在 apply 时必须 dry_run=False 且 confirmed=True，已通过的预演包与账号/版本绑定；不能用改客户端字段绕过预演和所有者验证。默认拒绝恢复已删除记录，若用户明确恢复需新的独立控制事件。

所有写操作有 Origin/CSRF、请求体上限、幂等与认证。session 创建要求 memory_enabled=True 时具备 account 身份；匿名只能 memory_enabled=False。关掉保存但仍想访问旧记忆是否允许，必须由独立用户可见的“读取已有记忆”设置决定，默认临时无记忆会话不读取私人历史。

认证 callback 的网络载荷由锁定的 OIDC 库定义并校验，不自定义一套未验证的 token schema。账号恢复和生产认证提供方仍为部署审核项，不能用临时示例凭据代替。

