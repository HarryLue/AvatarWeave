# AvatarWeave

[English](README.md) | **简体中文**

实时数字人交互 SDK、可配置 LiveKit Agent 和独立 Demo。前端提供 core、renderer、React 三个包；后端可组合云端/自建 ASR、LLM、TTS。SQLite 默认可用，支持 PostgreSQL。本项目只维护一个当前接口。

## 安装与启动

Linux Debian/Ubuntu：

```bash
cd /data/luwei/avatarweave
bash scripts/deploy.sh
```

脚本安装所需系统依赖、Node.js/uv/Python、LiveKit Server、锁定的前后端依赖及本地模型，然后构建并启动服务。已有 `.env`、数据库和模型缓存保留；`.env` 不存在时创建模板，初次模板为无模型凭证的文字 mock 模式。

```bash
bash scripts/deploy.sh install  # 只安装
bash scripts/deploy.sh start
bash scripts/deploy.sh restart
bash scripts/deploy.sh status
bash scripts/deploy.sh stop
```

可选 `install --skip-system-deps` 和 `install --all-providers`。首次安装需要访问 GitHub、PyPI、npm 等；网络代理使用标准环境变量。日志在 `logs/`。

真实语音在 `.env` 设置 `PIPELINE_MODE=pipeline` 并填写所选 provider 的凭证；`config/pipeline.yaml` 选择组合。`PIPELINE_PROFILE` 可显式覆盖 profile，`PIPELINE_CONFIG` 可指定配置文件。

前端 http://localhost:5174，API 文档 http://localhost:8090/docs。`scripts/dev.py` 只启动已安装的项目，不代替依赖安装。

## SDK 与服务边界

| 路径 | 职责 |
| --- | --- |
| `packages/core` / `@avatarweave/core` | Room、文字流、音频绑定、当前动作协议 |
| `packages/renderer` / `@avatarweave/renderer` | 模型渲染、口型、动作、生命周期 |
| `packages/react` / `@avatarweave/react` | `AvatarView` React 组件 |
| `apps/demo` | 使用 SDK 的参考前端 |
| `backend/avatarweave_agent` | API、Agent、配置、历史、推理编排 |
| `backend/livekit/plugins` | 供应商协议适配 |
| `backend/kokoro-runtime-gpu` | 隔离 GPU ONNX Runtime 的运行环境 |
| `config` | 模型组合、数字人、音色目录 |
| `infra` | 基础设施配置 |
| `scripts` | 安装、启动、模型下载、验收 |
| `docs` | 当前架构、协议、schema 和验证记录 |

```ts
import { AvatarClient } from '@avatarweave/core';
import { AvatarRenderer } from '@avatarweave/renderer';
import { AvatarView } from '@avatarweave/react';
```

```bash
npm run check
npm run pack:libs
```

产物为项目根目录的三个 `avatarweave-*.tgz`。Python 应用只使用 `avatarweave_agent`，Kokoro TTS 直接使用 `from livekit.plugins.kokoro import TTS`。当前包未发布到公共 registry。

## 语音与数字人配置

LiveKit 始终是实时传输和 Agent 框架。模型供应商独立选择：

```text
麦克风 → LiveKit → ASR → LLM → TTS → LiveKit → 浏览器音频与口型
```

- `config/pipeline.yaml`：ASR/LLM/TTS、端点、轮次检测、打断和提前生成参数。
- `config/avatars.yaml`：名称、资源 URL、默认数字人和语义动作映射。
- `config/voices.yaml`：各 provider 可用音色与说明，扩展插件目录。
- `.env`：凭证、数据库、配置位置和本机设备参数；公共接口不返回这些值。

配置中提供 OpenAI、火山、阿里、自建、Cartesia、Deepgram/ElevenLabs、Google 等组合，以及 OpenAI/阿里 ASR + Kokoro TTS。只解析所选节点的环境变量。`LLM_BASE_URL` / `ASR_BASE_URL` / `TTS_BASE_URL` 分别控制对应节点，具体以 YAML options 为准。供应商可用性受账号和资源权限约束。

数字人和音色在建立会话前选择，通过唯一的 `avatarweave_session` JSON envelope 传给 Agent。动作只使用 `avatarweave.command.v1`，一次发送；转写只走官方 Text Stream。完整规则见 [协议](docs/protocol.md)。

