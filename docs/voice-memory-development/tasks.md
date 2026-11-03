# 语音记忆开发任务与阶段记录

设计基线：[实施方案 v0.3](../voice-memory-implementation-plan.md)、[算法](../voice-memory-design/algorithms.md)、[运行算法](../voice-memory-design/runtime-and-longevity.md)、[联合验收](../voice-memory-design/engineering-slo.md)及 00—05 契约。用户本轮已授权开始开发；设计文档的“尚未授权实现”是此前的状态快照，不再作为开发暂停条件。

本文件记录代码实际覆盖范围，不修改原设计的验收门槛。**目前交付的是基础批次，完整 P1 和 P2—P7 均未验收；尚不能在现有语音产品中使用长期记忆。** 单元/数据库测试不是真实语音、L1 容量或十年质量验收。

## 设计分析与依赖

1. 原工程只有 Conversation/Message 的房间级历史，没有可恢复身份；旧房间不能被昵称、客户端 identity 或共享 access key 认领。新增 `vm_*` 表与旧表不建立归属关系。
2. 来源事件、结构化事实、向量、上下文和播放观测是五种不同对象。首先建立来源版本、持久提交与可见性；后续抽取不能将原始转写直接当成 active 事实。
3. 双时间属于事实修订，不能用 SourceEvent.occurred_at/received_at 代替。当前批次转写修订已实现，MemoryRecord 的双时间契约已移植，**事实双时间存储与提交留给 VM-04**。
4. 实时链路只做授权、短事务、覆盖层、有界检索与组装。抽取/向量/压缩/反思/归档放到持久后台；模型或对象 I/O 不进入持锁事务。
5. 删除与纠正是控制链路，不能等异步向量；privacy、memory、owner、generation、source revision 的栅栏各有用途。当前只对来源写入/读取和会话租约实现对应检查，未声称完成输出取消或删除 DAG。
6. SQLite 适合单元回归，PostgreSQL 必须另外验证。真实语音、冷热对象、恢复和多用户质量需要后续联合验收，不能从 schema 或普通 API 测试推断通过。

依赖顺序：VM-01/02 → VM-03 → VM-04/05 → VM-06/07 → VM-08/09 → VM-10 → VM-11 → VM-12。单执行者顺序推进；本次没有启动多代理。

## 任务安排

