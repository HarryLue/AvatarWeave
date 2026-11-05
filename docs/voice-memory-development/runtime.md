# 记忆模块运行与验证

对应[交付记录](tasks.md)，原设计及 166 个模型保持不变。根 `.env` 默认不开放长期记忆或私人 RTC；下面明确区分部署命令和独立验证命令。2026-11-04 修复 Nginx 登录重定向循环并验证真实 OIDC 登录/退出；2026-11-05 修复账号 RTC 初始化顺序、助手就绪握手和连接超时，已更新部署前端并重启本项目应用。

## 第一次体验与面板功能

登录账号，开启“允许保存长期记忆”，再勾选“本次对话使用记忆”，点击“开始账号对话”。全局设置允许保存，本次开关决定这一场是否读取和形成长期记忆；关闭本次开关不会删除已经保存的记忆。需要临时聊天时可以不勾选。先等助手就绪，再发送文字或开启麦克风。

| 面板入口 | 用途 |
| --- | --- |
| 事实与偏好 | 查看整理出的信息，如称呼、口味；可置顶、纠正、删除和查看来源。并非每句聊天都会变成长期记忆。 |
| 重要人物 | 确认人物姓名和日常称呼，例如“妈妈”；给同名的人不同称呼，减少记错对象。 |
| 待确认推测 | 审核系统根据多次交流提出的推测。可拒绝，或用自己的话确认希望保存的偏好/边界。 |
| 人生时间线 | 按时间回顾经历和过去阶段，例如搬家前后的生活信息。 |
| 未结束的事项 | 管理面试、计划等尚待结果的事情，可完成/取消，并单独选择是否允许聊天时问起。 |
| 原话与纠正 | 查看本次账号对话仍在保留期内的原话；语音转写有误时可修正，并使依赖旧原话的内容重新处理。 |
| 导出与恢复 | 下载自己的记忆文件；恢复时先检查再确认，不能用旧文件绕过删除或纠正。 |

“允许长期保留必要的来源片段”控制支持记忆的最少原话证据；“完整转写保留天数”控制完整聊天原文的保留期。完整原文到期与删除结构化记忆是不同操作。“允许在对话中关心未结束的事项”还需要单项事项许可，作用于正在进行的聊天，不是离线通知或自动推送。

账号 RTC 连接默认最多等待 20 秒的助手就绪确认，期间文字和麦克风不开放；超时会断开并提示重试。重连使用新 nonce，握手丢包有限重试；后台连接房间后才初始化绑定用户的语音输出，语音初始化完成才发送就绪确认。

会话数量上限仍为每账号两场，按有效 Worker 租约或两分钟连接预留计算。过去连接失败但已过租约的历史会话不再占名额，也不会因此删除原话或记忆。新建、恢复和延迟 Worker 认领都检查同一配额，防止恢复旧会话绕过并发上限。刷新恢复请求补齐严格契约要求的 `last_event_sequence`；校验/临时失败保留原会话引用，不再悄悄新建另一场。新建请求重试复用幂等键，结束请求确认成功后才移除本地引用。

短暂刷新保留私人 RoomIO，绑定用户离线超过约一分钟则取消旧生成并结束 Worker、释放租约。长时间离线或 Worker 已失效时，恢复保留原 session ID 及记忆归属，生成新的房间和 participant，避免返回没有有效 dispatch 的旧房间；旧房间令牌无法认领新传输。真正达到两场有效会话时，页面明确提示先结束一场。

此次启动故障源于：显式绑定 participant 的 LiveKit RoomIO 在初始化转写输出时会访问 `room.local_participant`，需要先 `ctx.connect()`；此前账号路径顺序相反，Worker 退出而浏览器误把房间连接当成助手就绪。`start_private_session` 固定此顺序，并保留所有私人录制开关关闭。重复 hello 保留同一连接的序号下限及正在执行的动作，不把握手重试当成一次重连。

