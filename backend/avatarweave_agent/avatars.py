"""Validated server-owned avatar assets and semantic-to-model action mappings."""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import ROOT, settings
from .protocol import ExpressionFields, MotionFields, StrictModel


class LabeledAction(StrictModel):
    label: str = Field(min_length=1, max_length=128)

    def command(self):
        return self.model_dump(exclude={"label"})


class MotionAction(MotionFields, LabeledAction):
    pass


class ExpressionAction(ExpressionFields, LabeledAction):
    pass


Action = Annotated[MotionAction | ExpressionAction, Field(discriminator="type")]


class Assets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    core_url: str
    model_url: str
    motion_sync_url: str | None = None

    @model_validator(mode="after")
    def resource_urls(self):
        for value in (self.core_url, self.model_url, self.motion_sync_url):
            if value is not None and not (
                value.startswith("/")
                and not value.startswith("//")
                or value.startswith(("https://", "http://"))
            ):
                raise ValueError(
                    "Avatar resource URL must be an absolute path or HTTP(S) URL"
                )
        return self


class Avatar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    renderer: Literal["live2d"] = "live2d"
    assets: Assets
    draggable: bool = True
    actions: dict[str, Action] = Field(default_factory=dict)
    default_action: str | None = None

    @model_validator(mode="after")
    def valid_default(self):
        if self.default_action is not None and self.default_action not in self.actions:
            raise ValueError("default_action must reference a configured action")
        return self


class AvatarDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    default_avatar: str
    avatars: dict[str, Avatar]

    @model_validator(mode="after")
    def valid_default(self):
        if self.default_avatar not in self.avatars:
            raise ValueError("default_avatar must reference a configured avatar")
        return self


def load_avatars(path: Path | None = None) -> AvatarDocument:
    path = path or settings.avatar_config
    if not path.is_absolute():
        path = ROOT / path
    return AvatarDocument.model_validate(yaml.safe_load(path.read_text()))


def get_avatar(avatar_id: str | None = None):
    document = load_avatars()
    avatar_id = avatar_id or document.default_avatar
    if avatar_id not in document.avatars:
        raise ValueError("Unknown avatar_id")
    return avatar_id, document.avatars[avatar_id]
