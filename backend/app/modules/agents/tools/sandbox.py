"""Inert compatibility DTO/helper for the retired in-process Python prototype."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject


class PythonExecInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(min_length=1, max_length=20000, description="Python script to execute in sandbox")
    timeout_seconds: int = Field(default=5, ge=1, le=10, description="Execution timeout in seconds")


class PythonExecOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    stdout: str
    result: Any | None = None
    error: str | None = None
    elapsed_ms: int


# In-process Python cannot safely isolate Host objects. This compatibility
# helper is intentionally inert until AGT-033 supplies a real OS boundary.
LEGACY_CODE_DISABLED = "tool_legacy_code_execution_disabled"


def execute_python_sandbox(
    session: Session,
    requester: AuthenticatedSubject,
    payload: PythonExecInput,
) -> PythonExecOutput:
    """Retain the historical result shape, never evaluate caller code."""
    return PythonExecOutput(success=False, stdout="", result=None,
                            error=LEGACY_CODE_DISABLED, elapsed_ms=0)
