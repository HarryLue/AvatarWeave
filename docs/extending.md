# 扩展 provider、音色与数字人

## 新增或调整音色

当前 TTS 的音色能力来自插件内置目录、`config/voices.yaml` 以及配置中的默认音色。后两者无需修改 API、Worker 或页面。示例：

```yaml
version: 1
providers:
  zhiai:
    notice: 按自建服务实际部署的音色填写。
    voices:
      - id: your-speaker
        name: 自建音色
        language: zh
        language_label: 普通话
        description: 自建服务中的指定说话人。
        preview_text: 你好，这是一个试听示例。
```

目录是部署方审核的白名单，不会自动枚举付费账号的所有声音。没有专用目录的 provider 至少展示配置中字符串形式的默认音色。前端按返回的 `capabilities.voice_selection`、`capabilities.preview` 决定显示控件；语言分组从返回数据生成，不限定中文或英语。

试听统一调用标准 LiveKit `TTS.synthesize()` 并转发 AudioFrame PCM；使用实际采样率/声道数。需要借用 JobContext HTTP session 的插件，在 API 试听中由能力层注入并关闭自己的 HTTP session。HTTP 首包前合成失败返回 503，流中失败终止响应。客户端退出时关闭合成流、TTS 和自建 HTTP session。

`MOCK_TTS_PREVIEW=true` 可以在文字 mock 模式下启用当前 profile 的 TTS 试听，只解析 TTS 配置，不要求无关 ASR/LLM 密钥。

## 新增 provider

1. 按 LiveKit STT / LLM / TTS 接口实现插件，或使用已有官方插件。
2. 在 `backend/avatarweave_agent/providers.py` 注册支持节点、显示名称、可选依赖、音色参数名和可选目录模块；例如自建服务使用 `speaker` 而其他服务使用 `voice`。
3. 在 pipeline YAML 中组合该 provider，补充相应依赖与锁文件。
4. 为流式行为、打断、资源释放和参数适配增加协议测试；有真实凭证时运行语音验收。

新的 provider 不需要修改 Token API、会话编排或前端。供应商源码按来源清单维护，供应商差异声明集中在 registry。供应商云端是否可用仍取决于账号、模型和资源权限；目录能力不代表已经对所有云端服务实测。

## 新增数字人和动作

在 `config/avatars.yaml` 增加条目，配置资源 URL、名称和语义动作。前端会读取 `/api/avatars`，多个数字人时显示选择框；连接后固定本次选择。修改服务端配置后重启 API 和 Agent，并刷新前端。

```yaml
version: 1
default_avatar: custom
avatars:
  custom:
    name: 我的数字人
    renderer: live2d
    assets:
      core_url: /live2d/your-core.js
      model_url: /models/your.model3.json
      motion_sync_url: /models/your.motionsync3.json
    default_action: wave
    actions:
      wave:
        label: 挥手
        type: motion
        name: Wave
        index: 0
      happy:
        label: 高兴
        type: expression
        name: Smile
```

动作组/索引/表情必须存在于该模型中，`wave` 等语义标识由应用自行命名。Agent 的动作工具只允许调用此配置中的动作，并将语义标识解析为现有 `avatarweave.command.v1`。前端直接动作按钮与 Agent 使用同一份映射。新增数字人不需修改 Demo JSX 或 Worker 的固定索引。

目前配置层支持 `live2d` 渲染器；要增加 VRM/视频等渲染方案，仍需实现对应 renderer、前端选择机制并扩展配置类型。协议尚不提供动作执行回执或精确语音时间同步。

## 会话选项

`POST /api/token` 可传 `avatar_id` 和 `tts_voice`。签名 dispatch metadata 示例：

```json
{"avatarweave_session":{"version":1,"avatar_id":"custom","tts_voice":"your-speaker"}}
```

选项会在 Token API 和 Worker 中分别校验。业务附加信息只能放在 envelope 的 `context` 对象中；非 JSON 字符串、未知顶层字段和版本会返回 422，不做自动解析或回退。

配置 schema 位于 `docs/avatar-profile.schema.json`、`docs/session-options.schema.json`。改变配置模型后运行 `backend/.venv/bin/python scripts/config-schema.py`，CI 会检查它们是否过期。
