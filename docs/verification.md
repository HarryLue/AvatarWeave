# 当前验证记录

## 2026-10-29：酒店入住英语陪练首版

开发分支：`feat/travel-english-mvp`，基于 `e245dbd`。原 Demo 保留，产品入口为 `apps/speaking`，部署路径 `/speaking/`。

| 验证 | 结果 |
| --- | --- |
| Python 后端测试 | 42 项通过，包含 7 项陪练业务测试 |
| 前端构建、TypeScript、现有 Node 测试 | `npm run check` 通过 |
| 修改的后端代码静态检查 | Ruff 通过 |
| mock 端到端 | 独立 LiveKit、临时 SQLite、手机尺寸 Chromium，完整流程通过 |
| 真实阿里组合与当前 LLM | 三个目标、帮助、结束归档、真实报告、失败重试、历史恢复通过 |
| 实际播放 | 通过 SDK 首次非静音音频播放标记，确认收到音频能量并播放 |
| 权限拒绝 | 拒绝麦克风时显示错误，不创建练习或消耗试用次数 |
| 浏览器错误 | 完整验收零 pageerror；393px 页面无横向溢出 |

真实模型使用浏览器文字消息驱动练习，麦克风输入使用 Chromium 测试设备；没有把此次验证等同于真人英语识别或真实手机验收。原始录音未落库。邀请试用前仍需用真实 iPhone/Android 检查语音识别、音频恢复、蓝牙与前后台切换。浏览器测试机缺少中文字体，截图中的中文可能显示为方框，产品使用设备系统字体。

复现：先运行 `npm run build`，再执行 `backend/.venv/bin/python scripts/practice-smoke.py`；加 `--pipeline` 调用真实付费模型。真实组合验证产物在 `/tmp/avatarweave-practice-smoke-hsng1pel/`，只供本机本轮审阅。隔离验收服务已清理。

同日补充公网发布：执行 `bash scripts/deploy.sh publish`，发布 `/speaking/` 静态产物和独立 SPA 路由，沿用 main 已有 Nginx、TLS 证书与自动续期。配置校验成功并重载；没有重启后端或重新申请证书。共享路由旧版已备份在 `.runtime/web-publish-*/routes.conf`。

`node scripts/browser-speaking-https-smoke.mjs` 通过：浏览器正常验证公网证书，393px 页面和数字人加载成功，麦克风 API 处于安全上下文，`/speaking` 正确重定向、深层路径返回陪练页面、缺失静态资源返回 404，根路径仍是原 Demo。经 `/api/practice/` 生成的凭证使用同站 `/realtime` WSS 地址，零 pageerror。本次公网验收没有开启付费模型对话。

### 结束练习时的 1006 回归

用户反馈的房间在服务端记录为 `CLIENT_REQUEST_LEAVE`，对应练习状态 `complete`，报告已保存。修复结束按钮和状态轮询的关闭竞争：点击后停麦克风，等待 Worker 确认落库，再由统一流程关闭客户端、生成反馈；延迟返回的 `active` 快照不能重新启用界面。

SDK 的并发 `dispose()` 调用共享同一个 Promise，关闭后的转写取消不再产生页面错误。每个 SDK 自建房间使用独立日志处理，仅在明确主动关闭后，将信令流的精确 1006 错误记为调试信息；活动连接中的同类错误、其他类型错误仍保留错误日志。SDK 读循环可能在 disconnect 完成后才拒绝，日志处理因此保留到迟到回调结束，不引用房间或客户端。

`npm run check` 通过，20 项 Node 测试包含主动退出、活动断线、并发销毁和迟到转写。`scripts/practice-smoke.py` 通过真实 LiveKit + mock Agent + Chromium 端到端回归，注入 Safari 类似的 1006 事件及延迟状态响应后，保存、反馈重试、历史恢复正常，零 pageerror／信令错误。隔离前端端口改为 5176，并在启动前检查端口占用，避免误连已经运行的 5175 开发服务。真实 iOS 关闭行为仍需手机复测。

修复已通过 `bash scripts/deploy.sh publish` 发布。公网 HTTPS 上运行 `PRACTICE_SMOKE_URL=https://111.56.189.4/speaking/ PRACTICE_SMOKE_FINISH_ONLY=1 node scripts/browser-practice-smoke.mjs`，独立访客、真实模型与反馈同样通过结束流程回归；包括主动关闭时注入 1006、延迟轮询、反馈失败重试及刷新后的记录恢复，零页面／信令错误。未重启后端或变更证书。

## 2026-10-28：SDK 与通用 Demo

2026-10-28，AvatarWeave 仅维护当前接口。

| 验证 | 结果 |
| --- | --- |
| Python 后端测试 | 30 项通过 |
| SDK 协议与流式转写测试 | 7 项通过 |
| 前端构建、TypeScript | 通过 |
| 三个 npm 库打包 | 通过，当前导出不含别名 |
| 配置及四份公开 JSON Schema 一致性 | 通过 |
| 通用数字人/任意语言/能力控件浏览器检查 | 通过，零 pageerror |
| 当前阿里组合真实音色试听、会话和回复 | 通过，零 pageerror |
| 本地 GPU FP32 推理 | 通过，使用已有 CUDA/cuDNN |
| 主/GPU 虚拟环境直接 uvicorn 入口 | 通过，不依赖其他项目路径 |

## 契约验证

- metadata 仅接受 avatarweave_session + context。API 生成版本 1 及默认数字人，Worker 再次校验。未知字段/版本、非 JSON、重复 key、非有限数值和超长内容被拒绝。
- 动作只在 avatarweave.command.v1 发布一次；接收端只消费这个 topic，并按 ID 去重。动作和表情分别有严格字段约束，Python、TypeScript、配置与 schema 一致。
- 文本只用 lk.transcription Text Stream，保留同一语段的增量合并、final 标记及过期快照保护。
- 当前公开入口为 avatarweave_agent、AvatarView、AvatarRenderer、loadRendererCore 和 /avatar-assets/；项目中不提供其他名称/路径的转发。
- 自有代码和文档扫描完成，供应商源码 SHA256 校验通过。来源/版权记录不参与运行协议。

## 验证范围

当前真实会话使用部署选择的阿里 ASR/TTS 与 LLM；没有假设所有云端 provider 均实测。SQLite 测试本轮通过；PostgreSQL 保留同一 CI 矩阵，本轮未重复启动数据库容器。CPU/GPU 手动工作流已提供，本轮未在远端 GitHub 执行。未升级或重装机器 CUDA/cuDNN，未修改现有密钥值、模型文件或数据库数据。
