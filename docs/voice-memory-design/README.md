# 长期陪伴设计与模块契约索引

这是 [主技术方案](../voice-memory-implementation-plan.md) 的设计附件目录。所有文件为 Markdown；Python 代码块只定义待实现的数据契约，没有新增生产 Python 模块或运行服务。

版本 0.3 新增 [联合验收规范](engineering-slo.md)：速度、并发、新鲜度和质量必须在固定负载同时通过。内部 RequestMeta.schema_version 升至 2，不影响独立的 RTC/动作版本。

## 1 阅读顺序

1. [主方案](../voice-memory-implementation-plan.md)：产品范围、十年目标、架构、容量、路线图与审核入口。
2. [生命周期算法 M01—M10](algorithms.md)：准入、实体、时间、冲突、提交、分层、压缩、反思、衰减、遗忘。
3. [运行与长年维护 M11—M26](runtime-and-longevity.md)：召回、上下文、语音、可靠任务、归档、删除、迁移和验证。
4. [00 共享模型](contracts/00-common.md) → [01 生命周期模型](contracts/01-lifecycle.md) → [02 召回与实时模型](contracts/02-recall-runtime.md) → [03 长年操作模型](contracts/03-operations.md)。
5. [04 HTTP 公开边界](contracts/04-http-boundary.md)：浏览器输入、公开视图与模块请求的安全投影。
6. [联合验收规范](engineering-slo.md) 与 [05 工程契约](contracts/05-engineering.md)：容量预算、资源准入、取消、处理水位和验收报告。

## 2 模块输入输出总表

每个调用返回 ModuleResult[输出类型]；表中列的是具体 data 类型。评估与身份输入不继承 ScopedRequest，因为前者在隔离测试域运行，后者尚未建立可信 Scope。HTTP 请求只能使用公开输入子集，可信 Scope、版本栅栏与服务端时钟由服务端附加。

| 模块 | 职责 | 输入 BaseModel | 输出 data BaseModel | 契约文件 |
| --- | --- | --- | --- | --- |
| M01 | 候选抽取与准入 | AdmissionInput | AdmissionOutput | [01](contracts/01-lifecycle.md) |
| M02 | 实体消歧与规范化 | EntityInput | EntityOutput | [01](contracts/01-lifecycle.md) |
| M03 | 双时间解析 | TemporalInput | TemporalOutput | [01](contracts/01-lifecycle.md) |
| M04 | 冲突校验与提案 | ConflictInput | ConflictOutput | [01](contracts/01-lifecycle.md) |
| M05 | 原子提交与即时覆盖层 | CommitInput | CommitOutput | [01](contracts/01-lifecycle.md) |
| M06 | 温度分层与迁移计划 | TieringInput | TieringOutput | [01](contracts/01-lifecycle.md) |
| M07 | 有来源的层次压缩 | CompressionInput | CompressionOutput | [01](contracts/01-lifecycle.md) |
| M08 | 反思假设与画像候选 | ReflectionInput | ReflectionOutput | [01](contracts/01-lifecycle.md) |
| M09 | 时间衰减与独立确认强化 | DecayInput | DecayOutput | [01](contracts/01-lifecycle.md) |
| M10 | 遗忘、失效与保留期 | ForgetInput | ForgetOutput | [01](contracts/01-lifecycle.md) |
| M11 | 查询理解与扩展 | QueryInput | QueryOutput | [02](contracts/02-recall-runtime.md) |
| M12 | 混合召回与冷热检索 | RetrievalInput | RetrievalOutput | [02](contracts/02-recall-runtime.md) |
| M13 | 每轮上下文与使用策略 | ContextInput | ContextOutput | [02](contracts/02-recall-runtime.md) |
| M14 | 角色与动态语气 | PersonaInput | PersonaOutput | [02](contracts/02-recall-runtime.md) |
| M15 | 未完成事项与跟进候选 | OpenLoopInput | OpenLoopOutput | [02](contracts/02-recall-runtime.md) |
| M16 | 语音、动作与执行账本 | OutputInput | OutputOutput | [02](contracts/02-recall-runtime.md) |
| M17 | 持久任务与租约 | JobInput | JobOutput | [03](contracts/03-operations.md) |
| M18 | 冷归档与校验读取 | ArchiveInput | ArchiveOutput | [03](contracts/03-operations.md) |
| M19 | 隐私控制与删除传播 | PrivacyInput | PrivacyOutput | [03](contracts/03-operations.md) |
| M20 | 长年迁移与备份恢复 | MigrationInput | MigrationOutput | [03](contracts/03-operations.md) |
| M21 | 隔离评估与失败归因 | EvaluationInput | EvaluationOutput | [03](contracts/03-operations.md) |
| M22a | 认证与可信身份 | IdentityInput | IdentityOutput | [03](contracts/03-operations.md) |
| M22b | 会话创建与恢复 | SessionInput | SessionOutput | [03](contracts/03-operations.md) |
| M23a | 最终输入与修订 | InputEventInput | InputEventOutput | [03](contracts/03-operations.md) |
| M23b | 模型任务网关 | ModelTaskInput | ModelTaskOutput | [03](contracts/03-operations.md) |
| M24 | embedding 与索引 | EmbeddingInput | EmbeddingOutput | [02](contracts/02-recall-runtime.md) |
| M25 | 导出与导入 | ExportInput | ExportOutput | [03](contracts/03-operations.md) |
| M26 | 受控存储读接口 | RepositoryInput | RepositoryOutput | [03](contracts/03-operations.md) |

