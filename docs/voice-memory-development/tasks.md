# 语音记忆开发任务与交付记录

更新日期：2026-11-05。设计基线为[实施方案 v0.3](../voice-memory-implementation-plan.md)、[算法](../voice-memory-design/algorithms.md)、[运行算法](../voice-memory-design/runtime-and-longevity.md)、[联合验收](../voice-memory-design/engineering-slo.md)及 00—05 契约。用户已授权实现并要求持续推进；原设计“尚未启动实现”是历史状态。本记录不改变原验收门槛。

本次从 P0/P1 来源基础继续实现了事实、控制、检索、上下文、私人语音接入、后台任务、长期操作及前端管理闭环；随后补上增量维护、人物称呼索引、反思审核、完整来源图恢复和部署脚本。**代码与开发环境验证不能代替 P0—P7 的生产联合验收。** 操作见[运行与验证说明](runtime.md)。

## 任务安排与实际状态

单执行者按依赖顺序推进，未启动子代理。内部模块使用原 BaseModel 与 ModuleResult；新增 HTTP DTO、媒体握手单独记录，未改写原 166 个模型。

| 任务 | P/M/T；输入→输出 | 本次代码及边界 |
| --- | --- | --- |
| VM-01 | P0；00—05 Contract；T01/T09 | 166 模型已移植并逐项比对原 JSON Schema；严格类型、未知版本、水位及结果形状有回归。 |
| VM-02 | P0；CapacityManifest/BudgetProfile/JointAcceptanceReport；T07/T10 | 有界执行器、独立进程、真实模型/媒体 hooks 检查、联合报告验证器已实现。供应商总额度、生产进程清单、held-out 和容量档未冻结，不声明 L1/L2/L3 通过。 |
| VM-03 | P1/M22/M23/M26；Identity/Session/InputEvent/VisibilityRead/Repository 的 Input→Output；T01/T09 | OIDC+PKCE、稳定账号、恢复/租约、来源修订、同事务 outbox、TTL、水位、真实 Worker 文字/最终 ASR/开场桥已接。无记忆会话不存正文/派生；旧历史不归属私人账号。真实身份商及账号恢复待配置。 |
| VM-04 | P1/P2/M01—M05/M26；Admission/Entity/Temporal/Conflict/Commit 的 Input→Output；T02/T06/T09 | 谓词 registry、实体匹配、逐字区间与语境、值/条件核验、双时间版本、胶囊、确定性冲突、事实键锁、纠正 suppression、反向失效已实现。人物新增/改名/别名/删除有明确确认与版本审计；确认人物后有界重试原始来源，不提高事实权威。同名保持歧义，唯一称呼才绑定；尚未提供实体合并/拆分操作。 |
| VM-05 | P2/M19；PrivacyInput→PrivacyOutput、公开记忆模型；T06/T09 | 列表、证据、时间线、pin、原话修订、事实/自然语言纠正、删除进度、偏好、账号屏障及页面已接。先撤可见性及取消，再分批清理。备份清单未验证时保持 cleaning。 |
| VM-06 | P3/M11/M12/M24；Query/Embedding/Retrieval 的 Input→Output；T02/T03/T10 | 精确/词法/实体/时间与 pgvector 路由、query 合批、RRF、历史去近期偏置、模型空间校验已实现。已明确的人物在词法、时间及向量 SQL 路线均先过滤主语，不混入其他人物的同类事实。语义超时明确 partial。当前是有界精确 pgvector 查询，未验证十年规模性能。 |
| VM-07 | P3/M13—M15/M23；Context/Persona/OpenLoop/ModelTask 的 Input→Output；T02/T07/T09 | 逐轮预算/Manifest、不可信数据块、角色/短期语气、来源 overlay、事项保存/编辑及语音跟进已接。全局及单项双重许可、到期、非敏感有效事实、每轮最多一项、并发共享 72 小时尝试冷却；取消后旧生成失效。普通路径无额外查询/语气模型，私人预生成关闭，不自动推送。 |
| VM-08 | P4/M17；JobInput→JobOutput；T06/T10 | 独立 Worker、dispatcher、持久 typed payload、claim/renew/complete/fail、有限抖动退避/租约、公平准入已实现。Outbox 与 Job 共同计入数量/字节/年龄上限，已分发抽取不重复计数。有持久游标的增量维护调度，自动补缺向量、核对来源索引水位、分批规划摘要/分层/反思及抽样验证归档；重启可继续。 |
| VM-09 | P4/M06—M10/M18；Tiering/Compression/Reflection/Decay/Forget/Archive 的 Input→Output；T03—T05 | hot/warm 位置元数据可执行更新，cold 必须完成加密写入/回读/校验再标记；位置变化不制造事实版本。摘要保存来源 DAG 且 mandatory coverage=100%。反思持久为待确认假设，用户拒绝或以自己的表述确认；确认走新的合法用户来源，旧支持失效后禁止确认。保留在线原子事实，不自动依据衰减删除人生事实。 |
| VM-10 | P5/M16；OutputInput→OutputOutput；T07 | v2 动作、nonce 握手、能力协商、账本、renderer 实际开始/结束/取消、owner/generation/读集合栅栏已接。旧 SpeechHandle 不采用新回合状态；重连不重放。TTS 帧不冒充设备已播放。 |
| VM-11 | P6/M19/M20/M25；Privacy/Migration/Export 的 Input→Output；T06/T08 | v3 开放携带包包含历史事实版本、来源修订/原有 TTL、证据、会话、人物、摘要 DAG、事项、批准角色引用及反思。逐类清单计数/checksum、作用域、环、引用、纠正/删除及历史映射均预检；同事务重建并幂等，保留原权威，不激活导入角色或自动允许跟进。v2 只读并要求人工来源审查。当前单包各类最多 1000 对象、明文最多 8 MiB；超限明确拒绝，自动多分片携带尚未实现。 |
| VM-12 | P6/P7/M21；EvaluationInput→EvaluationOutput、JointAcceptanceReport；T01—T10 | 独立观测评分、案例摘要、失败/超时保留分母、联合门槛验证及开发故障回归已实现。100 个相对年参数案例只是解析器回归；未做 ≥1000 held-out、关键组各 ≥100、三次固定负载、设备播放和用户灰度。 |
| VM-13 | 部署交付；配置→迁移→进程管理 | `scripts/deploy-memory.sh` 复用原安装脚本，提供 up/install/init/migrate/start/restart/stop/status/check/credentials。受保护部署配置与根 `.env` 分离；可选本机 Docker OIDC 或外部 OIDC；API、Agent、记忆 Worker、前端统一管理，Worker 有启动健康状态。配置幂等、权限、HTTPS 跳转及 Compose 展开已验证。用户部署后补齐 `/api/` 的代理来源头，修复自重定向；真实 Nginx HTTP/HTTPS 回归、真实浏览器 OIDC 登录/退出通过。 |

