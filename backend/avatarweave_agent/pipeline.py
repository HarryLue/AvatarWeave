"""Provider-independent configuration and LiveKit session construction."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import ROOT
from .providers import EXTRAS, PROVIDERS, REGISTRY

logger = logging.getLogger("avatarweave.pipeline")


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: str
    options: dict[str, Any] = Field(default_factory=dict, repr=False)


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    turn_detection: Literal["vad", "stt"] = "vad"
    min_endpointing_delay: float = Field(default=0.25, ge=0, le=5)
    max_endpointing_delay: float = Field(default=2, gt=0, le=15)
    preemptive_generation: bool = True
    preemptive_tts: bool = False
    min_interruption_duration: float = Field(default=0.5, ge=0.1, le=5)
    false_interruption_timeout: float = Field(default=1, gt=0, le=10)
    resume_false_interruption: bool = True
    user_away_timeout: float = Field(default=300, gt=0)
    vad: dict[str, Any] = Field(default_factory=lambda: {"min_silence_duration": 0.3})

    @model_validator(mode="after")
    def delays(self):
        if self.max_endpointing_delay < self.min_endpointing_delay:
            raise ValueError("max_endpointing_delay must be >= min_endpointing_delay")
        return self


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1] = 1
    mode: Literal["mock", "pipeline"] = "pipeline"
    profile: str = "openai-streaming"
    llm: ProviderConfig | None = None
    stt: ProviderConfig | None = None
    tts: ProviderConfig | None = None
    session: SessionConfig = Field(default_factory=SessionConfig)

    def summary(self):
        return {
            "mode": self.mode,
            "profile": self.profile,
            **{
                name: getattr(self, name).provider if getattr(self, name) else None
                for name in PROVIDERS
            },
        }

    @model_validator(mode="after")
    def providers(self):
        if self.mode == "pipeline":
            for kind, names in PROVIDERS.items():
                spec = getattr(self, kind)
                if spec is None or spec.provider not in names:
                    raise ValueError(f"{kind}.provider must be one of {sorted(names)}")
        return self


ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def resolve_env(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env(item, env) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        key, default = match.groups()
        result = env.get(key) or default
        if result is None:
            raise ValueError(
                f"Missing credential/config variable: {key}; set it in .env"
            )
        return result

    return ENV_PATTERN.sub(replace, value)


def load_pipeline(
    path: Path | None = None, *, env: dict[str, str] | None = None
) -> PipelineConfig:
    environment = {
        k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None
    }
    environment.update(os.environ if env is None else env)
    path = path or Path(
        environment.get("PIPELINE_CONFIG", ROOT / "config/pipeline.yaml")
    )
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise ValueError(f"Pipeline configuration not found: {path}")
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict) or set(raw) - {
        "version",
        "mode",
        "profile",
        "profiles",
        "session",
    }:
        raise ValueError(
            "Invalid pipeline document; expected version, mode, profile, profiles, session"
        )
    mode = environment.get("PIPELINE_MODE") or raw.get("mode", "pipeline")
    profile = environment.get("PIPELINE_PROFILE") or raw.get(
        "profile", "openai-streaming"
    )
    selected = {}
    if mode != "mock":
        profiles = raw.get("profiles", {})
        if profile not in profiles:
            raise ValueError(f"Unknown pipeline profile: {profile}")
        selected = profiles[profile]
        if set(selected) - {"llm", "stt", "tts", "session"}:
            raise ValueError("Profile may contain only llm, stt, tts, session")
    result = {
        "version": raw.get("version", 1),
        "mode": mode,
        "profile": profile,
        **{k: v for k, v in selected.items() if k != "session"},
        "session": {**raw.get("session", {}), **selected.get("session", {})},
    }
    # Resolve only the selected profile. Unused providers do not require credentials or imports.
    return PipelineConfig.model_validate(resolve_env(result, environment))


def provider_class(kind: str, spec: ProviderConfig):
    try:
        module = importlib.import_module(f"livekit.plugins.{spec.provider}")
    except ImportError as exc:
        extra = EXTRAS.get(spec.provider)
        hint = f"uv sync --extra {extra}" if extra else "uv sync --locked"
        raise ValueError(
            f"{spec.provider} dependencies unavailable; run `{hint}` in backend"
        ) from exc
    name = {"llm": "LLM", "stt": "STT", "tts": "TTS"}[kind]
    if kind == "tts":
        name = REGISTRY[spec.provider].tts_class
    return getattr(module, name)


def build_provider(kind: str, spec: ProviderConfig):
    cls = provider_class(kind, spec)
    signature = inspect.signature(cls)
    if not any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values()):
        unknown = set(spec.options) - signature.parameters.keys()
        if unknown:
            raise ValueError(
                f"Unsupported {kind}/{spec.provider} options: {sorted(unknown)}"
            )
    return cls(**spec.options)


def prewarm(proc):
    from livekit.plugins import silero

    config = load_pipeline()
    if config.mode == "mock":
        return
    # Import only selected plugins in the job process before it accepts a session.
    for kind in PROVIDERS:
        provider_class(kind, getattr(config, kind))
    proc.userdata["vad"] = silero.VAD.load(**config.session.vad)


def create_session(ctx, config: PipelineConfig):
    from livekit.agents import AgentSession
    from livekit.plugins import silero

    components = {
        kind: build_provider(kind, getattr(config, kind)) for kind in PROVIDERS
    }
    vad = ctx.proc.userdata.get("vad") or silero.VAD.load(**config.session.vad)
    options = config.session
    return AgentSession(
        **components,
        vad=vad,
        turn_handling={
            "turn_detection": options.turn_detection,
            "endpointing": {
                "min_delay": options.min_endpointing_delay,
                "max_delay": options.max_endpointing_delay,
            },
            "interruption": {
                "mode": "vad",
                "min_duration": options.min_interruption_duration,
                "false_interruption_timeout": options.false_interruption_timeout,
                "resume_false_interruption": options.resume_false_interruption,
            },
            "preemptive_generation": {
                "enabled": options.preemptive_generation,
                "preemptive_tts": options.preemptive_tts,
            },
        },
        user_away_timeout=options.user_away_timeout,
    )


def load_tts_profile(profile: str) -> ProviderConfig:
    """Resolve TTS only for mock preview; unused ASR/LLM credentials are unnecessary."""
    environment = {
        k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None
    }
    environment.update(os.environ)
    path = Path(environment.get("PIPELINE_CONFIG", ROOT / "config/pipeline.yaml"))
    if not path.is_absolute():
        path = ROOT / path
    raw = yaml.safe_load(path.read_text())
    spec = ProviderConfig.model_validate(
        resolve_env(raw["profiles"][profile]["tts"], environment)
    )
    if spec.provider not in PROVIDERS["tts"]:
        raise ValueError("Unsupported TTS provider")
    return spec
