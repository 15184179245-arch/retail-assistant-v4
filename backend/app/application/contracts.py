"""Internal command references; none of these values grants authorization."""
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.security import AuthenticatedSubject


class CommandIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: str = Field(min_length=1, max_length=100)
    version: Literal["1"] = "1"
    payload: dict[str, Any] = Field(default_factory=dict)
    target_id: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


@dataclass(frozen=True)
class ExecutionContext:
    """Server-owned, transaction-bound provenance, never a serialized principal."""
    actor: AuthenticatedSubject
    origin: Literal["employee_http", "person_host", "agent_tool"]
    scope_epoch: int
    session: Any = field(repr=False, compare=False)
    root: Any = field(repr=False, compare=False)
    seal: Any = field(repr=False, compare=False)
    agent: AuthenticatedSubject | None = None


@dataclass(frozen=True)
class CommandReceipt:
    """Reference the original domain record/digest; create no second receipt."""
    domain: str
    object_id: str
    namespace: str
    idempotency_key: str | None
    receipt_id: str | None = None
    payload_digest: str | None = None
    source_version: int | None = None


@dataclass(frozen=True)
class CommandResult:
    data: Any
    receipt: CommandReceipt
    context: ExecutionContext
