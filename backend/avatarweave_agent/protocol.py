"""One versioned, validated AvatarWeave command contract."""

from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

COMMAND_TOPIC = "avatarweave.command.v1"
SESSION_KEY = "avatarweave_session"
MAX_METADATA_BYTES = 4096
MAX_SAFE_INTEGER = 9007199254740991


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Versioned(StrictModel):
    version: Literal[1]

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value


class CommandBase(Versioned):
    id: str = Field(min_length=1, max_length=128)


class MotionFields(StrictModel):
    type: Literal["motion"]
    name: str = Field(max_length=128)
    index: int = Field(ge=0, le=MAX_SAFE_INTEGER)


class ExpressionFields(StrictModel):
    type: Literal["expression"]
    name: str = Field(min_length=1, max_length=128)


class MotionCommand(CommandBase, MotionFields):
    pass


class ExpressionCommand(CommandBase, ExpressionFields):
    pass


Command = Annotated[MotionCommand | ExpressionCommand, Field(discriminator="type")]
COMMANDS = TypeAdapter(Command)


def make_command(action: dict):
    return COMMANDS.validate_python({"version": 1, "id": uuid4().hex, **action})
