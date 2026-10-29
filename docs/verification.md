# 当前验证记录

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