## 关键实现决定

1. **隔离**：仅信 OIDC issuer/sub 与服务端 Scope；昵称、room identity、共享 key 不能认领记忆。跨 companion 共享当前拒绝。OIDC 不绑定厂商，尚未选厂商不阻塞开发。
2. **提交**：专属库 additive `vm_*` schema v3；显式升级回填可重建的人物别名投影。旧 Conversation/Message 不迁移归属。事实键排序锁及短事务；模型/向量/对象 I/O 在事务外。原文默认 90 天；来源/结构化/索引分别报告。数据库版本与携带包版本分别管理。
3. **证据**：最终合法用户来源、逐字区间、值及比较条件均需服务验证。唯一逐字 quote 可对齐 Unicode 并扩为完整分句，不模糊补字；展示由注册谓词和值构造，不采信模型额外陈述。旧源不能重抽取覆盖纠正。
4. **栅栏**：memory_revision 是水位；输出校验实际事实/来源读集合。无关新增不强制打断，相关纠正/删除/隐私变化立即失效。每个 SpeechHandle 捕获 generation/OutputPlan，晚到 TTS/工具不读下一回合全局状态。
5. **资源**：每进程 DB 池 2/overflow 0/等待 100ms；PG lock/statement 初值 50/150ms。执行器总 8，control/dialogue/deep_recall/maintenance 为 2/4/1/1，逐用户为 1/2/1/1，无无界等待。物理对象线程限 2，取消后占位至真正结束并报告 detached_bounded。跨进程额度需真实 CapacityManifest 约束。
6. **删除**：先逻辑屏障，后分批物理清理；对象 staged intent 先提交，文件锁及持久撤销标记防延迟 writer 复活。签名 SQLite 删除日志独立于 PG 快照，但同机开发文件不等于生产独立备份。无实际备份盘点不标物理完成。
7. **维护**：分层/遗忘不自动改变真值；在线原子事实保留，归档返回 `may_remove_online_copy=False`。摘要/反思不替代原子证据。调度器自动补建当前 profile 的缺失向量；来源贡献的所有当前事实完成索引后才推进 settled 水位。替换 profile 仍需显式 migration/reindex 与切换审核。
8. **媒体**：仅报告实际可观测粒度；浏览器当前无设备音频回执。Live2D completion 来自有限动作真实 motionFinish；发送成功/函数返回/first frame 不是 completion。