| 任务 | P/M/T 与输入→输出（均以 ModuleResult 包装） | 可审查闭环和状态 |
| --- | --- | --- |
| VM-01 | P0；00—05 全部 Contract；T01/T09 及契约不变量 | **基础代码完成**：166 个模型按原附件转为运行代码，逐模型 JSON Schema 对照测试；未知 schema、严格类型、结果形状、水位顺序。不是 26 个算法已完成。 |
| VM-02 | P0；CapacityManifest、BudgetProfile、JointAcceptanceReport；T07/T10 | **进行中**：记录本地 hooks、隔离策略、开发数据库和池大小。待冻结供应商额度、硬件/进程清单、held-out、首发容量档；待实测文字/开场/语音的统一路径和设备播放。 |
| VM-03 | P1/M22/M23/M26；IdentityInput→IdentityOutput、SessionInput→SessionOutput、InputEventInput→InputEventOutput、VisibilityReadInput→VisibilityReadOutput、RepositoryInput→RepositoryOutput；T01/T09 | **已实现子集**：OIDC HTTP 适配、issuer/sub 稳定账号映射、会话创建/恢复/结束、可信 Worker 租约基础、最终来源修订、同事务有界 outbox、来源精确读取/元数据、TTL 批次擦除、跨会话处理状态。待真实身份提供方、匿名受控会话、账号恢复、HTTP 字幕修订、统一 ModelTask 网关、真实 Worker 输入桥。 |
| VM-04 | P1/P2/M01—M05/M26；Admission/Entity/Temporal/Conflict/Commit 的 Input→Output；T02/T06/T09 | **待开发**：谓词 registry、实体稳定 ID、来源区间逐字验证、双时间事实和胶囊/依赖表、确定性冲突、模型候选准入、事实键锁与不可变提交、纠正覆盖层。必须区分“过去住过”与“从未住过”。 |
| VM-05 | P2/M19；PrivacyInput→PrivacyOutput；04 公开记忆/删除模型；T06/T09 | **待开发**：查看、pin、纠正、删除、记忆设置与记忆页；逻辑撤销先提交、epoch 递增、目标状态可查询；来源/派生/归档/导出传播、旧任务和恢复防复活。当前偏好接口只允许尚无来源时的初始选择。 |
| VM-06 | P3/M11/M12/M24；QueryInput→QueryOutput、EmbeddingInput→EmbeddingOutput、RetrievalInput→RetrievalOutput；T02/T03/T10 | **待开发**：中文词法配置、同模型向量空间、单批 query embedding、精确事实/实体/时间通道、RRF、历史取消近期偏置、冷查 partial、独立池与最多四分支。参考 rag-zeroai 的 OpenAI 兼容接入，不能引入整套 RAG/Celery/Redis 作为捷径。 |
| VM-07 | P3/M13—M15/M23；Context/Persona/OpenLoop 的 Input→Output、ModelTaskInput→ModelTaskOutput；T02/T07/T09 | **待开发**：每轮预算上下文与 Manifest、规则/角色版本、provisional 覆盖层、互动边界、待反馈事项；文字、主动开场、语音统一栅栏，记忆模式关闭预生成；普通路径无额外查询/语气模型。 |
| VM-08 | P4/M17；JobInput→JobOutput；T06/T10 | **待开发**：outbox dispatcher、租约 claim/renew/complete/fail、有限退避、dead/obsolete、按用户公平调度、control/dialogue 保留配额；持久队列与供应商总配额共同有界。现有 outbox **尚无执行进程**。 |
| VM-09 | P4/M06—M10/M18；Tiering/Compression/Reflection/Decay/Forget/Archive 的 Input→Output；T03—T05 | **待开发**：分层只搬位置，protected 全覆盖压缩，反思可撤销、独立用户确认强化，低权重不删除人生事实；加密开放格式、写/回读校验/切清单后撤在线副本。密钥和对象托管待选。 |
| VM-10 | P5/M16；OutputInput→OutputOutput；T07 | **待开发**：v2 command/receipt、能力协商、生成/发布/开始/完成账本、实际媒体粒度、打断/迟到/重连、有限 detached 槽。v1 发送成功不能证明播放完成。 |
| VM-11 | P6/M19/M20/M25；Privacy/Migration/Export 的 Input→Output；T06/T08 | **待开发**：完整删除、备份删除重放、版本化迁移与 dry-run、数据携带、恢复确认、切换审核和故障演练。 |
| VM-12 | P6/P7/M21；EvaluationInput→EvaluationOutput、JointAcceptanceReport；T01—T10 | **待开发**：100 开发案例、≥1000 held-out 跨会话问题与关键分组、独立故障集；固定负载三次运行、后台干扰与恢复、真实设备音频、成本、盲评与灰度。报告未测 gate，不能用空分母或 mock 通过。 |

## 每批共用的工程要求

