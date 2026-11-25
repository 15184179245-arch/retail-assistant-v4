"""Inert compatibility API for the retired Host code-creation prototype."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.tools.sandbox import LEGACY_CODE_DISABLED


class CreateToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool_key: str = Field(
        min_length=2,
        max_length=80,
        pattern=r"^[a-zA-Z0-9_.-]+$",
        description="Unique identifier for the new tool, e.g. 'custom.calculate_pipe_discount'",
    )
    description: str = Field(
        min_length=5,
        max_length=1000,
        description="Clear description of what this tool does and when to call it",
    )
    python_code: str = Field(
        min_length=10,
        max_length=10000,
        description="Executable Python code defining a function `def run(arguments: dict) -> dict:`",
    )
    test_arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Sample arguments to run a test execution to verify correctness",
    )


class CreateToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    tool_key: str
    version: str = "1"
    message: str
    test_result: Any | None = None
    error: str | None = None


# In-memory registry for dynamically created tools per runtime process
# Keyed by (store_id, tool_key, version)
DYNAMIC_STORE_TOOLS: dict[tuple[str, str, str], dict[str, Any]] = {}


def create_dynamic_tool(
    session: Session,
    requester: AuthenticatedSubject,
    payload: CreateToolInput,
) -> CreateToolOutput:
    # No test execution or registration is permitted in the Host process.
    return CreateToolOutput(
        success=False, tool_key=payload.tool_key, version="1",
        message="Legacy code creation is unavailable; isolated execution and reviewed publication are pending.",
        error=LEGACY_CODE_DISABLED,
    )
