"""Single strict JSON metadata envelope for API-issued and directly dispatched jobs."""

import json

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .avatars import get_avatar
from .protocol import MAX_METADATA_BYTES, Versioned
from .voices import voice_service


class SessionOptions(Versioned):
    avatar_id: str | None = Field(default=None, min_length=1, max_length=128)
    tts_voice: str | None = Field(default=None, min_length=1, max_length=128)

    def validate_choices(self, config):
        get_avatar(self.avatar_id)
        if self.tts_voice:
            voice_service(config).select(self.tts_voice)
        return self


class SessionMetadata(BaseModel):
    # The envelope is not separately versioned: its version belongs to session options.
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    avatarweave_session: SessionOptions = Field(
        default_factory=lambda: SessionOptions(version=1)
    )
    context: dict[str, JsonValue] = Field(default_factory=dict)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Metadata must contain finite JSON values")


def read_metadata(metadata: str) -> SessionMetadata:
    if not isinstance(metadata, str) or len(metadata.encode()) > MAX_METADATA_BYTES:
        raise ValueError("Agent metadata must be a JSON string of at most 4096 bytes")
    if not metadata:
        return SessionMetadata()
    try:
        value = json.loads(
            metadata, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Agent metadata must be valid JSON") from exc
    return SessionMetadata.model_validate(value)


def read_options(metadata: str) -> SessionOptions:
    return read_metadata(metadata).avatarweave_session


def merge_options(
    metadata: str, *, avatar_id: str | None, tts_voice: str | None, config
):
    document = read_metadata(metadata)
    supplied = document.avatarweave_session.model_dump()
    if avatar_id is not None:
        supplied["avatar_id"] = avatar_id
    if tts_voice is not None:
        supplied["tts_voice"] = tts_voice
    options = SessionOptions.model_validate(supplied).validate_choices(config)
    options.avatar_id, _ = get_avatar(options.avatar_id)
    document.avatarweave_session = options
    result = document.model_dump_json(exclude_none=True)
    if len(result.encode()) > MAX_METADATA_BYTES:
        raise ValueError("Agent metadata too large")
    return result