2026-11-05 使用独立临时测试账号完成了实际 HTTPS 页面验证：勾选记忆、文字回复、合成麦克风输入转写、语音回复及浏览器非静音音频解码。测试未读取或修改用户的偏好/记忆，结束后通过账号删除屏障撤销测试数据和凭据；不以音频解码证明真实设备已播放或用户已听见。后端 SQLite 全量 217 项、前端 26 项通过；新增启动/提前 hello/重复 hello/超时回归。实际 RTC 检查脚本为 `scripts/browser-memory-rtc-smoke.mjs`，需要运维预先准备受保护的独立合成账号 fixture，不使用真实用户账号做写入测试。

## 一键部署与启动

已有真实界面：登录与记忆选择、事实/偏好、重要人物、待确认推测、人生时间线、事项、原话纠正、导出/恢复，以及账号语音入口。入口在原 demo 页面，部署后访问配置的 HTTPS 地址。

先在 `.env` 配置独立的 `MEMORY_DATABASE_URL`、真实 ASR/LLM/TTS 及 embedding；`infra/https.json` 配置浏览器访问的 IP 或域名。`--origin` 必须与实际 HTTPS 根地址、证书和反向代理一致，支持 IP；未另配 origin 时默认取该文件的首个 host。当前为 `https://111.56.189.4`，没有域名也可以使用。脚本不自动改写其他地址的证书或创建远程数据库。已有专用开发数据库可直接使用，不要填旧历史业务库。

尚未选择登录厂商时，可用 Docker 运行独立的开发 OIDC。服务器需已安装 Docker Engine 和 Compose，能拉取镜像：

```bash
# 安装依赖、构建/发布前端和 Nginx、生成受保护配置、显式迁移、启动全部进程
bash scripts/deploy-memory.sh up --local-auth --origin https://111.56.189.4

# 查看本机开发登录账号和随机密码（只有此命令会显示密码）
bash scripts/deploy-memory.sh credentials
```

`up`/`install` 会调用原 `scripts/deploy.sh install`，更新 `/var/www/avatarweave` 和 Nginx 配置，并按 `infra/https.json` 配置 HTTPS；应在准备好发布的服务器执行。脚本以前台方式持续管理进程，终端需保持运行，或由已有进程管理器托管。已部署环境可只执行配置、迁移与启动：

```bash
bash scripts/deploy-memory.sh init --local-auth --origin https://你的域名
bash scripts/deploy-memory.sh migrate
bash scripts/deploy-memory.sh start
```

已有外部 OIDC 时，在 `.env` 填 `MEMORY_OIDC_ISSUER`、`MEMORY_OIDC_CLIENT_ID` 和需要时的 `MEMORY_OIDC_CLIENT_SECRET`，将上述 `--local-auth` 换为 `--external-auth`。客户端回调为 `https://你的域名/api/v2/auth/callback`，启用 Authorization Code + PKCE S256。不要复用公开 demo 的共享 access key 作为账号身份。

```bash
bash scripts/deploy-memory.sh status
bash scripts/deploy-memory.sh check           # 配置、schema、密钥、provider/profile
bash scripts/deploy-memory.sh check --online  # 另验证 HTTPS OIDC discovery/PKCE
bash scripts/deploy-memory.sh restart
bash scripts/deploy-memory.sh stop
```

配置保存于 `.runtime/memory-deployment.env`，权限 0600；重复初始化保留随机 session secret、开发登录凭据和数据，根 `.env` 不改写。此配置只对记忆部署启动生效。停止只处理当前 checkout 管理的进程及认证容器，不删数据库、对象、密钥或认证卷。从本地认证切换外部认证后，下一次 start/restart/stop 会停止原本地认证容器，保留其卷。