- 允许修改 `backend/avatarweave_agent/memory/`、新增测试、公开 API/SDK/renderer 的对应适配、`scripts/memory-*.py` 和本目录记录；不修改受哈希保护的 provider。依赖更新必须进锁文件。原用户未提交文档保留。
- 普通来源和检索使用 dialogue，明确控制使用 control，复杂回顾使用 deep_recall，抽取/归档使用 maintenance。浏览器不能提交 lane、Scope、版本栅栏或放大预算。当前执行器只供基础控制/来源服务，**没有实现完整部署级四通道资源系统**。
- 公开边界只接受 04 模型；认证层构建 Scope。模型仅提议候选；引用在 SQL 授权域内校验。源码中的可信接口不等同可公开匿名 RPC。
- 所有模型/embedding/对象 I/O 在事务外；短源/控制事务可锁账号计数行，事实提交只锁必要事实键并排序；不在整个推理期间锁住 memory_revision。
- deadline 用单调计时执行，UTC 用于审计；父取消传入下游。当前短服务由 asyncio.timeout 取消并等待会话清理；后续不能强制停止的 CPU 推理单独计有限 detached 槽，不能套用短 I/O 的释放语义。
- outbox 在同一事务入库；容量耗尽拒绝保存且完整回滚。任务必须校验 source version、privacy epoch 和目标事实版本。向量未就绪仍可从合法来源/覆盖层读取，不能误报事实已形成。
- 每批保留成功语义、具体失败案例和实际检查。功能旗标关闭即可回退流量；保留新增表，不通过删新表回滚，不复活已删除内容。生产迁移/索引切换/部署另走明确实施步骤。

## 本批实现说明

代码位于 `backend/avatarweave_agent/memory/`：

- `contracts.py`：设计附件 00—05 的完整类型，少量仅等价的格式整理；尚未加进服务的模型仅是契约。
- `storage.py`：独立 additive schema v1、账号/登录/会话/来源修订/引用 outbox/控制幂等凭据；与旧历史隔离。每进程连接池上限 2、overflow 0、池等待 100 ms；PostgreSQL lock/statement timeout 起点 50/150 ms。没有据此声称吞吐已通过。
- `identity.py`：Authlib Authorization Code + S256 PKCE，issuer/audience/nonce/state/签名/时效由库与边界校验；账号仅映射 issuer/sub。浏览器使用随机 opaque 登录 cookie，库中只存随机 token 的 SHA-256，不持久第三方 access/id token。
- `service.py`：来源持久化与队列同事务；来源新修订使旧源不可读取、旧任务 obsolete；连续水位取首个未完成洞；精确源读取检查当前版本、SQL 作用域、epoch 与 TTL；原文过期只读元数据，显式批次清理正文。尚无事实/摘要/向量，因此不能用这段来源修订替代 P2 的派生失效实现。
- `execution.py`：进程内最多 8 个基础调用、每用户 2 个、无等待队列；超额明确 overloaded；异步取消与事务上下文清理；执行报告不含正文。跨进程公平/供应商配额属于 VM-08，不冒充已经实现。
- `http.py`：账号、初始偏好、受控会话和处理状态的公开投影；64 KiB 正文上限、Origin+CSRF、严格 JSON、幂等、no-store 和 OpenAPI 请求 schema。尚未开放记忆 CRUD 或带正文的浏览器输入 RPC。
- `api.py`/`worker.py`：预留 `vm-` 私人房间，旧 demo Token API 拒绝加入，旧 Worker 在连接/读取历史前拒绝启动私人语音。PublicSessionView.rtc 为 null，等待 VM-07/10 验证后才开放，避免把来源框架误当可用语音记忆。

原文默认 90 天。`expire_raw_batch` 只是有界清理原语，尚未自动调度；未接任务进程前不开放真实私人输入。来源/索引状态分开显示，不返回完整提示、Scope、对象路径或私有执行 handle。

## 身份与基础设施决策

