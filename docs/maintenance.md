# 自动更新与验收约定

## 源码与依赖更新的范围

`packages` 提供独立 SDK，`apps/demo` 消费这些包。Renovate 负责依赖与锁文件更新，应用功能通过本仓库的代码、接口与测试演进。

Renovate 显式覆盖前端、主后端与 GPU 推理环境的两个 Python 锁文件，以及基础设施和 CI 配置。LiveKit 依赖按联合更新组更新；Kokoro、G2P 和 ONNX Runtime 按联合更新组提交，需人工审核。受来源清单保护的 huoshan/alibaba/zhiai 实现排除自动改写，测试校验来源清单中的 22 个源码哈希。

仓库必须接入 GitHub/Renovate 并启用应用和 Actions 后，更新计划才会执行；本地配置不会自行创建远程任务。自动合并还需要仓库设置正确的必需状态检查，配置文件不能代替分支保护。

## 日常验证

```bash
npm run check
backend/.venv/bin/python -m pytest backend/tests -q
backend/.venv/bin/python scripts/config-schema.py --check
backend/.venv/bin/python scripts/check-config.py
```

`.github/workflows/ci.yml` 覆盖构建、类型、SDK 协议/转写、SQLite/PostgreSQL、mock 房间与通用配置浏览器测试、干净系统安装。普通 CI 不需要付费模型密钥。

## 真实推理和语音

`.github/workflows/kokoro-runtime.yml` 是手动推理验收：CPU 使用普通 Linux runner，GPU 需要带 `self-hosted`、`linux`、`gpu` 标签且已安装 CUDA/cuDNN 的 runner。可设置仓库变量 `KOKORO_CUDA_LIB_DIR` 指向已有库。该流程只运行本地模型，不调用付费 ASR/LLM，也不下载 CUDA/cuDNN。

本机相同检查：

```bash
# 当前 .env 应配置正确设备；执行程序与设备对应
backend/.venv/bin/python scripts/kokoro-smoke.py             # CPU
backend/kokoro-runtime-gpu/.venv/bin/python scripts/kokoro-smoke.py  # GPU
node scripts/browser-config-smoke.mjs                      # 通用 UI 配置
node scripts/browser-voice-smoke.mjs                      # 当前真实 TTS/Agent 对话
backend/.venv/bin/python scripts/voice-smoke.py --exercise-asr
```

最后两项会调用当前配置的 ASR/LLM/TTS，可能产生对应服务费用，不放入无凭证公共 CI。浏览器配置测试验证任意语言分组、无试听能力时隐藏控件、自定义数字人名称和动作映射，不需要修改生产配置。

## 更新时维护的契约

- `avatarweave.command.v1` 的含义发生破坏性变化时，使用新版本和 topic。
- 配置与会话选项变更时重新导出 schema，更新扩展指南。
- 变更 provider 接口时，保留原厂商源码及哈希，并更新测试和来源记录。
- 环境相关 CUDA/cuDNN 路径留在 `.env`，不写入公共源码。