可选 Keycloak 使用固定版本和镜像 digest、独立开发 realm、随机密码、无公开注册，端口只绑定 `127.0.0.1:18882`；Nginx `/idp/` 转发。该配置用于单机开发体验；正式账号恢复、认证数据库、备份与运维应使用审定的身份服务。实现依据为 [Keycloak 容器说明](https://www.keycloak.org/server/containers)、[hostname](https://www.keycloak.org/server/hostname) 和[反向代理说明](https://www.keycloak.org/server/reverseproxy)。自签证书环境需给浏览器及服务配置受信任 CA，不关闭 OIDC TLS 校验。

已验证配置初始化/权限/幂等、认证切换、Compose 展开、各进程入口和 UI，以及用户部署后的公网 HTTPS/OIDC 真实登录与退出。`check` 输出 `configuration_ready` 不代表生产联合验收通过。

如果登录出现同一 URL 不断返回 303，先检查 Nginx `/api/` 的 Host 和 `X-Forwarded-Proto` 转发。[Nginx 的继承规则](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header)规定：location 内只要出现一条 `proxy_set_header`，就不再继承上一层的整组头配置。仓库路由已在 `/api/` 中显式设置完整来源头；`X-Forwarded-Proto` 由 `$scheme` 覆写。不能以删除 HTTPS 校验或信任客户端伪造的协议头来消除循环。

实际开发登录回归可在已运行的本地认证部署上执行：

```bash
node scripts/browser-memory-login-smoke.mjs
```

该脚本读取受保护的开发凭据，真实验证登录页、OIDC 回调、账号状态、CSRF 退出及退出后匿名状态，不发送对话或修改记忆，也不输出密码、Cookie 或回调参数。`backend/tests/test_nginx_proxy_origin.py` 另外用临时 HTTP/HTTPS Nginx 验证代理头继承与协议覆写；CI 在已有 Nginx 的 bootstrap 阶段执行该回归。

## 配置与启动

依赖由 `backend/uv.lock`、`package-lock.json` 锁定；Python 使用 `backend/.venv/bin/python`，JS 使用 Node 22+。`.env.example` 列出 `MEMORY_*` 参数；实际 `.env` 被忽略，不把凭据写入文档/命令历史。

为 `MEMORY_DATABASE_URL` 配置新的 PostgreSQL asyncpg 数据库及 pgvector。禁止使用旧业务库或把开发库当测试库。迁移必须显式执行；API/Agent 启动只检查 schema。

```bash
backend/.venv/bin/python scripts/memory-schema.py --apply
backend/.venv/bin/python scripts/memory-admin.py init-store
backend/.venv/bin/python scripts/memory-worker.py --once
```

不带 `--apply` 只检查版本。开发对象默认初始化于 `.runtime/memory/objects`、`.runtime/memory/keys`，分别配置 `MEMORY_ARCHIVE_ROOT`、`MEMORY_ARCHIVE_KEY_ROOT` 和 key version。密钥必须独立保管，遗失无法解密。`MEMORY_DELETION_JOURNAL_PATH` 应位于不随 SQL 快照回滚的日志存储；同一开发盘不是生产独立备份。

`memory-worker.py` 不带 `--once` 时持续同步删除日志、分批删除、执行任务、清理到期原文/会话/导出，并通过持久游标调度缺向量补建、摘要、分层、反思和归档抽样校验。它与 API、Agent 分进程运行；控制/语音不等待抽取完成。`scripts/dev.py` 在受保护配置启用记忆时自动拉起该进程，检查 PID/时间匹配的健康状态后才认为启动成功。

账号入口需 HTTPS 的 `MEMORY_PUBLIC_ORIGIN`、OIDC issuer/client ID、随机 session secret；回调为 `/api/v2/auth/callback`。HTTP 或其他主机的登录入口会先跳转到配置的 HTTPS 地址，再创建 Secure 状态 cookie。未选正式厂商时可使用上述隔离开发 OIDC，根旗标保持关闭。私人 RTC 还受 `MEMORY_RTC_ENABLED` 控制；开发部署配置可明确开启体验，正式流量开放仍需真实账号与媒体验收。

embedding 明确 model/revision/dimensions，可独立配置 endpoint/key，或用 `MEMORY_EMBEDDING_REFERENCE_ENV` 引用受保护配置文件。当前实测候选 qwen3.7-text-embedding-flash/1024；不要猜默认维度。新旧 profile/index generation 不混算。故障时精确/词法路线保留，状态不会声称索引已完成。

## 对外接口与补充协议

`/api/v2` 由登录 cookie 确定账号，写操作要求 Origin+CSRF；浏览器不提交 Scope、owner/privacy epoch、lane、对象路径或 execution handle。私人房间 token 仅通过受控 session 返回。公开响应 no-store、严格 DTO、分页及正文有界。

| 接口（省略 `/api/v2`） | 用途 |
| --- | --- |
| `GET/PUT /memory-preferences`，`POST /sessions` 及 resume/end | 记忆/保留/跟进选择、创建和恢复。 |
| `GET /sessions/{id}/memory-status` | 来源/结构化/索引状态。 |
| `GET /memories`、`GET /memories/{id}`、`GET /memories/{id}/evidence` | 事实与合法证据；原文到期不伪造逐字回放。 |
| `PATCH /memories/{id}`、`PATCH /memories/{id}/pin`、`DELETE /memories/{id}` | 纠正、固定、撤销与清理。 |
| `GET /deletion-jobs/{id}`、`GET /timeline` | 删除阶段和时间线。 |
| `GET /sessions/{id}/transcripts`、`PATCH /transcripts` | 合法原话和来源修订，旧派生失效。 |
| `GET /open-loops`、`PATCH /open-loops/{id}` | 显式编辑计划、结果与跟进许可。 |
| `GET/POST /entities`、`PATCH/DELETE /entities/{id}` | 明确确认人物、改名/别名、删除人物及相关来源。 |
| `GET /reflections`、`PATCH /reflections/{id}` | 查看待确认推测、拒绝或以自己的表述确认偏好/边界。 |
| `POST /exports`、`GET /exports/{id}/download` | 短期授权、签名开放 JSON；服务端对象加密。 |
| `POST /imports/upload`、`POST /imports/dry-run`、`POST /imports` | 验签、冲突/删除预检、明确确认后导入。 |

pin/原话、人物、反思 DTO 是公开边界补充，未修改原 166 模型。下载包含开放结构、可选合法原文及签名；当前接受可验证的本账号包。v3 携带包保存事实的系统历史版本、来源修订及原 TTL、证据、会话、人物、摘要 DAG、事项、批准角色引用和反思；逐类计数及 checksum 验证后 dry-run，apply 在同一事务重映射所有引用并幂等恢复。保留原断言权威，角色引用不覆盖活动角色，事项不自动恢复主动跟进。旧 v2 包只读，需人工来源审查，不能自动升级为确认事实。删除、纠正和以前上传产生的 ID 映射均参与防复活检查。

当前单个包每类最多 1000 个对象、明文最多 8 MiB；超过上限会明确失败，尚无自动多分片携带。完整图丢失/恢复已在 SQLite 与 PG 测试中验证，但不等于十年规模携带或生产备份恢复验收。

人物使用稳定 ID，称呼经 Unicode 规范化建立有作用域的别名索引。两个“小林”不会自动合并，使用“同事小林”等唯一称呼后才绑定。确认人物后可有界重试尚合法的原始来源，不把早先未确认的提议直接提升为事实。人物合并/拆分尚未提供。

反思只出现在“待确认推测”。确认时需填写自己希望记住的表述，服务器为此创建新的用户来源，再走正常提交；拒绝、重复确认及支持证据已失效均有检查。模型自己的概括不直接成为 `user_confirmed`。

语音事项跟进需要全局及单项同时允许，约定时间已过且关联事实仍有效、非敏感；每轮最多一项，先回应当前问题，不推断结果。为防并发重复，在模型调用前记录仅含 ID/时间的 72 小时尝试预留；失败/打断也保守冷却，不把预留记录当成已问出或已听见。取消或编辑事项会撤销引用旧版本的生成。

私人动作新增 `avatarweave.command.v2`、`avatarweave.receipt.v1`。每次连接/重连发 `avatarweave.hello.v1` 随机 nonce，服务端 `avatarweave.bootstrap.v1` 回显 nonce、owner、generation、sequence_floor。v2 命令绑定当前 connection_nonce；匹配握手前文字/麦克风不可进入私人会话，旧连接命令不重放。版本、有效期、去重、缓存和队列有界，公共 v1 保留。

账号对话在语音已就绪且成功发送连接握手应答后，为该 nonce 触发一次简短问候；恢复旧会话、新 Worker 接手或刷新复用旧 Worker 均适用，不再用 owner_epoch=1 限制。重复握手不重复问候，握手发送失败可在客户端重试成功后触发。问候使用独立 SpeechHandle 对应的 turn ID 和空对话上下文，不重放旧用户输入或私人历史；用户已先开始新一轮时跳过迟到问候。旧 generation 的延迟失效检查不会打断后继问候。

当前动作限经批准的有限静默 Live2D 动作；motionFinish 才能提交 completed。音频无设备播放回执时保持 published/unknown，不能把 TTS 帧或发送成功记为“用户已听见”。

## 会议补充与跨会话姓名

同一会话最近 30 分钟内只有一个相符会议时，“下午有个会议”之后补充“下午3.00的会议”或“下午三点有个会议”，会保留原 memory ID、生成新版本，并合并两次原话证据。面板文字包含时间；原始表达及其精度独立保留。“下午三点”“15:00”、半点、刻钟和明确范围可解析；没有提供结束时间时保存点事件，不杜撰会议时长。多个可匹配会议、冲突时刻或无法判断上午/下午时需确认；明确“另外、还有”等新增事件、不同日期或不同明确主题不强行合并。迟到的补充可细化粗略记录，但不能覆盖较新的明确改期。旧版形成的同一事件重复记录可通过正常 M04/M05 提交合并，保留原编号及所有历史，不能直接删表去重。

事件的发生时间不等于知识失效时间：当前正确版本的未来计划或过去事件，仍可按问题召回；姓名、现居城市等状态事实仍按有效期过滤。明确历史/时间查询继续使用对应时间范围。

普通记忆会话会有界读取同账号、同 companion 的唯一有效本人姓名，并在上下文标明 subject=user_self、predicate=name；仍是有证据的非可信指令数据，经过来源、撤销、敏感性和 token 预算检查。新登录或新 session 不会重置该姓名；第三人消歧与历史查询不额外注入本人画像，临时会话不读旧记忆。开场问候仍不主动读历史。语音提示只把本轮新原话标为待整理，不能据此否定已经提交的旧姓名。

完整、明确的简短中文自我介绍（如“请记住，我叫林小舟。”）采用确定性提取，仍经过正常准入、证据与冲突检查；问题、假设、第三人介绍与复合句交给常规抽取。模型的结构/注册字段或逐字证据不合法时最多修复一次，两次共用原 deadline，失败如实报告，不降低原文校验或伪造用户确认。真实不同姓名的冲突仍需确认，不自动覆盖原姓名。

明确自我介绍直接使用本轮原话组织回答，不额外等待历史语义检索。明确会议补充使用精确路线保留画像/边界，通过合法原话增量保留前文，不把即将替换的旧会议版本绑定为本轮回答依据。上下文按实际引用逐条验证；无关新提交不因全局 revision 变化而取消本轮，隐私变化、已引用事实失效和来源撤销仍阻断。准备时限仍为 300ms，没有靠延长超时解决“已保存但无回复”。

## 内部维护与评估

内部命令读取严格 BaseModel JSON，仍验证 SQL 作用域及 epoch。request.meta.operation_id 应稳定；输出权限 0600，不在终端打印正文。

```bash
backend/.venv/bin/python scripts/memory-admin.py run compression --input /tmp/compression-request.json --output /tmp/compression-result.json --enqueue
backend/.venv/bin/python scripts/memory-admin.py sync-deletion-journal
backend/.venv/bin/python scripts/memory-evaluate.py cases --request /tmp/evaluation-request.json --observations /tmp/independent-observations.json --output /tmp/evaluation-result.json
backend/.venv/bin/python scripts/memory-evaluate.py joint --report /tmp/joint-report.json --capacity /tmp/capacity.json --load /tmp/load.json --raw-request-count 10000 --dataset-question-count 1000
```

这些 JSON 是需另行准备的输入文件示例，不是已存在的真实通过报告。`memory-admin.py run --help` 列出模块。分层计划由调度器执行位置更新，cold 先写入/回读/验证再标记，不更改事实语义版本；遗忘保持受控计划，反思保持待确认假设。embedding migration 分批重建/校验，不自动切生产索引。归档当前保留在线原子事实，尚未实现回收在线副本后的冷目录切换。

恢复必须先核对独立删除日志、水位、对象校验及 key/schema/profile；缺日志或已删对象拒绝。实际备份清单未接入时不宣称物理删除完成。回退撤流量/新索引，保留新表及删除日志。

评估来自独立观测/裁决，冻结案例摘要，超时/失败/拒绝/缺失保留分母。joint 拒绝缺容量/负载、样本不足、未测 gate、非三次运行及安全失败。开发单测数不等于生活问答案例数。

## 验证

```bash
npm run check
backend/.venv/bin/python -m pytest backend/tests -q
backend/.venv/bin/ruff check backend/avatarweave_agent/memory backend/tests/test_memory* scripts/memory-*.py scripts/dev.py
bash -n scripts/deploy-memory.sh
backend/.venv/bin/python scripts/config-schema.py --check
backend/.venv/bin/python scripts/check-config.py
git diff --check
```

PG 回归设置 `TEST_DATABASE=postgres`、`POSTGRES_TEST_URL` 指向独立可销毁库。测试会 drop `vm_*`，禁止指向 MEMORY_DATABASE_URL。真实模型 smoke 也清理 loopback 55436 的同一测试库，两者不能同时执行。

先启动独立 demo Vite，再执行 `MEMORY_UI_BASE_URL=http://127.0.0.1:<port> node scripts/browser-memory-smoke.mjs`。该脚本使用 fixture HTTP 与真实 Live2D，不登录真实账户；截图为 `.runtime/memory-ui-smoke.png`。

真实模型检查显式设置 `MEMORY_SMOKE_DATABASE_URL` 后执行 `backend/.venv/bin/python scripts/memory-provider-smoke.py`，只接受 `127.0.0.1:55436/avatar_memory_test`。合成账号调用真实抽取/embedding/LLM/TTS，验证跨会话回答与音频帧；不测设备播放，不输出原文，不创建容量/十年质量通过声明。

`scripts/browser-memory-continuity-smoke.mjs` 供操作员使用独立合成账号验证真实 HTTPS/RTC。`MEMORY_CONTINUITY_PHASE=write` 保存姓名、分两次说会议并检查同编号/新版本/明确时刻；操作员为同一合成账号轮换临时登录 cookie 后，以 `MEMORY_CONTINUITY_PHASE=read` 启动全新浏览器，验证姓名实际回答。它不模拟 OIDC 提供商流程，不读取真实用户 cookie；凭据仅来自受保护 `.runtime/private-rtc-smoke/profile.json`，结束需撤销合成账号。普通部署无需这个测试凭据文件。