- 用户尚未选择身份提供方。当前使用标准 OIDC 适配层，生产 `MEMORY_ENABLED=false`；无需阻塞代码和隔离测试。真实厂商配置、账号恢复策略、公开 HTTPS 回调和密钥保管待上线前落实。
- 已在用户提供的 PostgreSQL 实例上创建全新开发库 `avatarweave_memory_dev`，确认并启用 pgvector **0.8.1**，初始化本批 `vm_*` 表。连接信息只保存在被 Git 忽略的 `.env`；没有改旧业务库。当前应用未切流或重启。
- 回归使用独立本机容器 `avatarweave-memory-test`（仅绑定 `127.0.0.1:55436`），绝不把开发/生产库设为 POSTGRES_TEST_URL。测试会删除自己的 `vm_*` 测试表。
- `/data/luwei/rag-zeroai/app/services/model_manager.py` 采用 OpenAI 兼容 embeddings。该项目 `.env` 提供 endpoint/key，但没有显式 embedding 模型配置；`config.py` 默认 DashScope text-embedding-v4/1536，部分注释仍提 v3。VM-06 必须显式冻结真正 model/profile/dimensions、响应 usage 与批量上限，逐返回校验维度/有限值/非零范数，不复制占位模型名。**本批未调用 embedding 服务、未复制其密钥、未创建向量索引。**

本地检查版本：LiveKit Agents 1.8.9、Pydantic 2.14.1、SQLAlchemy 2.0.55、asyncpg 0.32.0、Authlib 1.9.3，依赖以 `backend/uv.lock` 为准。

## 验证与未完成关口

检查命令：

```bash
npm run check
backend/.venv/bin/python -m pytest backend/tests -q
backend/.venv/bin/ruff check backend/avatarweave_agent/memory backend/tests/test_memory* scripts/memory-schema.py
backend/.venv/bin/python scripts/config-schema.py --check
backend/.venv/bin/python scripts/check-config.py
# 使用独立测试库再执行同组后端测试：
TEST_DATABASE=postgres POSTGRES_TEST_URL=postgresql+asyncpg://.../isolated_test_db backend/.venv/bin/python -m pytest backend/tests -q
```

迁移命令 `backend/.venv/bin/python scripts/memory-schema.py` 默认只检查；`--apply` 才建表。必须显式设置 MEMORY_DATABASE_URL，不从旧 DATABASE_URL 猜测；API/Worker 启动不会自动迁移记忆表，未知 schema 拒绝启动。

基础回归覆盖账号映射、跨账号/companion/workspace 拒读、同域幂等、来源新修订、旧任务失效、无记忆模式无源/任务、未最终 ASR 拒绝、epoch/owner 栅栏、租约重领、队列拒绝回滚、连续水位、跨会话 provisional、TTL 后证据降级、取消/超时释放。OIDC 使用隔离的模拟服务与真实 RSA 签名，覆盖正常 Code+PKCE 及错误 nonce/issuer/audience/期限/state；这不是已接入真实厂商的声明。

本批最终检查结果：

| 检查 | 实际结果 |
| --- | --- |
| 后端全量，隔离 SQLite | 66 passed；含本批新增的 31 项回归；现有 provider 的 11 条弃用警告未修改。 |
| 后端全量，隔离 PostgreSQL | 66 passed，同一组测试；没有使用开发库运行破坏性测试。 |
| 契约一致性 | 166 个模型的 JSON Schema 与设计附件一致，计入上述回归。 |
| npm run check | 构建、类型检查和 17 项 SDK 测试通过；现有 bundle 大小提示仍在。 |
| Ruff、git diff --check | 本批 Python 文件与补丁检查通过。 |
| config-schema.py --check / check-config.py | 通过；现有 provider 配置未改。 |
| 开发库基础设施 | 新库及 pgvector 0.8.1、memory schema v1 实际验证；应用开关关闭。 |

SQLite 的异步线程在受限沙箱中等待，已停止这两次挂起进程；最终 SQLite 和 PostgreSQL 回归均在获准的沙箱外执行。临时 PostgreSQL 测试容器在回归后停止，开发库保留。

未完成：真实登录厂商、完整 P1 身份/输入桥、P2 事实和控制 UI、P3 检索/上下文、P4 后台/冷热、P5 实际媒体账本、P6 十年/恢复/联合验收和 P7 灰度。G01—G13 均未整体测量；无任何 L1/L2/L3 容量或十年记忆质量通过声明。
