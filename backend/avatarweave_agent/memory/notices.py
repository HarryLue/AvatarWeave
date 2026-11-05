from typing import Literal

from .contracts import Contract, Identifier


class MemoryNotice(Contract):
    version: Literal[1]
    session_id: Identifier
    generation_id: Identifier | None
    state: Literal["source_saved", "temporary", "failed", "cancelled"]
    source_saved: bool
    code: str | None
