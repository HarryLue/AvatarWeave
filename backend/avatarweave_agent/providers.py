"""Provider-specific declarations live here, outside API/session orchestration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    label: str
    kinds: frozenset[str]
    extra: str | None = None
    voice_option: str | None = None
    voice_catalog: str | None = None
    tts_class: str = "TTS"
    local_service: str | None = None


REGISTRY = {
    "openai": Provider(
        "OpenAI", frozenset({"llm", "stt", "tts"}), voice_option="voice"
    ),
    "google": Provider("Google", frozenset({"llm"}), extra="google"),
    "kokoro": Provider(
        "Kokoro",
        frozenset({"tts"}),
        voice_option="voice",
        voice_catalog="livekit.plugins.kokoro.voices",
        local_service="kokoro",
    ),
    "huoshan": Provider("火山", frozenset({"stt", "tts"}), voice_option="voice"),
    "alibaba": Provider("阿里", frozenset({"stt", "tts"}), voice_option="voice"),
    "zhiai": Provider(
        "自建语音",
        frozenset({"stt", "tts"}),
        extra="zhiai",
        voice_option="speaker",
        tts_class="TTS_WS_MultiTurn",
    ),
    "cartesia": Provider(
        "Cartesia", frozenset({"stt", "tts"}), extra="cartesia", voice_option="voice"
    ),
    "deepgram": Provider("Deepgram", frozenset({"stt"}), extra="deepgram"),
    "elevenlabs": Provider(
        "ElevenLabs", frozenset({"tts"}), extra="elevenlabs", voice_option="voice"
    ),
}
PROVIDERS = {
    kind: {name for name, definition in REGISTRY.items() if kind in definition.kinds}
    for kind in ("llm", "stt", "tts")
}
EXTRAS = {
    name: definition.extra for name, definition in REGISTRY.items() if definition.extra
}