## 数据库与模型环境

- 用户提供的 PG 上已创建专用 `avatarweave_memory_dev`，pgvector **0.8.1**；本次显式迁移并验证 **schema v3**，未迁移旧业务库。启动不隐式 DDL。
- 破坏性回归只用本机 `avatarweave-memory-test` 的 `127.0.0.1:55436/avatar_memory_test`，不对开发库 drop_all。
- embedding 通过 `MEMORY_EMBEDDING_REFERENCE_ENV` 引用 `/data/luwei/rag-zeroai` 受保护配置。真实 `qwen3.7-text-embedding-flash` 返回 **1024** 维（请求 1536 也返回 1024），显式 profile 因此固定 1024，不接受漂移。该候选 profile 不证明供应商权重不可变。
- 开发对象密钥已生成，文件 0600、目录 0700；`.env`/`.runtime` 被忽略，不提交密码/原文/导出包。依赖锁定；受保护 provider 未修改。私人 SDK 录制项全部显式关闭，避免音频/日志/trace 旁路保留。

## 本次验证证据

| 检查 | 2026-11-04 实际结果 |
| --- | --- |
| 后端全量 SQLite | **214 passed**，现有 provider 11 条弃用警告。 |
| 后端全量独立 PostgreSQL/pgvector | **214 passed**，开发库不参与破坏性测试。 |
| 原始契约比对 | 原 166 模型 JSON Schema 与附件一致，计入后端回归。 |
| `npm run check` | 工作区构建、类型检查、**24 项测试通过**；现有 bundle 大小提示。 |
| 浏览器 | fixture HTTP 驱动事实纠正/删除、人物新增/改名/删除、事项、推测确认和导出界面；Vite 阻止读取 `.env` 与运行时合成凭据文件；**真实 Live2D** started→completed / started→cancelled。不是实际 OIDC 或设备音频验收。 |
| 真实模型链路 | 两条合成事实经真实抽取及 1024 维 embedding，在新会话实际回复命中姓名/偏好；TTS 捕获 **97344 samples**，最终复测首帧 **2107.65ms**。不是容量、设备播放或质量通过声明。 |
| 代码/配置 | Ruff、diff 检查、config-schema/check-config 通过。 |
| 开发库迁移/任务入口 | `memory-schema.py --apply` 返回 `Memory schema v3 verified`；`memory-worker.py --once --health-file ...` 完成真实维护检查并输出受保护的健康状态；未切流/重启。 |
| 部署脚本 | Shell 语法、帮助、配置初始化幂等、凭据权限、登录服务切换、配置失败不写文件、Compose config 校验通过。仅检查临时目录；没有运行安装/发布/认证容器。 |
| 部署后登录修复 | 公网入口由自跳转 303 恢复为跳向认证端点的 302；discovery 200。真实浏览器使用开发账号登录后为 account、CSRF 退出后为 anonymous；TLS 校验开启。新增真实 Nginx HTTP/HTTPS 转发回归，临时配置/端口独立于业务 Nginx。 |
| 2026-11-05 账号 RTC 修复 | 先连接房间再初始化绑定用户的语音输出，初始化结束才确认就绪。客户端等待匹配的握手、有限重试、20 秒超时断开；重复 hello 不清空在途动作。独立临时账号在真实 HTTPS 页面通过勾选记忆、文字回复、合成麦克风转写、语音回复及非静音音频解码，随后撤销测试账号。最新 SQLite 全量 **217 passed**、前端 **26 passed**；PG 全量 214 为上一轮证据，本轮真实 RTC 使用 PG 开发库的独立合成账号。 |
| 2026-11-05 会话恢复与配额修复 | 恢复请求补齐 last_event_sequence；失败不丢原引用，新建重试复用幂等键。两场上限按有效租约/两分钟预留计算，过期会话不再长期占位；恢复和 Worker 认领也受同一上限约束。刷新保留语音 RoomIO，离线宽限到期释放 Worker；失效传输恢复时重建房间，保留 session ID/来源。最新 SQLite 与 PG 全量各 **222 passed**、前端 **26 passed**，浏览器 fixture 覆盖恢复、校验失败保留引用及新建重试。SQLite 并行运行曾有一项读取隔离断言失败，单项及串行全量复测通过，未更改隔离检查。 |
| 会话恢复现场复测 | 独立合成账号预置两个一小时前已过租约的 active 行，在实际 HTTPS 页面通过新建记忆会话、文字回复、合成麦克风转写/语音回复；刷新后 `/resume` 返回 200，保持相同 session ID，继续收到“恢复正常”回复。修复已更新前端并重启本项目应用，测试账号及凭据随后撤销，用户记忆未删除。 |
| 2026-11-05 会议补充与姓名连续性 | 新增中文时刻/范围解析，同一会话唯一相符会议的时间细化保留 ID、来源及系统历史；明确新增、不同日期/主题和冲突保持分开或确认。点事件在发生前后均能召回。普通新会话有界加入唯一有效本人姓名，上下文明确主语/谓词；完整自我介绍采用确定性提取。模型结构/证据错误最多修复一次，不放宽逐字校验。SQLite 串行全量与 PG 全量各 **265 passed**，原 166 模型不变，Ruff/config-schema/check-config/diff 通过。本轮未改前端构建；前端 **26 passed** 为前一轮结果。 |
| 并发写入与实际页面 | 明确写入语句减少不必要语义检索，会议更新不引用即将替换的旧事件版本；上下文按实际引用校验，保留 300ms 准备上限。隔离 PG 的真实并发抽取/LLM/TTS 得到实际回复、**67392 samples** 且无 failed 通知。真实 HTTPS/RTC 浏览器通过姓名保存及实际回复、两次会议输入的同编号/新版本/明确时刻；轮换同一合成账号的临时登录 cookie 后，全新浏览器再次回答姓名。此测试不等于真实 OIDC 提供商流程或设备听见验收。 |
| 原账号有限修复 | 仅重试两条已定位、未撤销且原文仍合法的失败来源；漏掉的会议时间恢复并经正常 M04/M05 提交合并，保留一条当前事件、原编号、两份原话和旧版本。原姓名仍有效；有歧义的后续姓名表达保持待确认，未直接覆盖。旧失败任务保留审计，未清空用户数据。 |
| 最终语音与清理 | 同一修复版本再通过真实 HTTPS 私人 RTC 的文字回复、合成麦克风转写/语音回复、非静音音频解码、刷新后同 session 恢复。合成账号/临时 cookie/音频随后撤销，专用测试 PG 停止；API、Agent、记忆 Worker、前端及 LiveKit 保持运行。 |
| 2026-11-05 账号连接问候修复 | 现场只读确认恢复中的账号会话 owner_epoch=3，原先仅 owner_epoch=1 触发的问候被跳过。改为语音就绪且成功应答新 nonce 后问候，重复握手不重复；发送应答失败可重试。每次问候使用独立 SpeechHandle turn ID、空对话及无工具调用，不重放私人历史；旧失效检查不能打断后继语音，用户先开始新一轮则跳过迟到问候。SQLite 与独立 PG 全量各 **270 passed**；真实 HTTPS 分别通过新会话、预置过期 Worker 的旧会话恢复、两种会话的刷新问候，以及文字/麦克风/语音和非静音音频解码。修复已重启生效，未更改前端或删除用户记忆。 |

