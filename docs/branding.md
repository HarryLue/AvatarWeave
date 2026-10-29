# AvatarWeave 命名与项目边界

这是开发设计中的独立项目，只提供当前接口，不提供历史版本兼容入口。

| 内容 | 当前名称 |
| --- | --- |
| 项目路径 | `/data/luwei/avatarweave` |
| npm 库 | `@avatarweave/core`、`@avatarweave/renderer`、`@avatarweave/react` |
| Python 应用 | `avatarweave_agent` |
| 组件与实例 | `AvatarView`、`AvatarRenderer`、`RendererOptions` |
| 渲染 Core 加载器 | `loadRendererCore` |
| 动作 topic | `avatarweave.command.v1` |
| Agent metadata | `avatarweave_session` + `context` |
| 音色偏好 | `avatarweave.voice.<provider>` |
| Demo 资产 URL | `/avatar-assets/` |
| Kokoro 插件 | `livekit.plugins.kokoro` |

外部项目按当前 npm 包和 [协议](protocol.md) 接入。不为其他模块名、类名、存储键、目录或协议提供别名、转发或双发。

LiveKit/Live2D/OpenAI 等实际依赖名称、标准 `lk.*` 通道、模型格式、厂商源码和版权声明保留真实名称。供应商协议适配是当前功能的一部分，来源记录不是历史版本兼容代码。