## 本地 Kokoro FP32

本地推理使用非量化 `kokoro-v1.0.onnx` 和配套音色文件，固定 SHA256 校验并缓存到 `models/kokoro/`。ASR 和 LLM 仍按各自配置运行。

```dotenv
PIPELINE_MODE=pipeline
PIPELINE_PROFILE=openai-asr-kokoro-tts
OPENAI_API_KEY=你的密钥
KOKORO_PRECISION=fp32
KOKORO_DEVICE=gpu
KOKORO_GPU_ID=0
```

GPU 环境只安装 `onnxruntime-gpu`，使用机器已有 CUDA/cuDNN，**不下载或安装 CUDA/cuDNN**。需要时通过 `KOKORO_CUDA_LIB_DIR` 或 `LD_LIBRARY_PATH` 指向已有库。GPU 初始化失败会报错，不静默退回 CPU。

```bash
# 修改设备后先确保对应依赖已安装
bash scripts/deploy.sh install
python3 scripts/dev.py --restart --kokoro-device gpu --kokoro-gpu-id 0
python3 scripts/dev.py --restart --kokoro-device cpu
```

GPU 参数为当前进程可见的 CUDA 编号。`http://127.0.0.1:8880/health` 返回实际设备、FP32、模型文件和 providers。

流式为“增量文本 → 短句合成 → PCM 分块送入 LiveKit”，单句仍需完成推理后出声。当前标准设置为 `speed=1.0`、`min_phrase_chars=12`、`max_phrase_chars=80`，具体以 YAML 为准。中文音色训练样本较少，先试听选择。可用 8 个中文、20 个美式英语、8 个英式英语目录项，说明见上游音色卡。

`MOCK_TTS_PREVIEW=true` 在 mock 模式启用所选 profile 的 TTS 试听，只解析 TTS 配置。`KOKORO_MANAGED=false` 使用独立部署的端点，不由本机启动器创建推理进程。

## 服务与数据库

| 服务 | 本机端口 |
| --- | --- |
| LiveKit | TCP 17880/17881，UDP 50100–50200 |
| API/Token | 127.0.0.1:8090 |
| Agent 健康检查 | 8091 |
| Demo 前端 | 5174 |
| Kokoro（选择该组合时） | 127.0.0.1:8880 |

选择云 TTS 时不需要本地 Kokoro。`compose.yaml` 当前只提供 LiveKit 和可选 PostgreSQL，没有将 API、Agent、前端和 Kokoro 全部容器化。

SQLite 默认使用 `avatar.db`。PostgreSQL：

```bash
docker compose --profile postgres up -d postgres
```

```dotenv
DATABASE_URL=postgresql+asyncpg://avatar:avatar@127.0.0.1:5433/avatar
```

`AGENT_HISTORY_LIMIT=100` 控制恢复最近 N 条，范围 1–2000；先取最新记录再正序交给模型，超过 500 条也不会遗漏最新上下文。当前是房间级历史，不提供跨房间用户记忆或多租户管理。

公网部署需要浏览器可达的 LiveKit 地址、HTTPS/WSS、媒体端口与用户鉴权。Vite 用于开发，正式前端部署 `apps/demo/dist`。

## 验证与维护

```bash
backend/.venv/bin/python -m pytest backend/tests -q
backend/.venv/bin/python scripts/config-schema.py --check
backend/.venv/bin/python scripts/check-config.py
node scripts/browser-config-smoke.mjs
node scripts/browser-voice-smoke.mjs
backend/.venv/bin/python scripts/voice-smoke.py --exercise-asr
```

最后两项调用所选模型服务，可能产生供应商费用。CI 包含构建、SQLite/PostgreSQL、当前协议和浏览器验证；CPU/GPU 真实推理提供手动工作流。Renovate 必须接入远程平台后才执行依赖更新。

[架构](docs/architecture.md) · [协议](docs/protocol.md) · [扩展指南](docs/extending.md) · [维护](docs/maintenance.md) · [验证记录](docs/verification.md) · [第三方来源与许可](THIRD_PARTY_NOTICES.md)
