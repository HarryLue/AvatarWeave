"""Independent v2 action envelope and v1 renderer receipts; v1 remains unchanged."""

from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ..protocol import ExpressionFields, MotionFields
from .contracts import Contract, Identifier

COMMAND_TOPIC_V2 = "avatarweave.command.v2"
RECEIPT_TOPIC = "avatarweave.receipt.v1"
HELLO_TOPIC = "avatarweave.hello.v1"
BOOTSTRAP_TOPIC = "avatarweave.bootstrap.v1"


class ClientHello(Contract):
    version: Literal[1]
    session_id: Identifier
    nonce: Identifier

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("Version must be an integer")
        return value


class SessionBootstrap(Contract):
    version: Literal[1]
    session_id: Identifier
    nonce: Identifier
    owner_epoch: int = Field(ge=1)
    generation_sequence: int = Field(ge=0)
    sequence_floor: int = Field(ge=0)
    expires_at: AwareDatetime


class ActionEnvelope(Contract):
    version: Literal[2]
    command_id: Identifier
    connection_nonce: Identifier
    session_id: Identifier
    generation_id: Identifier
    owner_epoch: int = Field(ge=1)
    generation_sequence: int = Field(ge=1)
    sequence: int = Field(ge=0)
    expires_at: AwareDatetime
    operation: Literal["execute", "cancel"]
    action: MotionFields | ExpressionFields | None

    @model_validator(mode="after")
    def action_matches_operation(self):
        if (self.operation == "execute") != (self.action is not None):
            raise ValueError("Execute requires an action; cancel has none")
        return self


class ActionReceipt(Contract):
    version: Literal[1]
    command_id: Identifier
    session_id: Identifier
    generation_id: Identifier
    client_sequence: int = Field(ge=0)
    state: Literal["accepted", "started", "completed", "cancelled", "failed", "unknown"]
    observed_at: AwareDatetime

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("Version must be an integer")
        return value
