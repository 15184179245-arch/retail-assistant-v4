"""Strict scheduling inputs. Parsing is neither authority nor dispatch."""
from datetime import date, datetime
import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints,
    field_validator, model_validator,
)

from backend.app.core.errors import ApiEnvelope
from backend.app.modules.office_scheduling.ofc_008_calendar import CalendarRule

Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=36,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, StringConstraints(strict=True, min_length=64, max_length=64,
    pattern=r"^[0-9a-f]{64}$")]
Version = Annotated[int, Field(strict=True, gt=0)]


def _text(value: str) -> str:
    if value != value.strip() or not value.strip():
        raise ValueError("schedule_text_must_be_nonblank_and_unpadded")
    return value


Reason = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000), AfterValidator(_text)]
Title = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200), AfterValidator(_text)]


class StrictInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")


class CalendarRuleInput(StrictInput):
    timezone_name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=100)]
    local_time: Annotated[str, StringConstraints(strict=True, pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
    cadence: Literal["daily", "weekly"]
    weekdays: tuple[Annotated[int, Field(strict=True, ge=0, le=6)], ...] = Field(max_length=7)
    fold_policy: Literal["first", "second"]
    gap_policy: Literal["skip"]
    window_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]

    @field_validator("weekdays", mode="before")
    @classmethod
    def json_weekdays(cls, value):
        # The wire is a JSON array; freeze it as a tuple without converting any
        # element or accepting sets/strings/unordered iterables.
        return tuple(value) if type(value) is list else value

    def to_calendar_rule(self) -> CalendarRule:
        return CalendarRule(**self.model_dump())

    @model_validator(mode="after")
    def valid_calendar(self):
        self.to_calendar_rule()
        return self


def _date(value):
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return date.fromisoformat(value)
    raise ValueError("schedule_date_must_be_iso_calendar_date")


def _timestamp(value):
    if type(value) is datetime:
        return value
    if isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value,
    ):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("schedule_timestamp_must_be_aware_iso")


class ScheduleTemplateInput(StrictInput):
    kind: Literal["task_template", "review_summary"]
    title: Title
    description: Reason
    rule: CalendarRuleInput
    start_date: date
    reviewer_id: Identifier

    @field_validator("start_date", mode="before")
    @classmethod
    def calendar_date(cls, value):
        return _date(value)


    @model_validator(mode="after")
    def bounded_summary_header(self):
        if self.kind == "review_summary" and len(self.description) > 1000:
            raise ValueError("schedule_summary_description_too_long")
        return self


class ScheduleCreateInput(ScheduleTemplateInput):
    reason: Reason


class ScheduleReplaceInput(ScheduleTemplateInput):
    expected_version: Version
    expected_template_digest: Digest
    reason: Reason


class ScheduleApproveInput(StrictInput):
    expected_version: Version
    expected_template_digest: Digest
    machine_id: Identifier
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    max_occurrences: Annotated[int, Field(strict=True, ge=1, le=100000)]
    reason: Reason

    @field_validator("valid_from", "expires_at", mode="before")
    @classmethod
    def timestamps(cls, value):
        return _timestamp(value)

    @model_validator(mode="after")
    def finite_interval(self):
        if self.expires_at <= self.valid_from:
            raise ValueError("schedule_authorization_interval_invalid")
        return self


class SchedulePauseInput(StrictInput):
    expected_version: Version
    expected_template_digest: Digest
    reason: Reason


# --------------------------------------------------------------------------- #
# Frozen response DTOs (B2b). Parsing/admissibility only: no grant, no
# scheduling, no dispatch, and no substitute for the service's own rechecks.
# --------------------------------------------------------------------------- #
NonNegative = Annotated[int, Field(strict=True, ge=0)]
UppercaseToken = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")]
Cursor = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256),
    AfterValidator(_text)]

SCHEDULE_KINDS = ("task_template", "review_summary")
SCHEDULE_STATES = ("draft", "active", "paused", "needs_reapproval")
GRANT_STATES = ("valid", "not_yet_valid", "expired", "exhausted", "needs_reapproval")
OCCURRENCE_STATES = ("queued", "materialized", "skipped", "cancelled", "needs_reapproval")
RECEIPT_OPERATIONS = ("create", "replace", "approve", "pause", "materialize",
                      "skip", "cancel", "invalidate", "checkpoint")


def _optional_date(value):
    return None if value is None else _date(value)


def _optional_timestamp(value):
    return None if value is None else _timestamp(value)


class ScheduleSummary(StrictInput):
    id: Identifier
    kind: Literal["task_template", "review_summary"]
    title: Title
    reviewer_id: Identifier
    template_digest: Digest
    state: Literal["draft", "active", "paused", "needs_reapproval"]
    version: Version
    start_date: date
    created_by: Identifier
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_checked_at: AwareDatetime | None = None

    @field_validator("start_date", mode="before")
    @classmethod
    def _date_field(cls, value):
        return _date(value)

    @field_validator("created_at", "updated_at", "last_checked_at", mode="before")
    @classmethod
    def _time_field(cls, value):
        return _optional_timestamp(value)

    @model_validator(mode="after")
    def _ordered_summary_times(self):
        if self.updated_at < self.created_at:
            raise ValueError("schedule_updated_before_created")
        if self.last_checked_at is not None and self.last_checked_at < self.created_at:
            raise ValueError("schedule_last_checked_before_created")
        return self


