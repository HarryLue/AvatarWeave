# Aliyun plugin for LiveKit Agents

Support for [Alibaba Cloud DashScope](https://dashscope.aliyun.com/)'s voice AI services in LiveKit Agents.

More information is available in the docs for the [TTS](https://help.aliyun.com/zh/model-studio/developer-reference/cosyvoice-api) and [STT](https://help.aliyun.com/zh/model-studio/developer-reference/paraformer-api) integrations.

## Installation

```bash
pip install livekit-plugins-aliyun
```

## Pre-requisites

You'll need an API key from Alibaba Cloud DashScope. It can be set as an environment variable: `DASHSCOPE_API_KEY`

## Usage

### TTS (Text-to-Speech)

```python
from livekit.plugins.aliyun import TTS

tts = TTS(
    model="cosyvoice-v3-flash",
    voice="longanyang",
    api_key="your-api-key",
)
```

### STT (Speech-to-Text)

```python
from livekit.plugins.aliyun import STT

stt = STT(
    model="paraformer-realtime-v2",
    api_key="your-api-key",
)
```

## Supported Models

### TTS Models
- `cosyvoice-v3-flash` - Fast speech synthesis model
- `cosyvoice-v3` - Standard speech synthesis model

### STT Models
- `paraformer-realtime-v2` - Real-time speech recognition