## 3 数据契约的边界

- 本组定义锁定 Pydantic v2 风格；正式实施时按仓库锁文件验证。BaseModel 负责形状、类型、有限数值与部分跨字段约束，服务负责权限、来源真实性、引用、算法和事务。
- 各文档代码块按 00→01→02→03→04→05 组合，无需虚构尚不存在的内部 import。未来落为 Python 模块时保持相同逻辑类型与引用关系，再拆明确 import。
- Scope、RequestMeta、VersionFence 为受信任内部请求信息。公开 HTTP 不暴露“用户自行填 scope 即可操作”的入口；API 使用公开字段构建内部请求。
- 所有服务失败均落为 ErrorInfo；partial 必须有可解释错误与已完成部分。模型生成候选和真实存储结果不可互换。
- v0.3 所有内部 RequestMeta 必须有 ExecutionControl，实际运行绑定受控资源与单调 deadline。ModuleResult 附 ExecutionReport/MemoryVisibility；公开 HTTP 使用 PublicApiResult 和 PublicEventProcessing，不透传内部资源 handle 与完整观测。
- 原模块之外的内部适配入口均有明确契约：M17/M23 调度器使用 ResourceAdmissionInput/Output、CancelInput/Output，M23/M26 可见性查询使用 VisibilityReadInput/Output。CapacityManifest、LoadProfile、QualityObservation、JointAcceptanceReport 定义验收证据，不由模型自报 passed 代替测量。
- 模型任务 payload 即使能解析为某个 Output，也必须按 task 对应类型与全部业务规则检查。model 输出中的水位、角色批准时间、来源引用等都不是可信服务器数据，必须重算或验证后由服务端填充。
- 定义的单批列表和文本上限是传输限制。超过限制分页或拆任务，不裁掉证据。初始 HTTP 正文上限 1 MiB，离线批次可经单独配置扩大。
- 所有代码块已做定义语法、Pydantic schema 生成检查；这仅证明契约可构造，不能证明算法、数据库、语音与隐私功能已实现。

## 4 审核与未来验证

审核时逐模块检查：输入从哪里得到、哪些字段不可信、输出怎样验证、失败或版本变化时怎样处理、占用何种资源、取消后怎样回收、何时对用户可见。不能只审模型名称。

后续必须补齐：正反序列化、非法类型/多余字段、错误结果形状、跨字段约束、引用失效、隐私代次、并发版本和协议兼容。模型 schema 测试不能替代真实 PostgreSQL 与浏览器语音验证。

算法参数与主方案冲突时停止对应阶段实施，记录差异后统一文档，不能自行选择更容易实现的一份。

工程验收以 engineering-slo 的完整 gate 为准。纯模型构造可以使用空执行观测，但生产返回与发布报告必须提供真实耗时、水位、结果分母和资源信息；不能把 schema 通过描述为性能或记忆效果已通过。

