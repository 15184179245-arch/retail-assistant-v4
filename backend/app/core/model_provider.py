"""Vendor-neutral async contract for future model calls.

This module deliberately has no provider implementation, network client,
database access, SQL surface, or tool-execution capability. Returned text is
untrusted suggestion material; domain services must validate it and people
must confirm business facts before they are recorded or acted on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

_SCOPE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CallBudget:
    """Per-call token ceiling, independent of any vendor's billing units."""

    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for name, value in (
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Complete scope and resource context required for a model invocation."""

    organization_id: str
    store_id: str
    requester_id: str
    task_id: str
    purpose: str
    input_text: str = field(repr=False)
    budget: CallBudget
    deadline_at: datetime

    def __post_init__(self) -> None:
        for name in ("organization_id", "store_id", "requester_id", "task_id", "purpose"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SCOPE_TOKEN.fullmatch(value) is None:
                raise ValueError(f"{name} is required")
        if not isinstance(self.input_text, str) or not self.input_text.strip():
            raise ValueError("input_text is required")
        if not isinstance(self.budget, CallBudget):
            raise ValueError("budget must be a CallBudget")
        if not isinstance(self.deadline_at, datetime):
            raise ValueError("deadline_at must be a timezone-aware datetime")
        if self.deadline_at.utcoffset() is None:
            raise ValueError("deadline_at must include a UTC offset")
        if self.deadline_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ValueError("deadline_at must be in the future")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Untrusted suggestion text and usage only; never a business fact."""

    suggestion: str = field(repr=False)
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if not isinstance(self.suggestion, str):
            raise ValueError("suggestion must be text")
        for name, value in (("input_tokens", self.input_tokens), ("output_tokens", self.output_tokens)):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


class ModelProvider(Protocol):
    """Replaceable async interface; implementations must honor request scope/budget."""

    async def complete(self, request: ProviderRequest, /) -> ProviderResponse:
        """Return an untrusted candidate for deterministic review by the caller."""
