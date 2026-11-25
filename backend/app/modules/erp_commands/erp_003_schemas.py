"""ERP-003 strict local-sandbox API data. No URL, SQL or credentials in input."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from backend.app.core.errors import ApiEnvelope

Identifier = Annotated[str, Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BatchState = Literal["prepared", "dispatched", "unknown", "partial", "sandbox_verified", "failed"]
LineState = Literal["pending", "unknown", "sandbox_verified", "failed"]


class SandboxDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SandboxCommandRef(SandboxDTO):
    command_id: Identifier
    request_digest: Digest


class SandboxQueueInput(SandboxDTO):
    adapter_id: Literal["synthetic.erp.v1"] = "synthetic.erp.v1"
    commands: list[SandboxCommandRef] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def commands_are_unique(self):
        if len({item.command_id for item in self.commands}) != len(self.commands):
            raise ValueError("each staged command may occur only once")
        return self


class SandboxLineReceipt(SandboxDTO):
    id: Identifier
    command_id: Identifier
    intent_id: Identifier
    line_no: int = Field(ge=1, le=50)
    input_digest: Digest
    state: LineState
    external_ref: str | None = Field(default=None, min_length=1, max_length=200)
    receipt_digest: Digest | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    version: int = Field(gt=0)

    @model_validator(mode="after")
    def verified_requires_evidence(self):
        if self.state == "sandbox_verified" and (self.external_ref is None or self.receipt_digest is None):
            raise ValueError("sandbox verification requires a line receipt")
        return self


class SandboxExecutionReceipt(SandboxDTO):
    id: Identifier
    protocol_version: Literal[1] = 1
    mode: Literal["synthetic"] = "synthetic"
    adapter_id: Literal["synthetic.erp.v1"] = "synthetic.erp.v1"
    state: BatchState
    input_digest: Digest
    version: int = Field(gt=0)
    lines: list[SandboxLineReceipt] = Field(min_length=1, max_length=50)
    created_at: datetime
    updated_at: datetime
    dispatched_at: datetime | None = None
    replayed: bool = False

    @field_validator("protocol_version", mode="before")
    @classmethod
    def version_is_exact(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported sandbox protocol")
        return value

    @model_validator(mode="after")
    def batch_matches_lines(self):
        if (len({line.id for line in self.lines}) != len(self.lines)
                or len({line.intent_id for line in self.lines}) != len(self.lines)
                or len({line.command_id for line in self.lines}) != len(self.lines)
                or {line.line_no for line in self.lines} != set(range(1, len(self.lines) + 1))):
            raise ValueError("duplicate or missing sandbox lines")
        states = {line.state for line in self.lines}
        if self.state in ("prepared", "dispatched") and states != {"pending"}:
            raise ValueError("unsent or inflight lines cannot have final outcomes")
        if self.state == "unknown" and states != {"unknown"}:
            raise ValueError("unknown batch cannot invent a known line outcome")
        if self.state == "partial" and (len(states) < 2 or not states.intersection({"failed", "sandbox_verified"})):
            raise ValueError("partial batch needs distinct observed line outcomes")
        if self.state == "sandbox_verified" and any(line.state != "sandbox_verified" for line in self.lines):
            raise ValueError("one line cannot verify the batch")
        if self.state == "failed" and any(line.state != "failed" for line in self.lines):
            raise ValueError("a failed batch requires all lines rejected")
        if self.state == "prepared" and self.dispatched_at is not None:
            raise ValueError("prepared batch cannot have been dispatched")
        if self.state == "dispatched" and self.dispatched_at is None:
            raise ValueError("dispatch intent needs its timestamp")
        return self


class SandboxExecutionEnvelope(ApiEnvelope):
    data: SandboxExecutionReceipt | None = None
