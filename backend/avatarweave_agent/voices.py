"""Provider-neutral voice discovery, selection and LiveKit PCM preview lifecycle."""

import importlib
import inspect
from dataclasses import dataclass

import aiohttp
import yaml
from pydantic import BaseModel, ConfigDict, Field

from .config import ROOT, settings
from .pipeline import (
    ProviderConfig,
    build_provider,
    load_pipeline,
    load_tts_profile,
    provider_class,
)
from .providers import REGISTRY


class Voice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=128)
    name: str
    description: str = ""
    language: str = "multi"
    language_label: str = "多语言"
    gender: str = ""
    preview_text: str = "你好，我是你的数字人助手。很高兴和你一起交流。"


@dataclass
class VoiceService:
    spec: ProviderConfig | None
    voices: list[Voice]
    notice: str = ""
    source: str = ""

    def describe(self):
        definition = REGISTRY[self.spec.provider] if self.spec else None
        return {
            "provider": self.spec.provider if self.spec else None,
            "provider_label": definition.label if definition else "",
            "enabled": self.spec is not None,
            "capabilities": {
                "voice_selection": bool(self.voices),
                "preview": bool(self.voices),
            },
            "default_voice": self.spec.options.get(definition.voice_option)
            if definition and definition.voice_option
            else None,
            "voices": [
                voice.model_dump(exclude={"preview_text"}) for voice in self.voices
            ],
            "notice": self.notice,
            "source": self.source,
        }

    def select(self, voice_id: str):
        if self.spec is None:
            raise ValueError("Voice selection is not enabled for this session")
        definition = REGISTRY[self.spec.provider]
        voice = next((v for v in self.voices if v.id == voice_id), None)
        if not voice or not definition.voice_option:
            raise ValueError("Unsupported TTS voice")
        spec = self.spec.model_copy(deep=True)
        spec.options[definition.voice_option] = voice_id
        return spec, voice


def voice_service(config=None) -> VoiceService:
    config = config or load_pipeline()
    spec = config.tts
    if config.mode == "mock":
        spec = load_tts_profile(config.profile) if settings.mock_tts_preview else None
    if spec is None:
        return VoiceService(None, [])
    definition = REGISTRY[spec.provider]
    voices, notice, source = {}, "", ""
    if definition.voice_catalog:
        module = importlib.import_module(definition.voice_catalog)
        voices.update(module.VOICES)
        notice, source = getattr(module, "NOTICE", ""), getattr(module, "SOURCE", "")
    path = settings.voice_config
    if not path.is_absolute():
        path = ROOT / path
    document = yaml.safe_load(path.read_text())
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or set(document) - {"version", "providers"}
    ):
        raise ValueError("Invalid voice configuration")
    entry = document.get("providers", {}).get(spec.provider, {})
    notice, source = entry.get("notice", notice), entry.get("source", source)
    for item in entry.get("voices", []):
        voice = Voice.model_validate(item)
        voices[voice.id] = voice.model_dump()
    default = (
        spec.options.get(definition.voice_option) if definition.voice_option else None
    )
    if isinstance(default, str) and default and default not in voices:
        voices[default] = {
            "id": default,
            "name": default,
            "description": "当前配置使用的音色；可在 config/voices.yaml 补充说明。",
        }
    return VoiceService(
        spec, [Voice.model_validate(v) for v in voices.values()], notice, source
    )


@dataclass
class PCMPreview:
    engine: object
    stream: object
    first: object
    http_session: object = None
    closed: bool = False

    @property
    def headers(self):
        frame = self.first.frame
        return {
            "X-Audio-Sample-Rate": str(frame.sample_rate),
            "X-Audio-Channels": str(frame.num_channels),
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        }

    async def chunks(self):
        try:
            yield self.first.frame.data.tobytes()
            async for event in self.stream:
                yield event.frame.data.tobytes()
        finally:
            await self.close()

    async def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.stream.aclose()
        finally:
            try:
                await self.engine.aclose()
            finally:
                if self.http_session:
                    await self.http_session.close()


async def open_preview(service: VoiceService, voice_id: str) -> PCMPreview:
    spec, voice = service.select(voice_id)
    http_session = None
    engine, stream = None, None
    try:
        # Plugins which normally borrow JobContext's HTTP session also work in
        # the API process. The preview owns and closes this injected session.
        cls = provider_class("tts", spec)
        if "http_session" in inspect.signature(cls).parameters:
            http_session = aiohttp.ClientSession(trust_env=True)
            spec.options["http_session"] = http_session
        engine = build_provider("tts", spec)
        stream = engine.synthesize(voice.preview_text)
        first = await anext(stream)
        return PCMPreview(engine, stream, first, http_session)
    except BaseException:
        try:
            if stream is not None:
                await stream.aclose()
        finally:
            try:
                if engine is not None:
                    await engine.aclose()
            finally:
                if http_session:
                    await http_session.close()
        raise
