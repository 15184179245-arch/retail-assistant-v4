from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from backend.app.core.errors import ApiEnvelope


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    proposal_ref: str | None = Field(default=None, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)


class TaskSuggestion(TaskCreate):
    reviewer_id: str = Field(min_length=1, max_length=36)


class TaskManagedCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    reason: str = Field(min_length=1, max_length=4000)
    assignee_id: str | None = Field(default=None, min_length=1, max_length=36)
    due_at: datetime | None = None

    @model_validator(mode="after")
    def assignment_is_complete(self):
        if (self.assignee_id is None) != (self.due_at is None):
            raise ValueError("assignee_id and due_at must be supplied together")
        return self


class TaskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["accept", "reject", "defer", "request_info"]
    reason: str = Field(min_length=1, max_length=4000)
    supplement_ref: str | None = Field(default=None, min_length=1, max_length=200)


class TaskReason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=4000)


class TaskAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee_id: str = Field(min_length=1, max_length=36)
    due_at: datetime
    reason: str = Field(min_length=1, max_length=4000)


def _stored_task_time_json(value, handler):
    # Task writers normalize to UTC; SQLite's result type drops the offset.
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return handler(value)


class TaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    organization_id: str
    store_id: str
    created_by: str
    title: str
    description: str
    source: Literal["agent", "human", "schedule"]
    proposal_ref: str | None
    reviewer_id: str | None
    decision_state: Literal["pending", "accepted", "rejected", "deferred", "info_requested"]
    state: Literal["suggested", "accepted", "assigned", "in_progress", "verified", "closed"]
    accepted_by: str | None
    accepted_at: datetime | None
    assigned_by: str | None
    assigned_at: datetime | None
    assignee_id: str | None
    due_at: datetime | None
    started_by: str | None
    started_at: datetime | None
    verified_by: str | None
    verified_at: datetime | None
    verification_reason: str | None
    closed_by: str | None
    closed_at: datetime | None
    close_reason: str | None
    created_at: datetime

    _json_times = field_serializer(
        "accepted_at", "assigned_at", "due_at", "started_at", "verified_at", "closed_at", "created_at",
        mode="wrap", when_used="json",
    )(_stored_task_time_json)


class TaskEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    id: str
    organization_id: str
    store_id: str
    task_id: str
    actor_id: str
    from_state: str | None
    to_state: str
    decision_state: str | None
    responsible_id: str | None
    due_at: datetime | None
    reason: str
    occurred_at: datetime

    _json_times = field_serializer("due_at", "occurred_at", mode="wrap", when_used="json")(
        _stored_task_time_json,
    )


class TaskEnvelope(ApiEnvelope):
    data: TaskResponse | None


class TaskListEnvelope(ApiEnvelope):
    data: list[TaskResponse] | None


class TaskEventsEnvelope(ApiEnvelope):
    data: list[TaskEventResponse] | None
