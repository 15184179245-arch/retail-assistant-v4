"""Lexical UTC-offset component guard for opt-in datetime inputs."""
from datetime import datetime
import re


_UTC_OFFSET_COMPONENTS = re.compile(
    r"(?P<hour>[0-9]{2})(?:(?P<separator>:?)(?P<minute>[0-9]{2})"
    r"(?:(?P=separator)(?P<second>[0-9]{2}))?)?(?=[.,]|\Z)"
)


def parse_bounded_iso_offset(value: object, *, invalid_offset: str) -> object:
    """Preserve fromisoformat semantics except normalization of bad components."""
    if not isinstance(value, str):
        return value
    wire = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(wire)
    if parsed.utcoffset() is not None:
        # Only an already parsed offset qualifies. The last sign is its
        # separator, even when the calendar/time separator also has a sign.
        start = max(wire.rfind("+"), wire.rfind("-")) + 1
        offset = _UTC_OFFSET_COMPONENTS.match(wire, start) if start else None
        if (offset is None or int(offset["hour"]) > 23
                or int(offset["minute"] or 0) > 59 or int(offset["second"] or 0) > 59):
            raise ValueError(invalid_offset)
    return parsed