故障回归包含越权、签名/CSRF、TTL/来源修订、否定语境、A→B→A、纠正 suppression、重复确认/任务重试、旧租约、取消后对象写、导出/删除日志篡改、恢复漏账、账号分批擦除、晚到 TTS、重连 nonce/序号下限、renderer 取消、事项双重授权/并发冷却/取消读集合。

线程/网络/浏览器检查按环境权限在获准的沙箱外执行。临时服务和测试容器验证后停止，不停止既有业务进程。

本轮 SQLite 在全量 PG 回归及服务重启并发时，一项既有实体用例在创建测试会话阶段触发 deadline_exceeded；没有修改该用例或放宽时限，随后串行全量 **265 passed**。这属于需要持续观察的环境时限波动，不作为联合负载通过证据。

## 发布关口与回退

根 `.env` 的记忆开关默认关闭；用户部署配置可明确开启开发体验。回退撤私人流量，保留表/删除日志，不通过删表或复活内容回滚。开发环境登录修复已平滑重载 Nginx；未执行生产索引切换或账号迁移。

尚有实现边界：实体合并/拆分；超出当前上限的多分片携带；回收在线大正文/向量后的冷目录切换。现有实现保留数据、显式拒绝超限或要求消歧，不以静默丢弃规避这些边界。因而不能把“模块入口都有代码”写成“完整十年方案全部开发验收完成”。

待落实及独立验收：正式 OIDC/账号恢复（开发 OIDC/HTTPS 已联调）；生产对象/密钥托管及独立备份删除清单；供应商/进程额度；隔离备份恢复演练；held-out 与压缩人审；固定档三次联合负载；设备音频与 P7 用户灰度。缺失项不填 passed；G01—G13 无整体通过声明。
