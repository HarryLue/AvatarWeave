# 记忆模块运行与验证

对应[交付记录](tasks.md)，原设计及 166 个模型保持不变。根 `.env` 默认不开放长期记忆或私人 RTC；下面明确区分部署命令和独立验证命令。用户部署开发环境后，已在 2026-11-04 修复 Nginx 登录重定向循环，并验证真实 OIDC 登录/退出；修复仅平滑重载 Nginx，未重启应用进程。

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

当前动作限经批准的有限静默 Live2D 动作；motionFinish 才能提交 completed。音频无设备播放回执时保持 published/unknown，不能把 TTS 帧或发送成功记为“用户已听见”。

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
