# 第三方来源与许可

- 原前端 `src/common/AvatarController.ts`、`NetworkService.ts`、`DigitalHumanEngine.ts` 提供渲染、AudioWorklet 延迟回调保护和 LiveKit 流式通信的迁移参考。新 SDK 重新定义了类型、实例生命周期和公开边界。
- 原后端 `/data/luwei/agents-v2/multi-role-agent` 提供可配置 agent、历史消息和独立 session pipeline 的设计参考。未复制业务数据库、硬编码凭证、账号、课程、计费服务。旧 provider 的完整迁移来源见下文。
- LiveKit Agents 和官方 starter 参考官方现行 AgentServer/AgentSession API。官方代码来源的许可应随实质复制内容保留；本实现未整段复制 starter。
- `apps/demo/public/live2d/aitutor.min.js` 保留 Live2D Cubism Core 原版权及许可说明：https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html 。
- `apps/demo/public/live2d/kei_vowels_pro/` 来自原项目前端，仅用于当前项目演示。未找到可核验的独立模型再分发授权，商业产品应取得授权或替换为自有模型。
- `.tools/livekit-server` 是官方 LiveKit Server 1.13.11 的本机运行副本；源码与 Apache-2.0 许可：https://github.com/livekit/livekit 。该目录不随 SDK npm 包发布。
- PixiJS、pixi-live2d-display、live2d-motionsync 和 LiveKit SDK 的具体许可见各依赖包。SDK `files` 仅包含编译产物，不打包第三方 Core 或模型。

当前包 license 字段为 UNLICENSED，用于内部/私有分发。公开或商业发布前由代码和素材权利人确定最终授权。

## 旧 provider 迁移

`backend/livekit/plugins/huoshan`、`alibaba`、`zhiai` 完整复制自原 agents-v2 对应目录，保留原版权和许可头。逐文件来源及 SHA256 见 `backend/provider-provenance.json`。未复制旧业务账号、计费逻辑、配置密钥或数据库。新实现只在外层工厂适配配置和新版 LiveKit 管道。

## Kokoro 本地推理

- `kokoro-onnx` 0.6.1：[thewh1teagle/kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)，MIT。
- 模型为 [hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)，Apache-2.0；ONNX 和音色转换文件来自 `kokoro-onnx` 的 `model-files-v1.1` release（实际使用 v1.0 模型与配套音色）。精确文件名和 SHA256 固定在 `scripts/download-kokoro.py`。
- 中文 G2P 使用 [hexgrad/misaki](https://github.com/hexgrad/misaki)（Apache-2.0）及其依赖；英语音素化使用 phonemizer（GPL-3.0）/eSpeak NG（GPL-3.0-or-later），保留依赖各自许可。模型、原生库及其许可证不打包进前端 SDK。发布后端二进制或容器时需随依赖保留对应许可证和源码提供义务。
- 音色名称、语言、男女声与上游质量分级来自 [官方 VOICES.md](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)。说明中的试听用途为本项目建议，不表示上游保证该场景音质。
