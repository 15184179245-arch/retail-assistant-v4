"""Allowlisted structured application logging.

Free-form messages and sensitive payload fields are intentionally not accepted.
Use stable event/error codes and correlation identifiers only.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Final

_LOGGER_NAME: Final = "retail_smart_ops"
_TOKEN_VALUE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_STRING_FIELDS: Final = frozenset(
    {
        "organization_id",
        "store_id",
        "actor_id",
        "task_id",
        "request_id",
        "run_id",
        "tool_call_id",
        "receipt_ref",
        "observed_at",
        "purpose",
        "action",
        "outcome",
        "error_code",
    }
)
_INTEGER_FIELDS: Final = frozenset({"duration_ms", "http_status"})
_ALLOWED_FIELDS: Final = _STRING_FIELDS | _INTEGER_FIELDS
_ALLOWED_LEVELS: Final = frozenset({logging.INFO, logging.WARNING, logging.ERROR})


def emit_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Emit one JSON event after strict field and value validation.

    This API accepts codes/IDs, not prompts, tokens, names, arbitrary messages,
    customer details, exception text, or model output. Rejected fields are not
    copied into an exception or a log record.
    """
    if not isinstance(event, str) or _TOKEN_VALUE.fullmatch(event) is None:
        raise ValueError("event must be a short stable code")
    if type(level) is not int or level not in _ALLOWED_LEVELS:
        raise ValueError("level must be INFO, WARNING, or ERROR")

    safe_fields: dict[str, str | int] = {}
    for name, value in fields.items():
        if name not in _ALLOWED_FIELDS:
            raise ValueError(f"field is not allowed for structured logs: {name}")
        if name in _STRING_FIELDS:
            if not isinstance(value, str) or _TOKEN_VALUE.fullmatch(value) is None:
                raise ValueError(f"{name} must be a short stable code or identifier")
            safe_fields[name] = value
        else:
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
            if name == "http_status" and not 100 <= value <= 599:
                raise ValueError("http_status must be between 100 and 599")
            safe_fields[name] = value

    record = {"event": event, **safe_fields}
    logging.getLogger(_LOGGER_NAME).log(level, json.dumps(record, sort_keys=True, separators=(",", ":")))

