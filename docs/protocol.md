# AvatarWeave 协议 v1

本项目只实现一个当前协议，不自动识别其他 topic、字段名、版本或不透明字符串。LiveKit 的标准媒体/文本接口与 AvatarWeave 应用协议分开定义。

| 能力 | 当前接口 |
| --- | --- |
| 凭证 | `POST /api/token` → HTTP 201 `{server_url, participant_token, room_name}` |
| 数字人目录 | `GET /api/avatars` |
| 音色能力 | `GET /api/tts/voices` |
| 音色试听 | `POST /api/tts/preview`，固定服务端文案 |
| 用户消息 | Text Stream `lk.chat` |
| 用户转写及助手回复 | Text Stream `lk.transcription` |
| 语音与口型 | Agent 音频轨道 → MediaStream → renderer |
| Agent 状态 | 参与者 attribute `lk.agent.state` |
| 数字人动作 | Reliable Data `avatarweave.command.v1`，每条指令发布一次 |

## 会话 metadata

LiveKit dispatch metadata 是字符串；AvatarWeave 使用严格 JSON envelope：

```json
{
  "avatarweave_session": {
    "version": 1,
    "avatar_id": "kei",
    "tts_voice": "zf_xiaoxiao"
  },
  "context": {
    "request_id": "your-request-id"
  }
}
```

- 只有一个应用命名空间。`context` 是应用自定义 JSON 数据，不能覆盖模型密钥、provider 参数或推理地址。
- 非空 metadata 必须是 JSON object，只接受以上两个顶层字段；命名空间存在时必须明确传 `version: 1`。
- API 未收到 metadata 时，生成当前默认数字人及明确版本；未选择音色时沿用配置中的默认音色。直接调度任务的空 metadata 同样使用服务端默认配置。
- 未知字段/版本、重复 JSON key、非有限数值、不透明字符串及超过 4096 UTF-8 字节的数据直接拒绝，不做降级解析。
- Token API 和 Worker 共用类型及选项校验；通过 `room_config` 直接传 metadata 也不能绕过校验。标准 LiveKit 的 `participant_metadata` 是另一字段，不等同于 Agent dispatch metadata。

通常前端只需在 Token 请求中传 `avatar_id` 和 `tts_voice`，由 API 生成 envelope。需要自定义上下文时：

```json
{
  "avatar_id": "kei",
  "tts_voice": "zf_xiaoxiao",
  "room_config": {
    "agents": [{
      "agent_name": "avatar-assistant",
      "metadata": "{\"context\":{\"request_id\":\"123\"}}"
    }]
  }
}
```

Agent name 必须在部署允许列表中。模型、音色及动作白名单来源于服务端配置。

## 动作消息

```json
{"version":1,"id":"unique-command-id","type":"motion","name":"","index":3}
```

```json
{"version":1,"id":"another-command-id","type":"expression","name":"Smile"}
```

- `id` 是 1–128 字符的唯一指令标识，发送方为每次动作生成新 ID；前端缓存最近 256 个 ID 去重。
- `motion` 必须包含模型动作组 `name` 和非负安全整数 `index`。空组名允许存在。
- `expression` 必须包含非空表情名 `name`，不接受动作索引。
- 只接受定义中的字段、整数版本 1 和已知动作类型；Python、TypeScript 和导出的 JSON Schema 使用同一契约语义。
- 前端只接受 Agent 参与者；自定义普通参与者身份需要显式指定可信 `agentIdentity`。
- `name` / `index` 必须对应模型真实资源；`config/avatars.yaml` 将语义动作映射为这些模型字段。

```python
from avatarweave_agent.protocol import COMMAND_TOPIC, make_command

command = make_command({"type": "motion", "name": "", "index": 3})
await ctx.room.local_participant.publish_data(
    command.model_dump_json(exclude_none=True).encode(),
    reliable=True,
    topic=COMMAND_TOPIC,
)
```

该协议不提供动作执行回执或精确语音时间同步。交付成功不等同于模型一定存在对应动作。

## 流式转写

SDK 仅注册 `lk.transcription` Text Stream，不订阅已废弃的转写事件。以 `lk.segment_id` 合并同一语段增量；没有该可选属性时使用官方 stream ID。以 `lk.transcription_final` 及 trailer 的最终属性判断临时/最终状态。短句只产生少量增量时，不伪造逐字动画。

接入宿主已管理的文字流时，设置 `manageTranscription:false`，由宿主使用官方 hooks 或 handler 展示消息。这是集成责任划分，不是另一个协议。

## 音频试听与生命周期

试听为 16-bit PCM 流，客户端读取 `X-Audio-Sample-Rate` / `X-Audio-Channels`，不固定假定 24 kHz。服务端负责关闭 TTS、音频流和自建 HTTP session。

`AvatarClient` 可管理自己的 Room，或接收宿主 Room；宿主 Room 不由 SDK 连接/断开。卸载调用 `dispose()`，切断口型绑定并清理自己注册的监听器。宿主负责自己管理的音频播放。

## 机器可读契约

- `docs/session-metadata.schema.json`：完整会话 envelope。
- `docs/session-options.schema.json`：命名空间内的选项。
- `docs/avatar-command.schema.json`：动作消息。
- `docs/avatar-profile.schema.json`：模型与动作配置。

运行 `backend/.venv/bin/python scripts/config-schema.py` 从实际 Python 类型导出；CI 使用 `--check` 检查一致性。改变含义或删除字段时发布新的明确协议版本，不增加自动猜测或回退分支。

官方依据：[Text and transcriptions](https://docs.livekit.io/agents/multimodality/text/)、[Text streams](https://docs.livekit.io/transport/data/text-streams/)、[Participant metadata](https://docs.livekit.io/transport/data/state/participant-attributes/)。
