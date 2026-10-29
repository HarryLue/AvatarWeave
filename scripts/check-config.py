"""Validate the selected provider profile without logging credentials or connecting models."""

import inspect
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from avatarweave_agent.pipeline import load_pipeline, provider_class
from avatarweave_agent.avatars import load_avatars
from avatarweave_agent.config import settings
from avatarweave_agent.providers import REGISTRY
from avatarweave_agent.voices import voice_service

try:
    config = load_pipeline()
    if config.mode == "pipeline":
        for kind in ("llm", "stt", "tts"):
            spec = getattr(config, kind)
            cls = provider_class(kind, spec)
            signature = inspect.signature(cls)
            unknown = set(spec.options) - signature.parameters.keys()
            if unknown and not any(
                p.kind == p.VAR_KEYWORD for p in signature.parameters.values()
            ):
                raise ValueError(
                    f"Unsupported {kind}/{spec.provider} option names: {sorted(unknown)}"
                )
    load_avatars()
    voices = voice_service(config)
    services = []
    if voices.spec:
        service = REGISTRY[voices.spec.provider].local_service
        if service and (service != "kokoro" or settings.kokoro_managed):
            services.append(service)
    print(
        json.dumps({**config.summary(), "local_services": services}, ensure_ascii=False)
    )
except (
    ValueError,
    ImportError,
    AttributeError,
    OSError,
    TypeError,
    yaml.YAMLError,
) as exc:
    # Pydantic and our own errors omit values; dependency errors identify only the package.
    print(f"Configuration check failed: {exc}", file=sys.stderr)
    sys.exit(1)