class ScheduleGrantView(StrictInput):
    id: Identifier
    schedule_id: Identifier
    schedule_version: Version
    template_digest: Digest
    template_snapshot: ScheduleTemplateInput
    reviewer_id: Identifier
    machine_id: Identifier
    approved_by: Identifier
    authorization_epoch: Version
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    approved_at: AwareDatetime
    max_occurrences: Annotated[int, Field(strict=True, ge=1, le=100000)]
    used_occurrences: NonNegative
    reserved_occurrences: NonNegative
    remaining_occurrences: NonNegative
    authorization_state: Literal["valid", "not_yet_valid", "expired", "exhausted",
                                 "needs_reapproval"]

    @field_validator("valid_from", "expires_at", "approved_at", mode="before")
    @classmethod
    def _time_field(cls, value):
        return _timestamp(value)

    @model_validator(mode="after")
    def _grant_is_consistent(self):
        if self.expires_at <= self.valid_from:
            raise ValueError("schedule_authorization_interval_invalid")
        if self.valid_from < self.approved_at:
            raise ValueError("schedule_authorization_before_approval")
        if self.template_snapshot.reviewer_id != self.reviewer_id:
            raise ValueError("schedule_grant_snapshot_reviewer_mismatch")
        if self.remaining_occurrences != max(
                0, self.max_occurrences - self.used_occurrences - self.reserved_occurrences):
            raise ValueError("schedule_grant_remaining_inconsistent")
        if (self.authorization_state == "valid"
                and self.used_occurrences + self.reserved_occurrences > self.max_occurrences):
            raise ValueError("schedule_grant_quota_exceeded")
        return self


