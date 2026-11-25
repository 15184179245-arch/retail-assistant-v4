from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, field_validator, model_serializer, model_validator


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    WAITING_FOR_EXTERNAL = "waiting_for_external"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class StepKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    CHECKPOINT = "checkpoint"


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_POSITIVE_INT = Field(strict=True, gt=0, le=2_147_483_647)
_USAGE_INT = Field(strict=True, ge=0, le=9_223_372_036_854_775_807)


def _validate_identifier(value: str, max_length: int) -> str:
    if not 1 <= len(value) <= max_length or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid identifier")
    return value


def _normalize_aware_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc)


class FrozenDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunCreate(FrozenDTO):
    id: str = Field(max_length=36)
    organization_id: str = Field(max_length=36)
    store_id: str = Field(max_length=36)
    requester_id: str = Field(max_length=36)
    task_id: str | None = Field(default=None, max_length=36)
    agent_key: str = Field(max_length=120)
    agent_version: str = Field(max_length=120)
    provider_key: str = Field(max_length=120)
    model: str = Field(min_length=1, max_length=200, pattern=r"^[\x21-\x7e]+$")
    prompt_version: str = Field(max_length=120)
    toolset_version: str = Field(max_length=120)
    input_ref: str | None = Field(default=None, max_length=200)
    max_input_tokens: int = _POSITIVE_INT
    max_output_tokens: int = _POSITIVE_INT
    max_model_calls: int = _POSITIVE_INT
    max_tool_calls: int = _POSITIVE_INT
    max_steps: int = _POSITIVE_INT
    deadline_at: datetime

    @field_validator("id", "organization_id", "store_id", "requester_id", "task_id")
    @classmethod
    def validate_ids(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value, 36)

    @field_validator("agent_key", "agent_version", "provider_key", "prompt_version", "toolset_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _validate_identifier(value, 120)

    @field_validator("input_ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value, 200)

    @field_validator("deadline_at", mode="before")
    @classmethod
    def validate_deadline(cls, value: datetime | str) -> datetime:
        return _normalize_aware_datetime(value)


class ToolCallCreate(FrozenDTO):
    id: str = Field(max_length=36)
    tool_key: str = Field(max_length=120)
    tool_version: str = Field(max_length=120)
    idempotency_key: str = Field(max_length=200)
    input_digest: str = Field(min_length=64, max_length=64, pattern=r"[0-9a-f]{64}")

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, 36)

    @field_validator("tool_key", "tool_version")
    @classmethod
    def validate_tool_identifier(cls, value: str) -> str:
        return _validate_identifier(value, 120)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        return _validate_identifier(value, 200)


class StepCreate(FrozenDTO):
    id: str = Field(max_length=36)
    organization_id: str = Field(max_length=36)
    store_id: str = Field(max_length=36)
    run_id: str = Field(max_length=36)
    sequence_no: int = _POSITIVE_INT
    kind: StepKind
    input_ref: str | None = Field(default=None, max_length=200)
    tool_call: ToolCallCreate | None = None

    @field_validator("id", "organization_id", "store_id", "run_id")
    @classmethod
    def validate_ids(cls, value: str) -> str:
        return _validate_identifier(value, 36)

    @field_validator("input_ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value, 200)

    @model_validator(mode="after")
    def tool_call_matches_kind(self) -> "StepCreate":
        if (self.kind is StepKind.TOOL) != (self.tool_call is not None):
            raise ValueError("tool_call is required only for tool steps")
        return self


class StepTransition(FrozenDTO):
    to_state: StepState
    input_tokens: int | None = Field(default=None, strict=True, ge=0, le=9_223_372_036_854_775_807)
    output_tokens: int | None = Field(default=None, strict=True, ge=0, le=9_223_372_036_854_775_807)
    output_ref: str | None = Field(default=None, max_length=200)
    error_code: str | None = Field(default=None, max_length=120)

    @field_validator("output_ref")
    @classmethod
    def validate_output_ref(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value, 200)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value, 120)

    @model_validator(mode="after")
    def validate_transition_payload(self) -> "StepTransition":
        if self.to_state in (StepState.PENDING, StepState.RUNNING):
            if any(value is not None for value in (self.input_tokens, self.output_tokens, self.output_ref, self.error_code)):
                raise ValueError("nonterminal transition cannot include result fields")
        if self.to_state is StepState.SUCCEEDED and self.error_code is not None:
            raise ValueError("succeeded transition cannot include error_code")
        if self.to_state in (StepState.FAILED, StepState.UNKNOWN) and self.error_code is None:
            raise ValueError("failed and unknown transitions require error_code")
        return self


class ConfirmationDecision(FrozenDTO):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    decision: Literal["approve", "reject"]
    input_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=500)
    run_version: int = Field(strict=True, gt=0)
    step_version: int = Field(strict=True, gt=0)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("confirmation reason is required")
        return value.strip()


class AnswerCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["knowledge", "catalog", "memory", "guanjia_observation", "guanjia_category", "research"]
    object_id: str
    version_ref: str
    version: str
    label: str
    source_ref: str | None
    observed_at: datetime | None


class AgentExecutionPhase(StrEnum):
    """AGT-020 public execution phases; inflight values are derived, not stored."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    MODEL_INFLIGHT = "model_inflight"
    WAITING_FOR_HUMAN = "waiting_for_human"
    TOOL_INFLIGHT = "tool_inflight"
    RECONCILING = "reconciling"
    TERMINAL = "terminal"


class AgentCancelResolution(StrEnum):
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    CANCELLED = "cancelled"
    TOO_LATE = "too_late"
    UNKNOWN = "unknown"


class AgentEffectStatus(StrEnum):
    NONE = "none"
    LOCAL_TASK_COMMITTED = "local_task_committed"
    EXTERNAL_UNKNOWN = "external_unknown"


class AgentExecutionView(BaseModel):
    """AGT-020 public_control v1: exactly five fields, no fencing internals.

    The row's mode, owner machine, epoch, lease, account/session bindings and
    approval grant are deliberately absent: they must never serialize here.
    """

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[1]
    phase: AgentExecutionPhase
    cancel_requested: bool = Field(strict=True)
    cancel_resolution: AgentCancelResolution
    effect_status: AgentEffectStatus

    @field_validator("protocol_version", mode="before")
    @classmethod
    def protocol_version_exact(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported execution protocol_version")
        return value

    @model_validator(mode="after")
    def cancel_relationships_hold(self) -> "AgentExecutionView":
        if not self.cancel_requested and self.cancel_resolution is not AgentCancelResolution.NOT_REQUESTED:
            raise ValueError("cancel_resolution must be not_requested without a request")
        if self.cancel_requested and self.cancel_resolution is AgentCancelResolution.NOT_REQUESTED:
            raise ValueError("a persisted cancel request must have a resolution")
        if (self.cancel_resolution is AgentCancelResolution.CANCELLED
                and self.effect_status is not AgentEffectStatus.NONE):
            raise ValueError("a cancelled run cannot claim an external effect")
        return self


class PublicTraceProjection(BaseModel):
    """Display-only projection; validation never grants source access."""
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    authority: Literal["projection_only"]
    authorization_checked: Literal[False]
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1, max_length=128)
    model_step_id: str = Field(min_length=1, max_length=128)
    tool_step_id: str | None = Field(max_length=128)
    receipt_id: str = Field(min_length=1, max_length=128)
    native_call_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_key: Literal["runtime.tool_search", "memory.search", "research.search", "research.read", "research.describe", "research.propose"] | None
    tool_version: Literal["1"] | None
    projection_schema: str | None = Field(max_length=128)
    input_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    phase: Literal["accepted", "result"]
    captured_at: str = Field(max_length=64)
    time_semantics: Literal["record_capture_not_execution_duration"]
    arguments: dict
    result: dict
    public_plan: dict
    source_authorization: Literal["not_checked"]
    body_is_original_stream: Literal[False]

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported public trace version")
        return value

    @model_validator(mode="after")
    def reviewed_projection(self) -> "PublicTraceProjection":
        from . import public_trace_projection as projection
        from .public_trace_capture import _field_shape
        if type(self.version) is not int or type(self.authorization_checked) is not bool or type(self.body_is_original_stream) is not bool:
            raise ValueError("invalid public trace constants")
        _normalize_aware_datetime(self.captured_at)
        if self.public_plan != {"state": "not_recorded"}:
            raise ValueError("invalid public plan")
        if self.tool_key is None:
            if self.tool_version is not None or self.projection_schema is not None:
                raise ValueError("invalid public trace schema")
        elif (self.tool_version != "1" or self.projection_schema != self.tool_key + "/public-trace/1"
              or projection.SCHEMA_PINS[self.tool_key] != (self.input_schema_sha256, self.output_schema_sha256)):
            raise ValueError("invalid public trace schema")
        for part, rules in (("arguments", projection.ARG_RULES), ("result", projection.RESULT_RULES)):
            node = getattr(self, part)
            if self.phase == "accepted" and part == "result":
                if node != {"state": "not_recorded"}:
                    raise ValueError("invalid public trace result")
            elif self.tool_key is None:
                if node != {"state": "withheld", "reason": "unsupported_tool_schema"}:
                    raise ValueError("invalid public trace body")
            else:
                _field_shape(rules[self.tool_key], node)
        if len(projection.canonical(self.model_dump())) > projection.MAX_PROJECTED_BYTES:
            raise ValueError("public trace projection too large")
        return self


class PublicTraceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt_id: str = Field(min_length=1, max_length=128)
    step_id: str | None = Field(max_length=128)
    native_state: Literal["accepted", "dispatch_intent", "succeeded", "failed", "unknown"]
    state: Literal["available", "withheld"]
    reason: Literal["current_source_unavailable"] | None = None
    accepted: PublicTraceProjection | None = None
    result: PublicTraceProjection | None = None

    @model_serializer(mode="wrap")
    def omit_absent_fields(self, handler):
        value = handler(self)
        for key in ('reason', 'accepted', 'result'):
            if key not in self.model_fields_set:
                value.pop(key, None)
        return value

    @model_validator(mode="after")
    def bounded_record(self) -> "PublicTraceRecord":
        if self.state == "withheld":
            if self.reason != "current_source_unavailable" or self.accepted is not None or self.result is not None:
                raise ValueError("invalid withheld public trace")
        elif self.reason is not None or self.accepted is None:
            raise ValueError("invalid available public trace")
        for phase in ("accepted", "result"):
            body = getattr(self, phase)
            if body is not None and (body.phase != phase or body.receipt_id != self.receipt_id
                                    or phase == "result" and body.tool_step_id != self.step_id):
                raise ValueError("invalid public trace record binding")
        return self


class PublicTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    state: Literal["available", "not_recorded", "withheld"]
    reason: Literal["record_binding_unavailable"] | None = None
    records: list[PublicTraceRecord] = Field(max_length=128)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("unsupported public trace version")
        return value

    @model_serializer(mode="wrap")
    def omit_absent_fields(self, handler):
        value = handler(self)
        for key in ('reason',):
            if key not in self.model_fields_set:
                value.pop(key, None)
        return value

    @model_validator(mode="after")
    def bounded_trace(self) -> "PublicTrace":
        if type(self.version) is not int:
            raise ValueError("unsupported public trace version")
        if (self.state == "withheld") != (self.reason == "record_binding_unavailable"):
            raise ValueError("invalid public trace reason")
        if self.state != "available" and self.records:
            raise ValueError("unavailable public trace has records")
        return self


class AgentRunReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    state: RunState
    answer: str | None = None
    error_code: str | None = None
    replayed: bool = False
    answer_status: Literal["available", "not_recorded", "unavailable"] = "not_recorded"
    answer_unavailable_reason: Literal["answer_or_source_unavailable"] | None = None
    citations: list[AnswerCitation] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    pending_action: dict | None = None
    task_id: str | None = None
    execution: AgentExecutionView | None = None
    public_trace: PublicTrace = Field(default_factory=lambda: PublicTrace(
        version=1, state="not_recorded", records=[]))

    @model_validator(mode="after")
    def execution_matches_run_state(self) -> "AgentRunReceipt":
        execution = self.execution
        if execution is None:
            return self
        terminal = self.state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED)
        if (execution.phase is AgentExecutionPhase.TERMINAL) != terminal:
            raise ValueError("execution phase contradicts run state")
        if (execution.cancel_resolution is AgentCancelResolution.CANCELLED
                and self.state is not RunState.CANCELLED):
            raise ValueError("cancelled resolution requires a cancelled run")
        if (execution.cancel_resolution is AgentCancelResolution.TOO_LATE
                and self.state is not RunState.COMPLETED):
            raise ValueError("too_late resolution requires a completed run")
        if (execution.phase is AgentExecutionPhase.WAITING_FOR_HUMAN
                and self.state is not RunState.WAITING_FOR_HUMAN):
            raise ValueError("human execution phase requires waiting_for_human")
        if (execution.phase in (AgentExecutionPhase.MODEL_INFLIGHT, AgentExecutionPhase.TOOL_INFLIGHT)
                and self.state is not RunState.RUNNING):
            raise ValueError("inflight execution phase requires a running run")
        return self


from backend.app.core.errors import ApiEnvelope


class AgentReceiptEnvelope(ApiEnvelope):
    data: AgentRunReceipt | None


CostStatus = Literal["reserved", "settled", "unknown", "released", "reconciliation_required"]


class AgentCostItem(BaseModel):
    """Minimal audit/billing view: no prompts, usage payloads or provider bodies."""
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    step_id: str
    call_key: str
    requester_id: str
    provider_key: str
    model: str
    price_version: str
    currency: str
    reserved_amount: str
    estimated_amount: str | None
    status: CostStatus
    error_code: str | None
    created_at: datetime
    settled_at: datetime | None


class AgentCostPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentCostItem]
    next_cursor: str | None


class AgentCostEnvelope(ApiEnvelope):
    data: AgentCostPage


class PiRuntimeDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PiRuntimeIdentity(PiRuntimeDTO):
    status: Literal["recorded", "not_recorded", "unsupported"]
    basis: Literal["run_runtime_binding"] | None
    reason: Literal["binding_absent", "binding_unsupported"] | None
    engine: Literal["@earendil-works/pi-coding-agent"] | None
    version: Literal["1.4.0"] | None
    adapter_contract: Literal["host-run-receipt-v1"] | None
    execution_proof: Literal["not_proven"] = "not_proven"

    @model_validator(mode="after")
    def consistent_identity(self):
        values = (self.basis, self.engine, self.version, self.adapter_contract)
        if self.status == "recorded":
            if any(v is None for v in values) or self.reason is not None:
                raise ValueError("inconsistent runtime identity")
        elif (any(v is not None for v in values)
              or self.reason != ("binding_absent" if self.status == "not_recorded" else "binding_unsupported")):
            raise ValueError("inconsistent runtime identity")
        return self


class PiRuntimeLaunch(PiRuntimeDTO):
    status: Literal["recorded", "not_recorded", "unsupported"]
    reason: Literal["launch_record_absent", "launch_record_unsupported"] | None
    wire_protocol: Literal["v4.pi-readonly/1", "v4.pi-memory/1"] | None
    tool_profile: Literal["pi-readonly-v1", "pi-memory-v1"] | None
    launch_profile: Literal["host-pi-readonly-node24-linux-v1", "host-pi-readonly-node24-darwin-v1", "host-pi-memory-node24-linux-v1", "host-pi-memory-node24-darwin-v1"] | None
    source_manifest_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    toolset_version: str | None = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent_launch(self):
        values = (self.wire_protocol, self.tool_profile, self.launch_profile,
                  self.source_manifest_sha256, self.toolset_version)
        if self.status == "recorded":
            if any(v is None for v in values) or self.reason is not None:
                raise ValueError("inconsistent launch record")
            family = 'memory' if self.tool_profile == 'pi-memory-v1' else 'readonly'
            suffix = '-online-v1' if self.wire_protocol.endswith('/2') else '-v1'
            if self.wire_protocol != 'v4.pi-' + family + ('/2' if suffix == '-online-v1' else '/1') or self.launch_profile not in tuple('host-pi-'+family+'-node24-'+platform+suffix for platform in ('linux','darwin')):
                raise ValueError('inconsistent launch profile')
        elif (any(v is not None for v in values)
              or self.reason != ("launch_record_absent" if self.status == "not_recorded" else "launch_record_unsupported")):
            raise ValueError("inconsistent launch record")
        return self


class PiRuntimeStep(PiRuntimeDTO):
    step_id: str = Field(min_length=1, max_length=36)
    kind: Literal["model", "tool", "checkpoint"]
    state: Literal["pending", "running", "succeeded", "failed", "unknown", "cancelled"]
    sequence_no: int = Field(strict=True, gt=0)
    output_ref: str | None = Field(max_length=200)
    error_code: str | None = Field(max_length=120)


class PiNativeToolReceipt(PiRuntimeDTO):
    receipt_id: str = Field(min_length=1, max_length=36)
    model_step_id: str = Field(min_length=1, max_length=36)
    tool_step_id: str | None = Field(min_length=1, max_length=36)
    native_call_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    pi_name: Literal["v4_tasks_list_mine_v1", "v4_knowledge_search_v1", "v4_catalog_search_v1", "v4_memory_search_v1", "v4_memory_save_v1", "v4_memory_correct_v1", "v4_memory_withdraw_v1"]
    tool_key: Literal["tasks.list_mine", "knowledge.search", "catalog.search", "memory.search", "memory.save", "memory.correct", "memory.withdraw"]
    tool_version: Literal["1"]
    toolset_version: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    state: Literal["accepted", "dispatch_intent", "succeeded", "failed", "unknown"]
    output_ref: str | None = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    error_code: str | None = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")

    @model_validator(mode="after")
    def consistent_call(self):
        expected = {"tasks.list_mine": "v4_tasks_list_mine_v1",
                    "knowledge.search": "v4_knowledge_search_v1", "catalog.search": "v4_catalog_search_v1", "memory.search": "v4_memory_search_v1", "memory.save": "v4_memory_save_v1", "memory.correct": "v4_memory_correct_v1", "memory.withdraw": "v4_memory_withdraw_v1"}
        if expected[self.tool_key] != self.pi_name:
            raise ValueError("inconsistent native mapping")
        if self.state == "accepted":
            valid = self.tool_step_id is None and self.output_ref is None and self.error_code is None
        elif self.state == "dispatch_intent":
            valid = self.tool_step_id is not None and self.output_ref is None and self.error_code is None
        elif self.state == "succeeded":
            valid = self.tool_step_id is not None and self.output_ref is not None and self.error_code is None
        else:
            valid = self.output_ref is None and self.error_code is not None
        if not valid:
            raise ValueError("inconsistent native receipt state")
        return self


class PiRunRuntimeReceipt(PiRuntimeDTO):
    contract_version: int = Field(strict=True, ge=1, le=1)
    run_id: str = Field(min_length=1, max_length=36)
    state: Literal["created", "running", "waiting_for_human", "waiting_for_external", "completed", "failed", "cancelled"]
    runtime: PiRuntimeIdentity
    launch: PiRuntimeLaunch
    steps: list[PiRuntimeStep]
    native_calls: list[PiNativeToolReceipt]
    recovery: Literal["A_stop_only"] = "A_stop_only"
    result_replay: Literal["unsupported"] = "unsupported"
    step_citation_association: Literal["unsupported"] = "unsupported"

    @model_validator(mode="after")
    def exact_step_relations(self):
        by_id = {s.step_id: s for s in self.steps}
        if len(by_id) != len(self.steps) or len({s.sequence_no for s in self.steps}) != len(self.steps):
            raise ValueError("duplicate runtime Step")
        native_ids, receipt_ids, tool_ids = set(), set(), set()
        for call in self.native_calls:
            if self.launch.tool_profile != 'pi-memory-v1' and call.tool_key in ('memory.save', 'memory.correct', 'memory.withdraw'):
                raise ValueError('write tool in readonly profile')
            model = by_id.get(call.model_step_id)
            if (self.launch.status != "recorded" or self.runtime.status != "recorded"
                    or call.toolset_version != self.launch.toolset_version
                    or model is None or model.kind != "model" or model.state != "succeeded"
                    or call.native_call_id in native_ids or call.receipt_id in receipt_ids):
                raise ValueError("inconsistent native receipt relation")
            native_ids.add(call.native_call_id)
            receipt_ids.add(call.receipt_id)
            if call.tool_step_id is not None:
                tool = by_id.get(call.tool_step_id)
                if (tool is None or tool.kind != "tool" or call.tool_step_id in tool_ids
                        or tool.sequence_no <= model.sequence_no
                        or call.state == "succeeded" and (tool.state != "succeeded" or tool.output_ref != call.output_ref)):
                    raise ValueError("inconsistent native tool Step")
                tool_ids.add(call.tool_step_id)
        return self


class PiOnlineRuntimeLaunch(PiRuntimeLaunch):
    wire_protocol: Literal["v4.pi-readonly/2", "v4.pi-memory/2"] | None
    launch_profile: Literal["host-pi-readonly-node24-linux-online-v1", "host-pi-readonly-node24-darwin-online-v1", "host-pi-memory-node24-linux-online-v1", "host-pi-memory-node24-darwin-online-v1"] | None


class PiUsagePresence(PiRuntimeDTO):
    prompt_tokens: bool = Field(strict=True)
    completion_tokens: bool = Field(strict=True)
    cached_tokens: bool = Field(strict=True)
    cache_write_tokens: bool = Field(strict=True)


class PiProviderCallReceipt(PiRuntimeDTO):
    receipt_id: str = Field(min_length=1, max_length=36)
    model_step_id: str = Field(min_length=1, max_length=36)
    attempt_id: str = Field(min_length=1, max_length=128)
    provider_request_id_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    message_digest: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    usage_presence: PiUsagePresence | None
    ledger_id: str = Field(min_length=1, max_length=36)
    state: Literal["permitted", "send_intent", "responded", "unknown", "not_sent"]
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def aware_time(cls, value):
        return _normalize_aware_datetime(value)

    @model_validator(mode="after")
    def consistent_provider(self):
        if self.updated_at < self.created_at:
            raise ValueError("inconsistent provider times")
        if self.state in ("permitted", "send_intent") and any(
                value is not None for value in (self.response_digest, self.message_digest, self.usage_presence)):
            raise ValueError("inconsistent provider state")
        if self.state == "responded" and self.response_digest is None:
            raise ValueError("missing response digest")
        return self


class PiOnlineRunRuntimeReceipt(PiRunRuntimeReceipt):
    contract_version: int = Field(strict=True, ge=2, le=2)
    launch: PiOnlineRuntimeLaunch
    provider_calls: list[PiProviderCallReceipt]

    @model_validator(mode="after")
    def exact_provider_relations(self):
        by_id = {step.step_id: step for step in self.steps}
        seen = [set() for _ in range(4)]
        for call in self.provider_calls:
            step = by_id.get(call.model_step_id)
            if (self.launch.status != "recorded" or self.runtime.status != "recorded"
                    or step is None or step.kind != "model"):
                raise ValueError("inconsistent provider Step")
            for values, value in zip(seen, (call.receipt_id, call.model_step_id, call.attempt_id, call.ledger_id)):
                if value in values:
                    raise ValueError("duplicate provider relation")
                values.add(value)
        if {step.step_id for step in self.steps if step.kind == "model"} != seen[1]:
            raise ValueError("missing provider receipt")
        return self


class PiStreamRuntimeLaunch(PiRuntimeLaunch):
    """Committed readonly /3 facts, never a launch or recovery permission."""
    wire_protocol: Literal["v4.pi-readonly/3"] | None
    tool_profile: Literal["pi-readonly-v1"] | None
    launch_profile: Literal["host-pi-readonly-node24-linux-online-stream-v1",
                            "host-pi-readonly-node24-darwin-online-stream-v1"] | None

    @model_validator(mode="after")
    def consistent_launch(self):
        # Override the parent's /1-/2 pairing rule without widening that class.
        values = (self.wire_protocol, self.tool_profile, self.launch_profile,
                  self.source_manifest_sha256, self.toolset_version)
        if self.status == "recorded":
            if any(value is None for value in values) or self.reason is not None:
                raise ValueError("inconsistent stream launch record")
        elif (any(value is not None for value in values)
              or self.reason != ("launch_record_absent" if self.status == "not_recorded"
                                 else "launch_record_unsupported")):
            raise ValueError("inconsistent stream launch record")
        return self


class PiStreamRunRuntimeReceipt(PiOnlineRunRuntimeReceipt):
    contract_version: int = Field(strict=True, ge=3, le=3)
    launch: PiStreamRuntimeLaunch


def _pi_receipt_contract(value):
    version = value.get("contract_version") if isinstance(value, dict) else getattr(value, "contract_version", None)
    return str(version) if type(version) is int and version in (1, 2, 3) else "unsupported"


PiPublicRuntimeReceipt = Annotated[
    Annotated[PiRunRuntimeReceipt, Tag("1")] | Annotated[PiOnlineRunRuntimeReceipt, Tag("2")]
    | Annotated[PiStreamRunRuntimeReceipt, Tag("3")],
    Discriminator(_pi_receipt_contract),
]


class PiRuntimeReceiptEnvelope(ApiEnvelope):
    data: PiPublicRuntimeReceipt | None


# Canonical result readback is independent of the runtime receipt DTO.
from backend.app.modules.memory.schemas import MemoryOut, MemorySearchPage


class PiSafeToolError(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    code: Literal['memory_query_invalid']
    field: Literal['query']
    hint: Literal['请使用1至256个有效字符和不超过16个查询词，移除空字符后重试。']


class PiResultLocation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: str
    tool_step_id: str


class PiAvailableResult(PiResultLocation):
    status: Literal['available']
    tool_key: Literal['memory.search']
    tool_version: Literal['1']
    captured_at: datetime
    output_ref: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    canonical_ref: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')


class PiMemoryResultItem(MemoryOut):
    # Preserve the captured JSON strings under the original output hash. The
    # normal business MemoryOut keeps its existing datetime serialization.
    model_config = ConfigDict(extra='forbid', strict=True)
    expires_at: str | None = Field(json_schema_extra={'format':'date-time'})
    created_at: str = Field(json_schema_extra={'format':'date-time'})
    updated_at: str = Field(json_schema_extra={'format':'date-time'})

    @field_validator('expires_at','created_at','updated_at')
    @classmethod
    def utc_timestamp_string(cls, value):
        if value is None:return None
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError('result timestamp must have explicit UTC offset')
        return value


class PiMemoryResultPayload(MemorySearchPage):
    model_config = ConfigDict(extra='forbid', strict=True)
    items: list[PiMemoryResultItem]


class PiAvailableToolResult(PiAvailableResult):
    kind: Literal['success','tool_error']
    payload: PiMemoryResultPayload | PiSafeToolError

    @model_validator(mode='after')
    def kind_matches_payload(self):
        expected = PiMemoryResultPayload if self.kind == 'success' else PiSafeToolError
        if type(self.payload) is not expected:
            raise ValueError('result kind does not match payload')
        return self


class PiWithheldResult(PiResultLocation):
    status: Literal['withheld']
    reason: Literal['binding_invalid','authorization_changed','source_changed','source_unsupported','source_incomplete']


class PiAbsentResult(PiResultLocation):
    status: Literal['not_recorded']
    reason: Literal['result_absent']


PiToolResultRead = Annotated[
    PiAvailableToolResult | PiWithheldResult | PiAbsentResult, Field(discriminator='status')]


class PiToolResultReadEnvelope(ApiEnvelope):
    data: PiToolResultRead | None
