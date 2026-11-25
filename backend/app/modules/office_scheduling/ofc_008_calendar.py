"""Bounded civil-time projections; no authority, persistence, or dispatch.

Callers pass the database clock. A projection is only an explanation of one
local-day slot: it is never evidence that a grant, Task, or receipt exists.
Windows are elapsed UTC seconds, half-open [scheduled_at, window_end).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule_clock_must_be_aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class CalendarRule:
    timezone_name: str
    local_time: str
    cadence: str
    weekdays: tuple[int, ...]
    fold_policy: str
    gap_policy: str
    window_seconds: int

    def __post_init__(self):
        if (not isinstance(self.timezone_name, str) or not 1 <= len(self.timezone_name) <= 100
                or self.timezone_name != self.timezone_name.strip()
                or self.timezone_name in ("localtime", "posixrules")):
            raise ValueError("schedule_timezone_invalid")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("schedule_timezone_unavailable") from None
        if (not isinstance(self.local_time, str)
                or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", self.local_time) is None):
            raise ValueError("schedule_local_time_invalid")
        if self.cadence not in ("daily", "weekly"):
            raise ValueError("schedule_cadence_invalid")
        if (not isinstance(self.weekdays, tuple) or len(self.weekdays) > 7
                or any(type(day) is not int or not 0 <= day <= 6 for day in self.weekdays)
                or len(set(self.weekdays)) != len(self.weekdays)
                or (self.cadence == "daily" and self.weekdays)
                or (self.cadence == "weekly" and not self.weekdays)):
            raise ValueError("schedule_weekdays_invalid")
        if self.fold_policy not in ("first", "second") or self.gap_policy != "skip":
            raise ValueError("schedule_dst_policy_invalid")
        if type(self.window_seconds) is not int or not 1 <= self.window_seconds <= 86400:
            raise ValueError("schedule_window_invalid")


@dataclass(frozen=True)
class CalendarSlot:
    local_date: date
    scheduled_at: datetime | None
    window_end: datetime | None
    timezone_name: str
    fold: int | None
    decision: str


def slot_for_date(rule: CalendarRule, local_date: date, *, now: datetime) -> CalendarSlot:
    """Explain a single daily identity, choosing one explicit fold or skip gap."""
    if not isinstance(rule, CalendarRule) or type(local_date) is not date:
        raise ValueError("schedule_calendar_input_invalid")
    current = _aware_utc(now)
    empty = dict(local_date=local_date, scheduled_at=None, window_end=None,
                 timezone_name=rule.timezone_name, fold=None)
    if rule.cadence == "weekly" and local_date.weekday() not in rule.weekdays:
        return CalendarSlot(**empty, decision="not_scheduled")
    zone = ZoneInfo(rule.timezone_name)
    hour, minute = map(int, rule.local_time.split(":"))
    civil = datetime.combine(local_date, time(hour, minute))
    candidates = {}
    for fold in (0, 1):
        instant = civil.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        restored = instant.astimezone(zone)
        # Merely attaching tzinfo accepts nonexistent local times. Only a
        # round-trip identifies a real instant, including non-hour changes.
        if restored.replace(tzinfo=None) == civil:
            candidates.setdefault(instant, restored.fold)
    if not candidates:
        return CalendarSlot(**empty, decision="gap_skipped")
    instants = sorted(candidates)
    scheduled = instants[0] if rule.fold_policy == "first" else instants[-1]
    end = scheduled + timedelta(seconds=rule.window_seconds)
    decision = "future" if current < scheduled else "due" if current < end else "expired"
    return CalendarSlot(local_date=local_date, scheduled_at=scheduled, window_end=end,
        timezone_name=rule.timezone_name, fold=candidates[scheduled], decision=decision)


def candidate_slots(rule: CalendarRule, *, now: datetime) -> tuple[CalendarSlot, ...]:
    """Project yesterday/today plus any still-due slot across a date jump.

    Python tzinfo offsets are strictly less than 24h in magnitude. Therefore
    a real instant in [now-window, now] can only have a civil date within
    [now-window-24h, now+24h]. With window <=24h this costs at most four
    dates, even across Samoa's skipped day. No historical catch-up is done.
    The service must still apply grant dates, last-check, pause and quota.
    """
    if not isinstance(rule, CalendarRule):
        raise ValueError("schedule_calendar_input_invalid")
    try:
        current = _aware_utc(now)
        today = current.astimezone(ZoneInfo(rule.timezone_name)).date()
        usual = {today - timedelta(days=1), today}
        first = (current - timedelta(seconds=rule.window_seconds) - timedelta(days=1)).date()
        last = (current + timedelta(days=1)).date()
        dates = usual | {first + timedelta(days=offset)
                         for offset in range((last - first).days + 1)}
        rows = [slot_for_date(rule, day, now=current) for day in sorted(dates)]
    except OverflowError:
        raise ValueError("schedule_clock_outside_supported_calendar") from None
    return tuple(row for row in rows if row.local_date in usual or row.decision == "due")