class ScheduleReferences(StrictInput):
    template_digest: Digest | None = None
    reason_digest: Digest | None = None
    source_digest: Digest | None = None
    decision_code: UppercaseToken | None = None
    result_schedule_version: Version | None = None
    current_epoch: Version | None = None
    local_date: date | None = None
    skipped_from: date | None = None
    skipped_to: date | None = None
    skipped_days: NonNegative | None = None
    total_count: NonNegative | None = None
    fact_ids: tuple[Identifier, ...] | None = Field(default=None, max_length=10)

    @field_validator("fact_ids", mode="before")
    @classmethod
    def _fact_ids_from_json(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value
    outbox_event_id: Identifier | None = None
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None

    @field_validator("local_date", "skipped_from", "skipped_to", mode="before")
    @classmethod
    def _date_field(cls, value):
        return _optional_date(value)

    @field_validator("window_start", "window_end", mode="before")
    @classmethod
    def _time_field(cls, value):
        return _optional_timestamp(value)

    @model_validator(mode="after")
    def _references_are_consistent(self):
        if (self.skipped_from is None) != (self.skipped_to is None):
            raise ValueError("schedule_skipped_range_must_be_paired")
        if self.skipped_from is not None and self.skipped_to < self.skipped_from:
            raise ValueError("schedule_skipped_range_inverted")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("schedule_window_must_be_paired")
        if self.window_start is not None and self.window_end <= self.window_start:
            raise ValueError("schedule_window_interval_invalid")
        if self.fact_ids is not None and len(set(self.fact_ids)) != len(self.fact_ids):
            raise ValueError("schedule_fact_ids_must_be_unique")
        return self


class ScheduleReceiptView(StrictInput):
    id: Identifier
    schedule_id: Identifier
    schedule_version: Version
    operation: Literal["create", "replace", "approve", "pause", "materialize",
                       "skip", "cancel", "invalidate", "checkpoint"]
    request_digest: Digest
    actor_id: Identifier
    references: ScheduleReferences
    created_at: AwareDatetime
    occurrence_id: Identifier | None = None
    grant_id: Identifier | None = None
    task_id: Identifier | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _time_field(cls, value):
        return _timestamp(value)

    @model_validator(mode="after")
    def _receipt_binding(self):
        if self.operation == "materialize":
            if self.occurrence_id is None or self.grant_id is None or self.task_id is None:
                raise ValueError("schedule_materialize_receipt_incomplete")
        elif self.task_id is not None:
            raise ValueError("schedule_receipt_task_must_be_empty")
        return self


class ScheduleView(ScheduleSummary):
    description: Reason
    rule: CalendarRuleInput
    authorization: ScheduleGrantView | None = None
    blocking_code: UppercaseToken | None = None
    command_receipt: ScheduleReceiptView | None = None
    replayed: Annotated[bool, Field(strict=True)]
    effect: Literal["local_suggestion_only"] = "local_suggestion_only"
    mode: Literal["human_supervised"] = "human_supervised"

    @model_validator(mode="after")
    def _view_is_consistent(self):
        if self.kind == "review_summary" and len(self.description) > 1000:
            raise ValueError("schedule_summary_description_too_long")
        if self.authorization is not None:
            if self.authorization.schedule_id != self.id:
                raise ValueError("schedule_grant_schedule_mismatch")
            if self.authorization.schedule_version > self.version:
                raise ValueError("schedule_grant_newer_than_schedule")
        if self.command_receipt is not None:
            if self.command_receipt.schedule_id != self.id:
                raise ValueError("schedule_receipt_schedule_mismatch")
            if self.command_receipt.schedule_version > self.version:
                raise ValueError("schedule_receipt_newer_than_schedule")
        if (self.state == "active" and self.authorization is None
                and self.blocking_code is None):
            raise ValueError("schedule_active_without_authorization_needs_blocking_code")
        return self


class ScheduleOccurrenceView(StrictInput):
    id: Identifier
    schedule_id: Identifier
    schedule_version: Version
    grant_id: Identifier
    local_date: date
    scheduled_at: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    state: Literal["queued", "materialized", "skipped", "cancelled", "needs_reapproval"]
    decision_code: UppercaseToken
    version: Version
    task_id: Identifier | None = None
    source_digest: Digest | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("local_date", mode="before")
    @classmethod
    def _date_field(cls, value):
        return _date(value)

    @field_validator("scheduled_at", "window_end", "created_at", "updated_at",
                     mode="before")
    @classmethod
    def _time_field(cls, value):
        return _optional_timestamp(value)

    @model_validator(mode="after")
    def _occurrence_is_consistent(self):
        if self.updated_at < self.created_at:
            raise ValueError("schedule_occurrence_updated_before_created")
        if (self.scheduled_at is None) != (self.window_end is None):
            raise ValueError("schedule_window_must_be_paired")
        if self.scheduled_at is not None and self.window_end <= self.scheduled_at:
            raise ValueError("schedule_window_interval_invalid")
        if self.state == "queued" and self.scheduled_at is None:
            raise ValueError("schedule_queued_occurrence_needs_window")
        if self.state == "materialized":
            if self.task_id is None or self.source_digest is None or self.scheduled_at is None:
                raise ValueError("schedule_materialized_occurrence_incomplete")
        elif self.task_id is not None:
            raise ValueError("schedule_occurrence_task_must_be_empty")
        return self


class SchedulePage(StrictInput):
    items: list[ScheduleSummary] = Field(max_length=100)
    next_cursor: Cursor | None = None

    @model_validator(mode="after")
    def _rows_are_unique(self):
        if len({row.id for row in self.items}) != len(self.items):
            raise ValueError("schedule_page_ids_must_be_unique")
        return self


class ScheduleOccurrencePage(StrictInput):
    schedule_id: Identifier
    items: list[ScheduleOccurrenceView] = Field(max_length=100)
    next_cursor: Cursor | None = None

    @model_validator(mode="after")
    def _rows_bind_to_schedule(self):
        if len({row.id for row in self.items}) != len(self.items):
            raise ValueError("schedule_page_ids_must_be_unique")
        if any(row.schedule_id != self.schedule_id for row in self.items):
            raise ValueError("schedule_occurrence_page_schedule_mismatch")
        return self


class ScheduleReceiptPage(StrictInput):
    schedule_id: Identifier
    items: list[ScheduleReceiptView] = Field(max_length=100)
    next_cursor: Cursor | None = None

    @model_validator(mode="after")
    def _rows_bind_to_schedule(self):
        if len({row.id for row in self.items}) != len(self.items):
            raise ValueError("schedule_page_ids_must_be_unique")
        if any(row.schedule_id != self.schedule_id for row in self.items):
            raise ValueError("schedule_receipt_page_schedule_mismatch")
        return self


class ScheduleCommandStatus(StrictInput):
    status: Literal["found", "not_found"]
    schedule: ScheduleView | None = None

    @model_validator(mode="after")
    def _status_is_consistent(self):
        if self.status == "found":
            if self.schedule is None or self.schedule.command_receipt is None:
                raise ValueError("schedule_command_found_requires_receipt")
        elif self.schedule is not None:
            raise ValueError("schedule_command_not_found_with_schedule")
        return self


class _ScheduleEnvelope(ApiEnvelope):
    success: Annotated[bool, Field(strict=True)]

    @model_validator(mode="after")
    def _envelope_is_consistent(self):
        if self.success:
            if self.data is None or self.code != "OK" or self.error is not None:
                raise ValueError("schedule_envelope_success_shape_invalid")
        elif self.data is not None or not self.error or self.code == "OK":
            raise ValueError("schedule_envelope_failure_shape_invalid")
        return self


class ScheduleEnvelope(_ScheduleEnvelope):
    data: ScheduleView | None


class ScheduleListEnvelope(_ScheduleEnvelope):
    data: SchedulePage | None


class ScheduleOccurrencePageEnvelope(_ScheduleEnvelope):
    data: ScheduleOccurrencePage | None


class ScheduleReceiptPageEnvelope(_ScheduleEnvelope):
    data: ScheduleReceiptPage | None


class ScheduleCommandStatusEnvelope(_ScheduleEnvelope):
    data: ScheduleCommandStatus | None
