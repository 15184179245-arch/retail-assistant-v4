"""Scoped persistence operations for versioned Agent runs and steps.

This module deliberately owns no transaction boundary beyond one savepoint per
write. Callers remain responsible for committing or rolling back the outer
transaction.
"""

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from uuid import uuid4
from sqlalchemy import and_, or_, func, select, update, text, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.modules.agents.models import AgentConfirmation, AgentExecution, AgentPiToolReceipt, AgentPiProviderReceipt, AgentRun, AgentStep, AgentToolCall
from backend.app.modules.agents.schemas import (
    RunCreate,
    RunState,
    StepCreate,
    StepKind,
    StepState,
    StepTransition,
)


class AgentPersistenceError(RuntimeError):
    """Persistence failure exposing only a stable machine-readable code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_RUN_EDGES = {
    RunState.CREATED: {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED},
    RunState.RUNNING: {
        RunState.WAITING_FOR_HUMAN,
        RunState.WAITING_FOR_EXTERNAL,
        RunState.COMPLETED,
        RunState.FAILED,
        RunState.CANCELLED,
    },
    RunState.WAITING_FOR_HUMAN: {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED},
    RunState.WAITING_FOR_EXTERNAL: {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED},
    RunState.COMPLETED: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}

_STEP_EDGES = {
    StepState.PENDING: {StepState.RUNNING, StepState.CANCELLED},
    StepState.RUNNING: {StepState.SUCCEEDED, StepState.FAILED, StepState.UNKNOWN},
    StepState.SUCCEEDED: set(),
    StepState.FAILED: set(),
    StepState.UNKNOWN: set(),
    StepState.CANCELLED: set(),
}

_ACTIVE_STEP_STATES = (StepState.PENDING.value, StepState.RUNNING.value)
_TERMINAL_RUN_STATES = (RunState.COMPLETED.value, RunState.FAILED.value, RunState.CANCELLED.value)


def _fail(code: str) -> None:
    raise AgentPersistenceError(code)


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _canonical_digest(value: object) -> str | None:
    """Recompute the canonical argument digest; any malformed value fails shut."""
    try:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return None
    return sha256(canonical.encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("agent_invalid_data")
    return value.astimezone(timezone.utc)


def _ensure_real_sqlite_transaction(session: Session) -> None:
    """Avoid releasing a SQLite savepoint that is not inside a DB transaction."""
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        raw_connection = connection.connection.driver_connection
        if not raw_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")


def _write_savepoint(session: Session):
    _ensure_real_sqlite_transaction(session)
    return session.begin_nested()


def _scoped_run_query(organization_id: str, store_id: str, run_id: str):
    return select(AgentRun).where(
        AgentRun.organization_id == organization_id,
        AgentRun.store_id == store_id,
        AgentRun.id == run_id,
    ).execution_options(populate_existing=True)


def _load_run(session: Session, organization_id: str, store_id: str, run_id: str) -> AgentRun | None:
    return session.scalar(_scoped_run_query(organization_id, store_id, run_id))


def _require_run(session: Session, organization_id: str, store_id: str, run_id: str) -> AgentRun:
    run = _load_run(session, organization_id, store_id, run_id)
    if run is None:
        _fail("agent_not_found")
    return run


def _cas_run(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    run_id: str,
    expected_version: int,
    now: datetime,
) -> AgentRun:
    """Bump a run version under its scope and observed-state CAS."""
    if not _positive_int(expected_version):
        _fail("agent_invalid_data")
    run = _require_run(session, organization_id, store_id, run_id)
    if run.version != expected_version:
        _fail("agent_conflict")
    # DB clock precision may be lower than created_at's precision; preserve
    # updated_at >= created_at even when both fall within the same DB tick.
    if now < run.created_at:
        now = run.created_at
    result = session.execute(
        update(AgentRun)
        .where(
            AgentRun.organization_id == organization_id,
            AgentRun.store_id == store_id,
            AgentRun.id == run_id,
            AgentRun.version == expected_version,
            AgentRun.state == run.state,
        )
        .values(version=expected_version + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        _fail("agent_conflict")
    session.flush()
    refreshed = _load_run(session, organization_id, store_id, run_id)
    if refreshed is None:  # The scoped row cannot disappear under RESTRICT, but fail closed.
        _fail("agent_not_found")
    return refreshed


def _map_integrity_error(exc: IntegrityError) -> AgentPersistenceError:
    # Do not expose database messages, SQL, bound values, or DTO contents.
    return AgentPersistenceError("agent_integrity_conflict")


def create_run(session: Session, data: RunCreate, *, now: datetime | None = None) -> AgentRun:
    if not isinstance(data, RunCreate):
        _fail("agent_invalid_data")
    current = _timestamp(now)
    if data.deadline_at <= current:
        _fail("agent_invalid_data")
    run = AgentRun(
        id=data.id,
        organization_id=data.organization_id,
        store_id=data.store_id,
        requester_id=data.requester_id,
        task_id=data.task_id,
        agent_key=data.agent_key,
        agent_version=data.agent_version,
        provider_key=data.provider_key,
        model=data.model,
        prompt_version=data.prompt_version,
        toolset_version=data.toolset_version,
        input_ref=data.input_ref,
        state=RunState.CREATED.value,
        version=1,
        max_input_tokens=data.max_input_tokens,
        max_output_tokens=data.max_output_tokens,
        max_model_calls=data.max_model_calls,
        max_tool_calls=data.max_tool_calls,
        max_steps=data.max_steps,
        deadline_at=data.deadline_at,
        created_at=current,
        updated_at=current,
    )
    try:
        with _write_savepoint(session):
            session.add(run)
            session.flush()
    except IntegrityError as exc:
        raise _map_integrity_error(exc) from None
    return run


def get_run(session: Session, organization_id: str, store_id: str, run_id: str) -> AgentRun | None:
    return _load_run(session, organization_id, store_id, run_id)


def list_steps(session: Session, organization_id: str, store_id: str, run_id: str) -> list[AgentStep]:
    statement = (
        select(AgentStep)
        .where(
            AgentStep.organization_id == organization_id,
            AgentStep.store_id == store_id,
            AgentStep.run_id == run_id,
        )
        .order_by(AgentStep.sequence_no)
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(statement).all())


def get_tool_call(
    session: Session, organization_id: str, store_id: str, run_id: str, step_id: str
) -> AgentToolCall | None:
    statement = (
        select(AgentToolCall)
        .where(
            AgentToolCall.organization_id == organization_id,
            AgentToolCall.store_id == store_id,
            AgentToolCall.run_id == run_id,
            AgentToolCall.step_id == step_id,
        )
        .execution_options(populate_existing=True)
    )
    return session.scalar(statement)


def transition_run(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    run_id: str,
    expected_version: int,
    to_state: RunState,
    now: datetime | None = None,
) -> AgentRun:
    if not isinstance(to_state, RunState):
        _fail("agent_invalid_data")
    current = _timestamp(now)
    try:
        with _write_savepoint(session):
            run = _require_run(session, organization_id, store_id, run_id)
            if not _positive_int(expected_version):
                _fail("agent_invalid_data")
            if run.version != expected_version:
                _fail("agent_conflict")
            if current < run.created_at:
                current = run.created_at
            old_state = RunState(run.state)
            if to_state not in _RUN_EDGES[old_state]:
                _fail("agent_invalid_transition")
            if to_state is RunState.RUNNING and current >= run.deadline_at:
                _fail("agent_deadline_exceeded")
            active_count = session.scalar(
                select(func.count())
                .select_from(AgentStep)
                .where(
                    AgentStep.organization_id == organization_id,
                    AgentStep.store_id == store_id,
                    AgentStep.run_id == run_id,
                    AgentStep.state.in_(_ACTIVE_STEP_STATES),
                )
            ) or 0
            if to_state in (RunState.WAITING_FOR_HUMAN, RunState.WAITING_FOR_EXTERNAL):
                running_count = session.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(
                        AgentStep.organization_id == organization_id,
                        AgentStep.store_id == store_id,
                        AgentStep.run_id == run_id,
                        AgentStep.state == StepState.RUNNING.value,
                    )
                ) or 0
                if running_count:
                    _fail("agent_invalid_transition")
            if to_state.value in _TERMINAL_RUN_STATES and active_count:
                _fail("agent_invalid_transition")
            if to_state is RunState.COMPLETED:
                incomplete_count = session.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(
                        AgentStep.organization_id == organization_id,
                        AgentStep.store_id == store_id,
                        AgentStep.run_id == run_id,
                        AgentStep.state != StepState.SUCCEEDED.value,
                    )
                ) or 0
                if incomplete_count:
                    _fail("agent_invalid_transition")
            result = session.execute(
                update(AgentRun)
                .where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.store_id == store_id,
                    AgentRun.id == run_id,
                    AgentRun.version == expected_version,
                    AgentRun.state == old_state.value,
                )
                .values(state=to_state.value, version=expected_version + 1, updated_at=current)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                _fail("agent_conflict")
            session.flush()
            refreshed = _load_run(session, organization_id, store_id, run_id)
            if refreshed is None:
                _fail("agent_not_found")
            return refreshed
    except IntegrityError as exc:
        raise _map_integrity_error(exc) from None


def append_step(session: Session, data: StepCreate, *, expected_run_version: int, now: datetime | None = None) -> AgentStep:
    if not isinstance(data, StepCreate):
        _fail("agent_invalid_data")
    if not _positive_int(expected_run_version):
        _fail("agent_invalid_data")
    current = _timestamp(now)
    try:
        with _write_savepoint(session):
            run = _require_run(session, data.organization_id, data.store_id, data.run_id)
            if run.version != expected_run_version:
                _fail("agent_conflict")
            if run.state != RunState.RUNNING.value:
                _fail("agent_invalid_transition")
            if current >= run.deadline_at:
                _fail("agent_deadline_exceeded")
            run = _cas_run(
                session,
                organization_id=data.organization_id,
                store_id=data.store_id,
                run_id=data.run_id,
                expected_version=expected_run_version,
                now=current,
            )
            count = session.scalar(
                select(func.count())
                .select_from(AgentStep)
                .where(
                    AgentStep.organization_id == data.organization_id,
                    AgentStep.store_id == data.store_id,
                    AgentStep.run_id == data.run_id,
                )
            ) or 0
            if data.sequence_no != count + 1:
                _fail("agent_invalid_data")
            if count >= run.max_steps or data.sequence_no > run.max_steps:
                _fail("agent_budget_exceeded")
            kind = data.kind.value
            if kind == StepKind.MODEL.value:
                used = session.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(
                        AgentStep.organization_id == data.organization_id,
                        AgentStep.store_id == data.store_id,
                        AgentStep.run_id == data.run_id,
                        AgentStep.kind == StepKind.MODEL.value,
                    )
                ) or 0
                if used >= run.max_model_calls:
                    _fail("agent_budget_exceeded")
            elif kind == StepKind.TOOL.value:
                used = session.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(
                        AgentStep.organization_id == data.organization_id,
                        AgentStep.store_id == data.store_id,
                        AgentStep.run_id == data.run_id,
                        AgentStep.kind == StepKind.TOOL.value,
                    )
                ) or 0
                if used >= run.max_tool_calls:
                    _fail("agent_budget_exceeded")
            step = AgentStep(
                id=data.id,
                organization_id=data.organization_id,
                store_id=data.store_id,
                run_id=data.run_id,
                sequence_no=data.sequence_no,
                kind=kind,
                state=StepState.PENDING.value,
                version=1,
                input_ref=data.input_ref,
                created_at=current,
                updated_at=current,
            )
            session.add(step)
            if data.tool_call is not None:
                call = data.tool_call
                session.add(AgentToolCall(
                    id=call.id,
                    organization_id=data.organization_id,
                    store_id=data.store_id,
                    run_id=data.run_id,
                    step_id=data.id,
                    step_kind=StepKind.TOOL.value,
                    tool_key=call.tool_key,
                    tool_version=call.tool_version,
                    idempotency_key=call.idempotency_key,
                    input_digest=call.input_digest,
                ))
            session.flush()
            return step
    except IntegrityError as exc:
        raise _map_integrity_error(exc) from None


def transition_step(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    run_id: str,
    step_id: str,
    expected_run_version: int,
    expected_step_version: int,
    change: StepTransition,
    now: datetime | None = None,
) -> AgentStep:
    if not isinstance(change, StepTransition):
        _fail("agent_invalid_data")
    if not _positive_int(expected_run_version) or not _positive_int(expected_step_version):
        _fail("agent_invalid_data")
    current = _timestamp(now)
    try:
        with _write_savepoint(session):
            run = _require_run(session, organization_id, store_id, run_id)
            if run.version != expected_run_version:
                _fail("agent_conflict")
            if run.state != RunState.RUNNING.value and not (
                run.state == RunState.WAITING_FOR_HUMAN.value
                and change.to_state is StepState.CANCELLED
            ):
                _fail("agent_invalid_transition")
            run = _cas_run(
                session,
                organization_id=organization_id,
                store_id=store_id,
                run_id=run_id,
                expected_version=expected_run_version,
                now=current,
            )
            statement = (
                select(AgentStep)
                .where(
                    AgentStep.organization_id == organization_id,
                    AgentStep.store_id == store_id,
                    AgentStep.run_id == run_id,
                    AgentStep.id == step_id,
                )
                .execution_options(populate_existing=True)
            )
            step = session.scalar(statement)
            if step is None:
                _fail("agent_not_found")
            if step.version != expected_step_version:
                _fail("agent_conflict")
            old_state = StepState(step.state)
            if change.to_state not in _STEP_EDGES[old_state]:
                _fail("agent_invalid_transition")
            if change.to_state is StepState.RUNNING and current >= run.deadline_at:
                _fail("agent_deadline_exceeded")
            if step.kind != StepKind.MODEL.value and (change.input_tokens is not None or change.output_tokens is not None):
                _fail("agent_invalid_data")
            if step.kind == StepKind.MODEL.value and change.to_state is StepState.SUCCEEDED:
                if change.input_tokens is None or change.output_tokens is None:
                    _fail("agent_invalid_data")
                # Observed usage is retained even above budget; execution gating
                # belongs to the runtime, not this persistence result write.
            if change.to_state is StepState.RUNNING:
                other_running = session.scalar(
                    select(func.count())
                    .select_from(AgentStep)
                    .where(
                        AgentStep.organization_id == organization_id,
                        AgentStep.store_id == store_id,
                        AgentStep.run_id == run_id,
                        AgentStep.id != step_id,
                        AgentStep.state == StepState.RUNNING.value,
                    )
                ) or 0
                if other_running:
                    _fail("agent_invalid_transition")
            result = session.execute(
                update(AgentStep)
                .where(
                    AgentStep.organization_id == organization_id,
                    AgentStep.store_id == store_id,
                    AgentStep.run_id == run_id,
                    AgentStep.id == step_id,
                    AgentStep.version == expected_step_version,
                    AgentStep.state == old_state.value,
                )
                .values(
                    state=change.to_state.value,
                    version=expected_step_version + 1,
                    input_tokens=change.input_tokens,
                    output_tokens=change.output_tokens,
                    output_ref=change.output_ref,
                    error_code=change.error_code,
                    updated_at=current,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                _fail("agent_conflict")
            session.flush()
            refreshed = session.scalar(statement)
            if refreshed is None:
                _fail("agent_not_found")
            return refreshed
    except IntegrityError as exc:
        raise _map_integrity_error(exc) from None


def get_confirmation(session: Session, organization_id: str, store_id: str,
                     run_id: str) -> AgentConfirmation | None:
    return session.scalar(select(AgentConfirmation).where(
        AgentConfirmation.organization_id == organization_id,
        AgentConfirmation.store_id == store_id,
        AgentConfirmation.run_id == run_id,
    ).execution_options(populate_existing=True))


def create_confirmation(session: Session, confirmation: AgentConfirmation) -> AgentConfirmation:
    try:
        with _write_savepoint(session):
            run = _require_run(session, confirmation.organization_id,
                               confirmation.store_id, confirmation.run_id)
            step = session.scalar(select(AgentStep).where(
                AgentStep.organization_id == confirmation.organization_id,
                AgentStep.store_id == confirmation.store_id,
                AgentStep.run_id == confirmation.run_id,
                AgentStep.id == confirmation.step_id,
            ))
            if run.state != RunState.RUNNING.value or step is None or step.state != StepState.PENDING.value:
                _fail("agent_invalid_transition")
            session.add(confirmation)
            session.flush()
    except IntegrityError as exc:
        raise _map_integrity_error(exc) from None
    return confirmation


def set_confirmation_state(session: Session, confirmation: AgentConfirmation,
                           to_state: str, *, reason: str | None = None,
                           result_task_id: str | None = None) -> AgentConfirmation:
    edges = {"pending": {"executing", "rejected", "expired"},
             "executing": {"approved", "failed", "unknown"}}
    if to_state not in edges.get(confirmation.state, set()):
        _fail("agent_invalid_transition")
    current = _timestamp(None)
    with _write_savepoint(session):
        result = session.execute(update(AgentConfirmation).where(
            AgentConfirmation.run_id == confirmation.run_id,
            AgentConfirmation.organization_id == confirmation.organization_id,
            AgentConfirmation.store_id == confirmation.store_id,
            AgentConfirmation.state == confirmation.state,
            AgentConfirmation.version == confirmation.version,
        ).values(state=to_state, version=confirmation.version + 1,
                 decision_reason=reason, result_task_id=result_task_id,
                 updated_at=current).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            _fail("agent_conflict")
        session.flush()
    return get_confirmation(session, confirmation.organization_id,
                            confirmation.store_id, confirmation.run_id)


def renew_human_run_deadline(session: Session, run: AgentRun,
                             *, seconds: int = 30) -> AgentRun:
    """Start a fresh, bounded tool execution window after human wait."""
    current = _timestamp(None)
    with _write_savepoint(session):
        result = session.execute(update(AgentRun).where(
            AgentRun.organization_id == run.organization_id,
            AgentRun.store_id == run.store_id, AgentRun.id == run.id,
            AgentRun.state == RunState.WAITING_FOR_HUMAN.value,
            AgentRun.version == run.version,
        ).values(deadline_at=current + timedelta(seconds=seconds),
                 version=run.version + 1, updated_at=current)
         .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            _fail("agent_conflict")
        session.flush()
    return get_run(session, run.organization_id, run.store_id, run.id)


def record_answer(session: Session, *, run: AgentRun, message,
                  evidence: list[dict], own_evidence: list[dict] | None = None) -> None:
    """Stage the receipt alongside the caller's final message/state/audit."""
    from backend.app.modules.agents.models import AgentAnswer, AgentConversation
    conversation = session.scalar(select(AgentConversation).where(
        AgentConversation.id == message.conversation_id,
        AgentConversation.organization_id == run.organization_id,
        AgentConversation.store_id == run.store_id,
        AgentConversation.subject_id == run.requester_id,
        AgentConversation.status == "active",
    ))
    if (run.state != "completed" or conversation is None or message.role != "assistant"
            or (message.organization_id, message.store_id) != (run.organization_id, run.store_id)
            or message.source_ref != f"run:{run.id}" or message.state != "active"):
        raise AgentPersistenceError("agent_answer_scope_invalid")
    session.add(AgentAnswer(run_id=run.id, organization_id=run.organization_id,
                            store_id=run.store_id, message_id=message.id,
                            evidence=evidence, own_evidence=own_evidence,
                            created_at=datetime.now(timezone.utc)))
    session.flush()


# AGT-021 control plane. All functions mutate inside the caller-owned transaction.
# A lease is scoped to the exact (run, machine, epoch); never to a process-global ID.

# Internal Host runtime ownership, not the formal Pi wire identity.
LEGACY_RUNTIME = ()
SDK_RUNTIME = ("@earendil-works/pi-coding-agent", "1.4.0", "host-run-receipt-v1")
_RUNTIME_KEYS = ("engine", "version", "adapter_contract")


def sdk_runtime_binding() -> dict:
    return dict(zip(_RUNTIME_KEYS, SDK_RUNTIME))


def validate_runtime_identity(expected: tuple) -> tuple:
    if type(expected) is not tuple or expected not in (LEGACY_RUNTIME, SDK_RUNTIME):
        _fail("agent_runtime_identity_invalid")
    return expected


def runtime_identity(context: object) -> tuple:
    if type(context) is not dict:
        _fail("agent_runtime_binding_invalid")
    if "runtime_binding" not in context:
        return LEGACY_RUNTIME
    value = context["runtime_binding"]
    if (type(value) is not dict or set(value) != set(_RUNTIME_KEYS)
            or any(type(value[k]) is not str for k in _RUNTIME_KEYS)
            or tuple(value[k] for k in _RUNTIME_KEYS) != SDK_RUNTIME):
        _fail("agent_runtime_binding_invalid")
    return SDK_RUNTIME


def _runtime_stamp(context: object) -> str:
    # Preserve absent versus explicit null, including malformed-root identity.
    value = (["absent"] if type(context) is dict and "runtime_binding" not in context
             else ["present", context["runtime_binding"]] if type(context) is dict
             else ["invalid_context", context])
    if type(context) is dict and "pi_runtime_record" in context:
        value.append(["pi_runtime_record", context["pi_runtime_record"]])
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _reject_runtime_mutations(session: Session) -> None:
    # Do not let an autoflush turn the caller's edited ORM dict into the
    # supposedly authoritative old value. In-place nested edits are covered.
    with session.no_autoflush:
        for obj in list(session.identity_map.values()):
            if not isinstance(obj, AgentExecution) or "frozen_context" not in obj.__dict__:
                continue
            state = obj.__dict__
            # ORM identity is retained even if the PK/scope columns are expired
            # or deferred. Never load those attributes: that could autoflush
            # the caller's edited JSON before we compare the durable old value.
            # run_id is the existing table's single globally unique primary key.
            identity = inspect(obj).identity
            if identity is None:
                continue  # Only unpersisted objects lack a durable old value.
            stored = session.execute(select(AgentExecution.frozen_context).where(
                AgentExecution.run_id == identity[0])).first()
            if stored is not None and _runtime_stamp(stored[0]) != _runtime_stamp(state["frozen_context"]):
                _fail("agent_runtime_binding_immutable")


def runtime_candidate_clause(session: Session, expected: tuple = LEGACY_RUNTIME):
    validate_runtime_identity(expected)
    context = AgentExecution.frozen_context
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        root_object = func.json_type(context) == "object"
        binding_type = func.json_type(context, "$.runtime_binding")
        if expected == LEGACY_RUNTIME:
            return and_(root_object, binding_type.is_(None))
        return and_(root_object, binding_type == "object", *(
            func.json_extract(context, "$.runtime_binding." + key) == value
            for key, value in zip(_RUNTIME_KEYS, expected)))
    if dialect == "postgresql":
        binding = context.op("->")("runtime_binding")
        if expected == LEGACY_RUNTIME:
            return and_(func.json_typeof(context) == "object", binding.is_(None))
        return and_(func.json_typeof(context) == "object", func.json_typeof(binding) == "object", *(
            binding.op("->>")(key) == value for key, value in zip(_RUNTIME_KEYS, expected)))
    _fail("agent_runtime_dialect_unsupported")


def require_run_runtime(session: Session, run, expected: tuple = LEGACY_RUNTIME) -> None:
    validate_runtime_identity(expected)
    _reject_runtime_mutations(session)
    with session.no_autoflush:
        stored = session.execute(select(AgentExecution.frozen_context).where(
            AgentExecution.run_id == run.id,
            AgentExecution.organization_id == run.organization_id,
            AgentExecution.store_id == run.store_id)).first()
    actual = LEGACY_RUNTIME if stored is None else runtime_identity(stored[0])
    if actual != expected:
        _fail("agent_runtime_mismatch")


def legacy_run_clause(session: Session):
    control = select(AgentExecution.run_id).where(
        AgentExecution.run_id == AgentRun.id,
        AgentExecution.organization_id == AgentRun.organization_id,
        AgentExecution.store_id == AgentRun.store_id)
    return or_(~control.exists(), control.where(runtime_candidate_clause(session)).exists())


@dataclass(frozen=True)
class AgentClaim:
    run_id: str
    organization_id: str
    store_id: str
    epoch: int
    machine_id: str
    runtime_identity: tuple = LEGACY_RUNTIME


def _db_clock(session: Session) -> datetime:
    """Read fresh DB time: PostgreSQL clock_timestamp, SQLite milliseconds."""
    if session.get_bind().dialect.name == "postgresql":
        return _timestamp(session.execute(text("SELECT clock_timestamp()")).scalar_one())
    value = session.execute(text("SELECT strftime('%Y-%m-%d %H:%M:%f','now')")).scalar_one()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _timestamp(value)


def execution_for(session: Session, org: str, store: str, run_id: str,
                  *, for_update: bool = False) -> AgentExecution | None:
    """Read one control row; ``for_update`` joins the execution lock domain."""
    query = select(AgentExecution).where(
        AgentExecution.organization_id == org, AgentExecution.store_id == store,
        AgentExecution.run_id == run_id,
    )
    if for_update:
        query = query.with_for_update()
    return session.scalar(query.execution_options(populate_existing=True))


def _scope_epoch(session: Session, organization_id: str, store_id: str,
                 *, exclusive: bool = False) -> int:
    """Take the shared scope fence and return the epoch observed under it."""
    from backend.app.modules.identity.command_guard import (
        ScopeSecurityFenceError, lock_scope_security_fence,
    )
    try:
        return lock_scope_security_fence(session, organization_id, store_id,
                                         exclusive=exclusive)
    except ScopeSecurityFenceError:
        _fail("agent_execution_authorization_unknown")


def current_authorization_epoch(session: Session, organization_id: str,
                                store_id: str) -> int:
    """Public read of the scope-fence epoch, taken in the caller's transaction.

    Every durable authorization writer freezes THIS value: a submit freezes it
    into the queued control row and an explicit human approval re-freezes it
    into a one-shot approval grant. Nothing else may mint or refresh it, and a
    NULL/stale value is always treated as unknown authorization.
    """
    return _scope_epoch(session, organization_id, store_id)


def _cas_execution(session: Session, row: AgentExecution, *, values: dict,
                   expected_epoch: int | None = None, require_live: bool = False) -> None:
    _reject_runtime_mutations(session)
    if "frozen_context" in values:
        with session.no_autoflush:
            stored = session.execute(select(AgentExecution.frozen_context).where(
                AgentExecution.run_id == row.run_id,
                AgentExecution.organization_id == row.organization_id,
                AgentExecution.store_id == row.store_id)).scalar_one()
        if _runtime_stamp(stored) != _runtime_stamp(values["frozen_context"]):
            _fail("agent_runtime_binding_immutable")
        if (type(stored) is dict and 'pi_online_campaign' in stored
                and stored['pi_online_campaign'] != values['frozen_context'].get('pi_online_campaign')):
            _fail('agent_pi_campaign_immutable')
    current = _db_clock(session)
    q = update(AgentExecution).where(
        AgentExecution.run_id == row.run_id,
        AgentExecution.organization_id == row.organization_id,
        AgentExecution.store_id == row.store_id,
        AgentExecution.version == row.version,
        AgentExecution.epoch == (row.epoch if expected_epoch is None else expected_epoch),
        AgentExecution.phase == row.phase,
    )
    if require_live:
        q = q.where(AgentExecution.lease_until > current,
                    AgentExecution.cancel_requested_at.is_(None))
    outcome = session.execute(q.values(**values, version=AgentExecution.version + 1,
                                       updated_at=current)
                              .execution_options(synchronize_session=False))
    if outcome.rowcount != 1:
        _fail("agent_execution_fenced")
    session.expire_all()


def _grant_dict(row: AgentExecution) -> dict | None:
    grant = row.approval_grant
    return dict(grant) if isinstance(grant, dict) else None


def _grant_pending(row: AgentExecution) -> dict | None:
    """The frozen one-shot approval, only while it has not been consumed."""
    grant = _grant_dict(row)
    if grant is None or grant.get("v") != 1 or grant.get("consumed_at") is not None:
        return None
    return grant


def _grant_time(grant: dict, key: str) -> datetime | None:
    value = grant.get(key)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # a naive or corrupted deadline fails closed
    return parsed


def _grant_bound(grant: dict, row: AgentExecution) -> bool:
    """Every frozen field must be strictly typed and still bound to this row.

    ``bool`` is not accepted as an integer, the approver must be a real subject
    id (``None`` never bypasses the person check) and the approval window must
    be internally consistent: an approval never outlives the moment it was
    granted.
    """
    approver = grant.get("approver_subject_id")
    approved = _grant_time(grant, "approved_at")
    expires = _grant_time(grant, "expires_at")
    return (
        type(grant.get("v")) is int and grant["v"] == 1
        and isinstance(approver, str) and bool(approver)
        and approved is not None and expires is not None and approved < expires
        and grant.get("run_id") == row.run_id
        and grant.get("authorization_epoch") == row.authorization_epoch
        and grant.get("account_id") == row.account_id
        and grant.get("session_id") == row.session_id
        and grant.get("account_version") == row.account_version
        and _positive_int(grant.get("authorization_epoch"))
        and _positive_int(grant.get("account_version"))
        and _positive_int(grant.get("confirmation_version"))
        and _positive_int(grant.get("run_version"))
        and _positive_int(grant.get("step_version"))
        and _positive_int(grant.get("request_run_version"))
        and _positive_int(grant.get("request_step_version"))
    )


def _grant_claimable(session: Session, row: AgentExecution, run: AgentRun,
                     grant: dict, now: datetime) -> bool:
    """Every field of the frozen approval must still match live durable state."""
    # Malformed grants are never claimable: bools are not integers, and every
    # frozen version/epoch/account version must be a positive integer.
    if (type(grant.get("v")) is not int or grant["v"] != 1
            or not _positive_int(grant.get("account_version"))
            or not _positive_int(grant.get("authorization_epoch"))
            or not _positive_int(grant.get("confirmation_version"))
            or not _positive_int(grant.get("run_version"))
            or not _positive_int(grant.get("step_version"))
            or not _positive_int(grant.get("request_run_version"))
            or not _positive_int(grant.get("request_step_version"))):
        return False
    if not _grant_bound(grant, row):
        return False
    if grant.get("approver_subject_id") != run.requester_id:
        return False
    expires = _grant_time(grant, "expires_at")
    approved = _grant_time(grant, "approved_at")
    if (expires is None or approved is None or approved > now
            or expires <= now or _timestamp(run.deadline_at) <= now
            or expires > _timestamp(run.deadline_at)):
        return False
    person_expiry = _live_person_expiry(session, row, now,
                                        subject_id=grant.get("approver_subject_id"))
    if person_expiry is None or expires > person_expiry:
        return False
    confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
    if confirmation is None or confirmation.state != "executing":
        return False
    if (grant.get("confirmation_id") != confirmation.run_id
            or grant.get("confirmation_version") != confirmation.version
            or grant.get("input_digest") != confirmation.input_digest
            or grant.get("tool_key") != confirmation.tool_key
            or grant.get("tool_version") != confirmation.tool_version
            or _timestamp(confirmation.expires_at) <= now
            or expires > _timestamp(confirmation.expires_at)):
        return False
    step = session.scalar(select(AgentStep).where(
        AgentStep.organization_id == run.organization_id,
        AgentStep.store_id == run.store_id,
        AgentStep.run_id == run.id,
        AgentStep.id == grant.get("step_id"),
    ).execution_options(populate_existing=True))
    if step is None or step.state != StepState.PENDING.value:
        return False
    if step.id != confirmation.step_id:
        return False
    if grant.get("run_version") != run.version or grant.get("step_version") != step.version:
        return False
    return True


def _load_run_step(session: Session, run: AgentRun, step_id: str) -> AgentStep:
    step = session.scalar(select(AgentStep).where(
        AgentStep.organization_id == run.organization_id,
        AgentStep.store_id == run.store_id,
        AgentStep.run_id == run.id,
        AgentStep.id == step_id,
    ).execution_options(populate_existing=True))
    if step is None:
        _fail("agent_not_found")
    return step


def _abort_active_steps(session: Session, run: AgentRun, *, code: str) -> AgentRun:
    """Close pending/running steps so a terminal Run transition is legal."""
    for step in list_steps(session, run.organization_id, run.store_id, run.id):
        if step.state == StepState.PENDING.value:
            change = StepTransition(to_state=StepState.CANCELLED)
        elif step.state == StepState.RUNNING.value:
            change = StepTransition(to_state=StepState.UNKNOWN, error_code=code)
        else:
            continue
        transition_step(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, step_id=step.id,
            expected_run_version=run.version, expected_step_version=step.version,
            change=change)
        refreshed = get_run(session, run.organization_id, run.store_id, run.id)
        if refreshed is None:
            _fail("agent_not_found")
        run = refreshed
    return run


def _end_run(session: Session, run: AgentRun, state: RunState, *,
             now: datetime) -> AgentRun:
    if run.state in _TERMINAL_RUN_STATES:
        return run
    run = _abort_active_steps(session, run, code="agent_interrupted")
    if run.state in _TERMINAL_RUN_STATES:
        return run
    return transition_run(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, expected_version=run.version,
        to_state=state, now=now)


def _close_control(session: Session, row: AgentExecution, *, phase: str,
                   dispatch_state: str, effect_status: str,
                   resolution: str | None = None, now: datetime,
                   requester_id: str | None = None) -> None:
    """CAS the control row into a closed phase; never touches a live owner twice."""
    values: dict = {
        "phase": phase,
        "owner_machine_id": None,
        "lease_until": None,
        "dispatch_state": dispatch_state,
        "effect_status": effect_status,
        "epoch": row.epoch + 1,
    }
    if resolution is not None:
        values["cancel_resolution"] = resolution
    if requester_id is not None:
        values["cancel_requested_at"] = now
        values["cancel_requested_by"] = requester_id
    _cas_execution(session, row, values=values)


def enqueue_worker_execution(session: Session, run: AgentRun, *, actor,
                             origin_message_id: str, frozen_context: dict,
                             authorization_epoch: int | None = None) -> AgentExecution:
    """Stage the durable queued control row; the HTTP caller never dispatches.

    ``authorization_epoch`` is the real scope-fence epoch frozen under the fence
    by the submitting transaction. ``None`` is reserved for legacy rows/tests
    and is treated by every claim/cancel path as an unknown authorization which
    fails shut and is never backfilled.
    """
    runtime_identity(frozen_context)
    if authorization_epoch is not None and not _positive_int(authorization_epoch):
        _fail("agent_execution_authorization_invalid")
    now = _db_clock(session)
    if (actor.account_id is None or actor.session_id is None
            or actor.account_version is None or run.requester_id != actor.subject_id
            or (run.organization_id, run.store_id) != (actor.organization_id, actor.store_id)
            or run.state != RunState.CREATED.value or run.input_ref is None
            or actor.auth_expires_at <= now or run.deadline_at <= now):
        _fail("agent_execution_authorization_invalid")
    entry = AgentExecution(
        run_id=run.id, organization_id=run.organization_id, store_id=run.store_id,
        mode="worker_v2", phase="queued", version=1, epoch=0,
        account_id=actor.account_id, session_id=actor.session_id,
        account_version=actor.account_version, authorization_expires_at=actor.auth_expires_at,
        authorization_epoch=authorization_epoch,
        origin_message_id=origin_message_id, input_digest=run.input_ref.removeprefix("sha256:"),
        frozen_context=frozen_context, cancel_resolution="not_requested", effect_status="none",
        dispatch_state="not_dispatched", created_at=now, updated_at=now,
    )
    session.add(entry)
    session.flush()
    return entry


def _close_executing_confirmation(session: Session, run_id: str, *, dispatched: bool,
                                  now: datetime) -> bool:
    """Close an orphaned ``executing`` approval in the caller's transaction.

    A dispatch that may have reached the process becomes ``unknown``; a settled
    attempt with no dispatch proof becomes ``failed``. An already-approved or
    rejected decision is never rewritten, and the human decision reason is kept.
    """
    confirmation = session.scalar(select(AgentConfirmation).where(
        AgentConfirmation.run_id == run_id,
        AgentConfirmation.state == "executing",
    ).with_for_update())
    if confirmation is None:
        return False
    set_confirmation_state(session, confirmation,
                           "unknown" if dispatched else "failed",
                           reason=(confirmation.decision_reason or
                                   ("agent_worker_unknown" if dispatched
                                    else "agent_worker_not_dispatched")))
    return True


def _close_authorization_unknown(session: Session, row: AgentExecution, run: AgentRun,
                                 now: datetime) -> None:
    """A stale or NULL authorization can only be closed, never refreshed."""
    dispatched = row.dispatch_state in ("dispatched", "unknown")
    _close_control(session, row, phase="unknown",
                   dispatch_state="unknown" if dispatched else row.dispatch_state,
                   effect_status="external_unknown" if dispatched else "none",
                   now=now)
    _end_run(session, run, RunState.FAILED, now=now)
    _close_executing_confirmation(session, run.id, dispatched=dispatched, now=now)


def _close_dispatch_unknown(session: Session, row: AgentExecution, run: AgentRun,
                            now: datetime) -> None:
    """A call may have left the process: preserve unknown, never replay it."""
    _close_control(session, row, phase="unknown", dispatch_state="unknown",
                   effect_status="external_unknown", resolution="unknown",
                   now=now)
    _end_run(session, run, RunState.FAILED, now=now)
    _close_executing_confirmation(session, run.id, dispatched=True, now=now)


def _close_interrupted(session: Session, row: AgentExecution, run: AgentRun,
                       now: datetime) -> None:
    """A settled attempt with no live process is interrupted, not re-run."""
    _close_control(session, row, phase="finished", dispatch_state="settled",
                   effect_status="none", now=now)
    _end_run(session, run, RunState.FAILED, now=now)
    _close_executing_confirmation(session, run.id, dispatched=False, now=now)


def _execution_candidate_ids(session: Session, query):
    """Walk a finite scoped queue in stable pages without a fixed-prefix trap.

    The caller holds the scope fence exclusively through its transaction. IDs
    and ordering keys are plain values so execution CAS/expire_all cannot change
    our cursor. A closed/skipped row never shifts an OFFSET or consumes the
    caller's claim/close allowance. Only one page is retained at a time.
    """
    cursor = None
    while True:
        page_query = query.add_columns(AgentExecution.created_at)
        if cursor is not None:
            created_at, run_id = cursor
            page_query = page_query.where(or_(
                AgentExecution.created_at > created_at,
                and_(AgentExecution.created_at == created_at,
                     AgentExecution.run_id > run_id),
            ))
        page = session.execute(page_query.order_by(
            AgentExecution.created_at, AgentExecution.run_id).limit(32)).all()
        if not page:
            return
        # Save the page boundary before the caller can close or expire any row.
        cursor = (page[-1][1], page[-1][0])
        for run_id, _created_at in page:
            yield run_id


def claim_worker_execution(session: Session, *, organization_id: str, store_id: str,
                           machine_id: str, lease_seconds: int = 30,
                           expected_runtime: tuple = LEGACY_RUNTIME,
                           run_id: str | None = None) -> AgentClaim | None:
    """Claim <=1 legal control row under the scope fence; never call a Provider.

    Lock order is the human command order: the scope fence is held first, then
    the bound employee account/session rows are re-read under lock, and only
    then the execution row itself (never the reverse). ``epoch`` is the claim
    fencing generation: every successful claim increments it, so a late owner
    can never publish a result. Rows whose frozen authorization epoch is NULL
    or stale, or whose bound employee account/session/version is no longer live,
    are closed as unknown authorization and never refreshed.
    """
    validate_runtime_identity(expected_runtime)
    _reject_runtime_mutations(session)
    if expected_runtime == SDK_RUNTIME and (type(run_id) is not str or not run_id):
        _fail("agent_runtime_exact_run_required")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        _fail("agent_lease_invalid")
    # A claim is a writer in the shared fence domain: hold it exclusively so a
    # concurrent human command cannot decide between our read and our CAS.
    epoch = _scope_epoch(session, organization_id, store_id, exclusive=True)
    now = _db_clock(session)
    candidates = select(AgentExecution.run_id).where(
        AgentExecution.organization_id == organization_id,
        AgentExecution.store_id == store_id,
        AgentExecution.mode == "worker_v2",
        runtime_candidate_clause(session, expected_runtime),
        AgentExecution.cancel_requested_at.is_(None),
        AgentExecution.authorization_expires_at > now,
        AgentExecution.phase.in_(("queued", "claimed")),
    )
    if run_id is not None:
        candidates = candidates.where(AgentExecution.run_id == run_id)
    for run_id in _execution_candidate_ids(session, candidates):
        snapshot = execution_for(session, organization_id, store_id, run_id)
        if snapshot is None or snapshot.phase not in ("queued", "claimed"):
            continue
        run = get_run(session, organization_id, store_id, run_id)
        if run is None or run.state in _TERMINAL_RUN_STATES:
            continue
        try:
            if runtime_identity(snapshot.frozen_context) != expected_runtime:
                continue
        except AgentPersistenceError:
            continue
        # Employee account/session locks MUST precede the execution lock so a
        # human approval transaction (account -> execution) cannot deadlock.
        live_person, _ = _execution_authority(session, snapshot, run, now)
        row = execution_for(session, organization_id, store_id, run_id,
                            for_update=True)
        if row is None or row.phase not in ("queued", "claimed"):
            continue
        run = get_run(session, organization_id, store_id, run_id)
        if run is None or run.state in _TERMINAL_RUN_STATES:
            continue
        try:
            if runtime_identity(row.frozen_context) != expected_runtime:
                continue
        except AgentPersistenceError:
            continue
        current_live, current_epoch = _execution_authority(session, row, run, now)
        if not live_person or not current_live:
            # Bound account/session/version gone: authorization is unknown.
            _close_authorization_unknown(session, row, run, now)
            continue
        if row.phase == "claimed" and row.lease_until is not None and row.lease_until > now:
            continue
        if row.authorization_epoch is None or row.authorization_epoch != current_epoch:
            _close_authorization_unknown(session, row, run, now)
            continue
        if row.dispatch_state in ("dispatched", "unknown"):
            # NEVER restart a request which may have reached a Provider.
            _close_dispatch_unknown(session, row, run, now)
            continue
        grant = _grant_pending(row)
        if row.phase == "claimed" and row.dispatch_state == "settled":
            # The owner settled an attempt and then died; no live loop continues.
            _close_interrupted(session, row, run, now)
            continue
        if run.state == RunState.CREATED.value:
            if grant is not None or row.dispatch_state != "not_dispatched":
                continue
        elif run.state == RunState.RUNNING.value:
            # A worker Run is only claimable through one frozen human approval.
            if grant is None or not _grant_claimable(session, row, run, grant, now):
                continue
            if row.dispatch_state not in ("not_dispatched", "settled"):
                continue
        else:
            # waiting_for_human / waiting_for_external never auto-claim.
            continue
        new_epoch = row.epoch + 1  # Freeze BEFORE _cas_execution expires ORM state.
        try:
            _cas_execution(session, row, values={
                "phase": "claimed", "owner_machine_id": machine_id,
                "epoch": new_epoch,
                "lease_until": now + timedelta(seconds=lease_seconds),
            })
        except AgentPersistenceError:
            session.expire_all()
            continue
        return AgentClaim(run.id, organization_id, store_id, new_epoch, machine_id, expected_runtime)
    return None


def require_worker_claim(session: Session, claim: AgentClaim,
                         *, permit_cancel: bool = False,
                         expected_runtime: tuple = LEGACY_RUNTIME) -> AgentExecution:
    validate_runtime_identity(expected_runtime)
    _reject_runtime_mutations(session)
    if not isinstance(claim, AgentClaim) or claim.runtime_identity != expected_runtime:
        _fail("agent_runtime_mismatch")
    row = execution_for(session, claim.organization_id, claim.store_id, claim.run_id)
    if row is not None and runtime_identity(row.frozen_context) != expected_runtime:
        _fail("agent_runtime_mismatch")
    now = _db_clock(session)
    if (row is None or row.mode != "worker_v2" or row.phase != "claimed"
            or row.owner_machine_id != claim.machine_id or row.epoch != claim.epoch
            or row.lease_until is None or row.lease_until <= now
            or row.authorization_expires_at <= now
            or (not permit_cancel and row.cancel_requested_at is not None)):
        _fail("agent_execution_fenced")
    return row


def heartbeat_worker_execution(session: Session, claim: AgentClaim,
                               *, lease_seconds: int = 30, expected_runtime: tuple = LEGACY_RUNTIME) -> None:
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        _fail("agent_lease_invalid")
    row = require_worker_claim(session, claim, expected_runtime=expected_runtime)
    now = _db_clock(session)
    _cas_execution(session, row, values={"lease_until": now + timedelta(seconds=lease_seconds)},
                   expected_epoch=claim.epoch, require_live=True)


def record_worker_dispatch(session: Session, claim: AgentClaim,
                           *, step_id: str | None = None, expected_runtime: tuple = LEGACY_RUNTIME) -> None:
    """Persist the dispatch intent BEFORE any Provider/tool call leaves.

    The first attempt may only move ``not_dispatched`` to ``dispatched``. A
    settled step inside the SAME live claim may start one further attempt, but
    only for its own real Step: current step RUNNING, unique next sequence, all
    prior steps SUCCEEDED, no unknown/running older attempt, and the frozen
    approval (when present) pointing at exactly this step.
    """
    row = require_worker_claim(session, claim, expected_runtime=expected_runtime)
    if step_id is None:
        # Legacy synthetic first attempt: no Step is asserted, so it can never
        # license a second business attempt.
        if row.dispatch_state != "not_dispatched":
            _fail("agent_execution_unknown_no_retry")
        _cas_execution(session, row, values={"dispatch_state": "dispatched"},
                       expected_epoch=claim.epoch, require_live=True)
        return
    if not isinstance(step_id, str) or not step_id:
        _fail("agent_execution_dispatch_invalid")
    run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
    if run is None:
        _fail("agent_not_found")
    steps = list_steps(session, run.organization_id, run.store_id, run.id)
    target = next((item for item in steps if item.id == step_id), None)
    if target is None or target.state != StepState.RUNNING.value:
        _fail("agent_execution_dispatch_invalid")
    if target.sequence_no != len(steps):
        _fail("agent_execution_dispatch_invalid")
    if any(item.state != StepState.SUCCEEDED.value for item in steps
           if item.sequence_no < target.sequence_no):
        _fail("agent_execution_prior_step_unsettled")
    if row.dispatch_state == "not_dispatched":
        if target.sequence_no != 1 or len(steps) != 1:
            _fail("agent_execution_dispatch_invalid")
    elif row.dispatch_state != "settled":
        _fail("agent_execution_unknown_no_retry")
    grant = _grant_pending(row)
    if grant is not None and grant.get("step_id") != step_id:
        _fail("agent_execution_dispatch_invalid")
    _cas_execution(session, row, values={"dispatch_state": "dispatched"},
                   expected_epoch=claim.epoch, require_live=True)


def settle_worker_attempt(session: Session, claim: AgentClaim, *, step_id: str,
                          outcome: str, expected_runtime: tuple = LEGACY_RUNTIME) -> None:
    """Settle one dispatched attempt whose Step is durably SUCCEEDED.

    The caller owns the transaction and the result writes; this primitive never
    commits, never calls a tool and never resumes a dead loop. ``continue``
    keeps the live claim for the next Step, ``waiting`` parks the control as
    queued without an owner, ``finished`` closes the control.
    """
    if outcome not in ("continue", "waiting", "finished"):
        _fail("agent_execution_invalid_outcome")
    row = require_worker_claim(session, claim, expected_runtime=expected_runtime)
    if row.dispatch_state != "dispatched":
        _fail("agent_execution_dispatch_missing")
    run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
    if run is None:
        _fail("agent_not_found")
    steps = list_steps(session, run.organization_id, run.store_id, run.id)
    target = next((item for item in steps if item.id == step_id), None)
    if target is None or target.state != StepState.SUCCEEDED.value:
        _fail("agent_execution_attempt_unsettled")
    if target.sequence_no != len(steps):
        # A human approval can already be parked as the newest pending Step: the
        # settled attempt is then the step just before it, and that pending Step
        # is exactly the one the pending confirmation owns. Anything else is an
        # out-of-order settlement.
        last = steps[-1]
        confirmation = get_confirmation(session, run.organization_id,
                                        run.store_id, run.id)
        if not (outcome == "waiting"
                and target.sequence_no == len(steps) - 1
                and last.state == StepState.PENDING.value
                and confirmation is not None
                and confirmation.step_id == last.id
                and confirmation.state == "pending"):
            _fail("agent_execution_attempt_unsettled")
    if outcome == "continue":
        if run.state != RunState.RUNNING.value:
            _fail("agent_execution_attempt_unsettled")
        values = {"dispatch_state": "settled"}
    elif outcome == "waiting":
        if run.state != RunState.WAITING_FOR_HUMAN.value:
            _fail("agent_execution_attempt_unsettled")
        values = {"phase": "queued", "owner_machine_id": None, "lease_until": None,
                  "dispatch_state": "settled"}
    else:
        if run.state not in _TERMINAL_RUN_STATES:
            _fail("agent_execution_attempt_unsettled")
        values = {"phase": "finished", "owner_machine_id": None, "lease_until": None,
                  "dispatch_state": "settled"}
    _cas_execution(session, row, values=values, expected_epoch=claim.epoch, require_live=True)


def settle_worker_control(session: Session, claim: AgentClaim, *, outcome: str,
                          effect_status: str = "none") -> None:
    row = require_worker_claim(session, claim)
    if outcome not in ("finished", "unknown"):
        _fail("agent_execution_invalid_outcome")
    if outcome == "finished" and row.dispatch_state != "dispatched":
        _fail("agent_execution_dispatch_missing")
    values = {
        "phase": outcome, "owner_machine_id": None, "lease_until": None,
        "dispatch_state": "settled" if outcome == "finished" else "unknown",
        "effect_status": effect_status if outcome == "finished" else "external_unknown",
    }
    _cas_execution(session, row, values=values, expected_epoch=claim.epoch, require_live=True)


def reject_worker_confirmation(session: Session, *, run: AgentRun,
                               confirmation: AgentConfirmation, step: AgentStep,
                               reason: str) -> AgentExecution:
    """Reject a pending worker approval: cancel its Step, Run and control row.

    A pending confirmation means nothing was dispatched, so this is a definite
    local close: no Provider or tool call happens, no Task exists, and any later
    approval is refused because the control row is already cancelled.
    """
    row = execution_for(session, run.organization_id, run.store_id, run.id,
                        for_update=True)
    if row is None or row.mode != "worker_v2":
        _fail("agent_execution_not_found")
    if row.phase in ("finished", "cancelled", "unknown") or row.cancel_requested_at is not None:
        _fail("agent_execution_fenced")
    if row.dispatch_state not in ("not_dispatched", "settled"):
        _fail("agent_execution_fenced")
    if (run.state != RunState.WAITING_FOR_HUMAN.value
            or confirmation.state != "pending"
            or step.state != StepState.PENDING.value or step.id != confirmation.step_id):
        _fail("agent_execution_fenced")
    now = _db_clock(session)
    transition_step(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, step_id=step.id,
        expected_run_version=run.version, expected_step_version=step.version,
        change=StepTransition(to_state=StepState.CANCELLED))
    set_confirmation_state(session, confirmation, "rejected", reason=reason)
    refreshed = get_run(session, run.organization_id, run.store_id, run.id)
    if refreshed is None:
        _fail("agent_not_found")
    _end_run(session, refreshed, RunState.CANCELLED, now=now)
    # ``settled`` means every earlier attempt and its Step are already durable,
    # so a rejection is still a definite local close with no external effect.
    _close_control(session, row, phase="cancelled",
                   dispatch_state=row.dispatch_state,
                   effect_status="none", resolution="cancelled", now=now)
    updated = execution_for(session, run.organization_id, run.store_id, run.id)
    if updated is None:
        _fail("agent_execution_not_found")
    return updated


def freeze_approval_grant(session: Session, *, run: AgentRun, step: AgentStep,
                          confirmation: AgentConfirmation, actor,
                          authorization_epoch: int,
                          request_run_version: int,
                          request_step_version: int) -> tuple[AgentExecution, dict]:
    """Freeze ONE exact, already-approved write into a queued control row.

    Runs only inside the real human approval transaction, after the fence and
    the approver's live session/permissions were revalidated. It never runs a
    tool, never creates a Task and never renews an earlier approval: a fresh
    approval is what authorizes the new epoch.
    """
    if not _positive_int(authorization_epoch):
        _fail("agent_execution_authorization_invalid")
    # A caller epoch only authorizes a grant while it is the REAL current scope
    # epoch. A stale row epoch is fine (this explicit approval updates it); a
    # fabricated or outdated caller epoch can never mint authority.
    # Approval callers already hold the shared fence and employee locks. Never
    # upgrade here: a second shared-fence holder waiting for Store would make
    # that upgrade deadlock. The shared fence blocks epoch writers until commit;
    # the execution row/CAS serializes approval writes.
    if _scope_epoch(session, run.organization_id, run.store_id) != authorization_epoch:
        _fail("agent_execution_authorization_unknown")
    row = execution_for(session, run.organization_id, run.store_id, run.id,
                        for_update=True)
    if row is None or row.mode != "worker_v2":
        _fail("agent_execution_not_found")
    if row.phase in ("finished", "cancelled", "unknown") or row.cancel_requested_at is not None:
        _fail("agent_execution_fenced")
    if row.dispatch_state not in ("not_dispatched", "settled"):
        _fail("agent_execution_fenced")
    if row.authorization_epoch is None:
        _fail("agent_execution_authorization_unknown")
    if not _positive_int(authorization_epoch):
        _fail("agent_execution_authorization_invalid")
    if (not _positive_int(request_run_version) or not _positive_int(request_step_version)
            or not _positive_int(run.version) or not _positive_int(step.version)):
        _fail("agent_invalid_data")
    # The grant is bound to the REAL approving session of THIS transaction, not
    # to whatever session the row was queued under. The requester identity is
    # still the run owner: a worker never re-signs authority for someone else.
    if (run.requester_id != actor.subject_id
            or actor.organization_id != run.organization_id
            or actor.store_id != run.store_id
            or actor.account_id is None or actor.session_id is None
            or not _positive_int(actor.account_version)):
        _fail("agent_execution_authorization_invalid")
    now = _db_clock(session)
    expires_at = min(_timestamp(confirmation.expires_at),
                     _timestamp(actor.auth_expires_at),
                     _timestamp(run.deadline_at))
    if expires_at <= now:
        _fail("agent_confirmation_expired")
    grant = {
        "v": 1,
        "run_id": run.id,
        "step_id": step.id,
        # AgentConfirmation is keyed by run_id, so that is its durable identity.
        "confirmation_id": confirmation.run_id,
        "input_digest": confirmation.input_digest,
        "tool_key": confirmation.tool_key,
        "tool_version": confirmation.tool_version,
        "confirmation_version": confirmation.version,
        "request_run_version": request_run_version,
        "request_step_version": request_step_version,
        "run_version": run.version,
        "step_version": step.version,
        "approver_subject_id": actor.subject_id,
        "account_id": actor.account_id,
        "session_id": actor.session_id,
        "account_version": actor.account_version,
        "authorization_epoch": authorization_epoch,
        "approved_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "consumed_at": None,
        "consumed_epoch": None,
        "consumed_by_claim": None,
    }
    _cas_execution(session, row, values={
        "phase": "queued", "owner_machine_id": None, "lease_until": None,
        "epoch": row.epoch + 1, "dispatch_state": "settled",
        "authorization_epoch": authorization_epoch,
        "account_id": actor.account_id,
        "session_id": actor.session_id,
        "account_version": actor.account_version,
        "authorization_expires_at": _timestamp(actor.auth_expires_at),
        "approval_grant": grant,
        "cancel_resolution": "not_requested", "effect_status": "none",
    })
    updated = execution_for(session, run.organization_id, run.store_id, run.id)
    if updated is None:
        _fail("agent_execution_not_found")
    return updated, grant


def _execution_authority(session, row, run, now):
    """Execution-only identity; human approvals retain _live_person unchanged."""
    from fastapi import HTTPException
    from backend.app.modules.assistant import principal
    try:
        person = principal.from_execution(session, row, run)
        if person is None:
            return _live_person(session, row, now), _scope_epoch(session, row.organization_id, row.store_id)
        if runtime_identity(row.frozen_context) != SDK_RUNTIME or row.approval_grant is not None:
            return False, None
        principal.check(session, person, now=now, lock=True)
        return True, person.work_epoch
    except HTTPException:
        return False, None


def _live_person(session: Session, row: AgentExecution, now: datetime,
                 *, subject_id: str | None = None) -> bool:
    """Re-read the REAL bound account/session under lock; never a snapshot."""
    return _live_person_expiry(session, row, now, subject_id=subject_id) is not None


def _live_person_expiry(session: Session, row: AgentExecution, now: datetime,
                        *, subject_id: str | None = None) -> datetime | None:
    """The live session expiry of the bound employee, or ``None`` if stale.

    Locks follow the human command order (account/session rows, then the
    execution row), so a revocation that commits before this read is observed.
    Scope, active flag, account version, lockout, session revocation and expiry
    are all re-checked. ``subject_id`` additionally pins the approver identity.
    """
    from backend.app.modules.auth.models import EmployeeAccount, LoginSession
    if (row.account_id is None or row.session_id is None
            or not _positive_int(row.account_version)):
        return None
    if subject_id is not None and (not isinstance(subject_id, str) or not subject_id):
        return None
    account = session.scalar(select(EmployeeAccount).where(
        EmployeeAccount.id == row.account_id,
        EmployeeAccount.organization_id == row.organization_id,
        EmployeeAccount.store_id == row.store_id,
    ).with_for_update().execution_options(populate_existing=True))
    if account is None:
        return None
    login = session.scalar(select(LoginSession).where(
        LoginSession.id == row.session_id,
        LoginSession.account_id == row.account_id,
    ).with_for_update().execution_options(populate_existing=True))
    if login is None:
        return None
    if not account.active or account.version != row.account_version:
        return None
    if subject_id is not None and account.subject_id != subject_id:
        return None
    if login.revoked_at is not None or login.account_version != row.account_version:
        return None
    locked = account.locked_until
    if locked is not None:
        if isinstance(locked, str):
            locked = datetime.fromisoformat(locked)
        if locked.tzinfo is None:  # SQLite returns naive DATETIME values
            locked = locked.replace(tzinfo=timezone.utc)
        if _timestamp(locked) > now:
            return None
    expires = login.expires_at
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if expires.tzinfo is None:  # SQLite returns naive DATETIME values
        expires = expires.replace(tzinfo=timezone.utc)
    expiry = _timestamp(expires)
    return expiry if expiry > now else None


def consume_approval_grant(session: Session, claim: AgentClaim, *, step_id: str,
                           confirmation_id: str) -> dict:
    """Consume the frozen approval exactly once, in the caller's transaction.

    Returns the updated (consumed) grant as a NEW dict. The caller then runs the
    one approved tool and commits both its result and this consumption
    atomically; a rollback leaves the grant unconsumed. The Step must already be
    the RUNNING approved step of this claim, so claim-time version equality is
    not re-applied here. This helper never runs the tool and never commits.
    """
    if (not isinstance(step_id, str) or not step_id
            or not isinstance(confirmation_id, str) or not confirmation_id):
        _fail("agent_approval_invalid")
    row = require_worker_claim(session, claim)
    grant = _grant_pending(row)
    if grant is None:
        _fail("agent_approval_missing")
    now = _db_clock(session)
    # Every integer in the frozen grant rejects bool and non-positive values.
    if (type(grant.get("v")) is not int or grant["v"] != 1
            or not _positive_int(grant.get("account_version"))
            or not _positive_int(grant.get("authorization_epoch"))
            or not _positive_int(claim.epoch)
            or not _positive_int(grant.get("run_version"))
            or not _positive_int(grant.get("step_version"))
            or not _positive_int(grant.get("request_run_version"))
            or not _positive_int(grant.get("request_step_version"))):
        _fail("agent_approval_fenced")
    if not _grant_bound(grant, row):
        _fail("agent_approval_fenced")
    # Lock order: shared scope fence, then the REAL bound
    # person, then the execution row, then Run/Step are read under those locks.
    # Keep an existing shared command guard shared; never upgrade after locks.
    if grant["authorization_epoch"] != _scope_epoch(session, claim.organization_id,
                                                    claim.store_id):
        _fail("agent_approval_fenced")
    if not _live_person(session, row, now, subject_id=grant.get("approver_subject_id")):
        _fail("agent_approval_fenced")
    row = execution_for(session, claim.organization_id, claim.store_id, claim.run_id,
                        for_update=True)
    if (row is None or row.phase != "claimed" or row.epoch != claim.epoch
            or row.owner_machine_id != claim.machine_id):
        _fail("agent_execution_fenced")
    grant = _grant_pending(row)
    if grant is None or not _grant_bound(grant, row):
        _fail("agent_approval_fenced")
    approver = grant.get("approver_subject_id")
    if (grant.get("step_id") != step_id or grant.get("confirmation_id") != confirmation_id
            or not isinstance(approver, str) or not approver
            or not _positive_int(grant.get("account_version"))
            or not _positive_int(grant.get("authorization_epoch"))):
        _fail("agent_approval_invalid")
    run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
    if run is None:
        _fail("agent_not_found")
    if run.state != RunState.RUNNING.value:
        _fail("agent_approval_fenced")
    if approver != run.requester_id:
        _fail("agent_approval_fenced")
    approved_at = _grant_time(grant, "approved_at")
    expires = _grant_time(grant, "expires_at")
    if (approved_at is None or expires is None or expires <= approved_at
            or expires <= now or approved_at > now
            or _timestamp(run.deadline_at) <= now):
        _fail("agent_approval_expired")
    confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
    if confirmation is None or confirmation.state != "executing":
        _fail("agent_approval_fenced")
    if (grant.get("confirmation_id") != confirmation.run_id
            or grant.get("confirmation_version") != confirmation.version
            or grant.get("input_digest") != confirmation.input_digest
            or grant.get("tool_key") != confirmation.tool_key
            or grant.get("tool_version") != confirmation.tool_version
            or _timestamp(confirmation.expires_at) <= now):
        _fail("agent_approval_fenced")
    if _canonical_digest(confirmation.arguments) != confirmation.input_digest:
        _fail("agent_approval_fenced")
    if grant.get("step_id") != step_id or grant.get("confirmation_id") != confirmation_id:
        _fail("agent_approval_invalid")
    step = _load_run_step(session, run, step_id)
    if step.id != confirmation.step_id or step.state != StepState.RUNNING.value:
        _fail("agent_approval_fenced")
    # Exactly ONE legal version growth: the consuming worker took the approved
    # Step PENDING -> RUNNING (which also bumped the Run version once). Any
    # other drift is rejected; the frozen view is never re-based.
    if (run.version != grant["run_version"] + 1
            or step.version != grant["step_version"] + 1):
        _fail("agent_approval_fenced")
    if (expires > _timestamp(confirmation.expires_at)
            or expires > _timestamp(run.deadline_at)):
        _fail("agent_approval_fenced")
    person_expiry = _live_person_expiry(session, row, now, subject_id=approver)
    if person_expiry is None or expires > person_expiry:
        _fail("agent_approval_fenced")
    consumed = {
        **grant,
        "consumed_at": now.isoformat(),
        "consumed_epoch": claim.epoch,
        "consumed_by_claim": claim.machine_id,
    }
    _cas_execution(session, row, values={"approval_grant": consumed},
                   expected_epoch=claim.epoch, require_live=True)
    return consumed


def request_worker_cancel(session: Session, run: AgentRun, requester_id: str,
                          *, now: datetime | None = None,
                          for_update: bool = True) -> AgentExecution:
    """Persist the cancel intent in the same execution lock domain as results.

    Three outcomes are decided from durable state, never from optimism: nothing
    was dispatched (definite cancellation, no effect), something may have been
    dispatched (unknown + external_unknown + Run FAILED), or the local Task was
    already committed (too_late receipt is kept, nothing is faked).
    """
    row = execution_for(session, run.organization_id, run.store_id, run.id,
                        for_update=for_update)
    if row is None:
        _fail("agent_execution_not_found")
    if row.cancel_resolution == "too_late" or row.phase in ("cancelled", "unknown"):
        return row  # idempotent: a closed resolution is never reopened
    current = _db_clock(session) if now is None else now
    dispatched = row.dispatch_state in ("dispatched", "unknown")
    current_run = get_run(session, run.organization_id, run.store_id, run.id)
    if current_run is None:
        _fail("agent_not_found")
    if current_run.state == RunState.FAILED.value:
        # A failure is already a durable outcome, not a successful cancellation.
        # In particular, never publish FAILED + cancel_resolution=cancelled,
        # which contradicts the public execution protocol. Unknown/cancelled
        # resolutions were handled idempotently above; preserve every fact.
        _fail("agent_invalid_transition")
    if current_run.state == RunState.COMPLETED.value:
        confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
        effect = row.effect_status
        if confirmation is not None and confirmation.result_task_id is not None:
            effect = "local_task_committed"
        _close_control(session, row, phase="finished",
                       dispatch_state=row.dispatch_state, effect_status=effect,
                       resolution="too_late", now=current, requester_id=requester_id)
        updated = execution_for(session, run.organization_id, run.store_id, run.id)
        if updated is None:
            _fail("agent_execution_not_found")
        return updated
    if dispatched:
        _close_control(session, row, phase="unknown", dispatch_state="unknown",
                       effect_status="external_unknown", resolution="unknown",
                       now=current, requester_id=requester_id)
        refreshed = get_run(session, run.organization_id, run.store_id, run.id)
        if refreshed is None:
            _fail("agent_not_found")
        _end_run(session, refreshed, RunState.FAILED, now=current)
        _close_executing_confirmation(session, run.id, dispatched=True, now=current)
    else:
        # Nothing left the process: close pending Step/confirmation first so the
        # terminal Run transition stays legal, and keep the approval as history.
        _close_control(session, row, phase="cancelled",
                       dispatch_state=row.dispatch_state, effect_status="none",
                       resolution="cancelled", now=current, requester_id=requester_id)
        refreshed = get_run(session, run.organization_id, run.store_id, run.id)
        if refreshed is None:
            _fail("agent_not_found")
        confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
        if confirmation is not None:
            if confirmation.state == "pending":
                set_confirmation_state(session, confirmation, "rejected",
                                       reason="requester_cancelled")
            elif confirmation.state == "executing":
                set_confirmation_state(session, confirmation, "failed",
                                       reason=confirmation.decision_reason)
        refreshed = _abort_active_steps(session, refreshed, code="agent_request_cancelled")
        if refreshed.state not in _TERMINAL_RUN_STATES:
            transition_run(session, organization_id=refreshed.organization_id,
                store_id=refreshed.store_id, run_id=refreshed.id,
                expected_version=refreshed.version, to_state=RunState.CANCELLED,
                now=current)
    updated = execution_for(session, run.organization_id, run.store_id, run.id)
    if updated is None:
        _fail("agent_execution_not_found")
    return updated


def close_cancelled_claim(session: Session, claim: AgentClaim) -> str:
    row = require_worker_claim(session, claim, permit_cancel=True)
    if row.cancel_requested_at is None:
        _fail("agent_execution_cancel_not_requested")
    # ``settled`` means the owner's previous attempt and its Step are already
    # durable, so nothing is in flight: only dispatched/unknown is external.
    outcome = "unknown" if row.dispatch_state in ("dispatched", "unknown") else "cancelled"
    run = get_run(session, row.organization_id, row.store_id, row.run_id)
    if run is None:
        _fail("agent_not_found")
    now = _db_clock(session)
    _cas_execution(session, row, values={
        "phase": outcome, "owner_machine_id": None, "lease_until": None,
        "cancel_resolution": outcome,
        "dispatch_state": "unknown" if outcome == "unknown" else "not_dispatched",
        "effect_status": "external_unknown" if outcome == "unknown" else "none",
    }, expected_epoch=claim.epoch)
    _end_run(session, run, RunState.FAILED if outcome == "unknown" else RunState.CANCELLED,
             now=now)
    _close_executing_confirmation(session, run.id,
                                  dispatched=outcome == "unknown", now=now)
    return outcome


def reap_worker_executions(session: Session, *, organization_id: str,
                           store_id: str, limit: int = 32,
                           run_id: str | None = None) -> int:
    """Fail closed on expired grants, cancelled runs and lost-dispatch leases.

    Only a machine already authorized by the caller may invoke this scoped
    function. Every row is re-read under the same lock order as the human
    command path (fence, bound person, then the execution row), and the
    execution CAS always commits before the Run it closes. Recovery
    distinguishes three durable situations: nothing was dispatched (safe to
    close/cancel), a dispatch intent exists (preserve unknown, never replay) and
    an attempt was settled but its process is gone (Run interrupted/failed,
    never resumed from a summary). No Provider or business tool is ever
    replayed, and no old approval is renewed.

    ``run_id`` optionally narrows the batch to one exact Run for an explicit
    single-Run reconciliation; ``None`` scans the scope in stable pages.
    ``limit`` bounds actual closures, not healthy/skipped candidates, so a live
    prefix cannot permanently hide later expired work. A neighbour Run in the
    same scope is never closed by a filtered call.
    """
    _reject_runtime_mutations(session)
    if type(limit) is not int or not 1 <= limit <= 100:
        _fail("agent_recovery_limit_invalid")
    if run_id is not None:
        allowed = frozenset(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-")
        if (type(run_id) is not str or not 1 <= len(run_id) <= 120
                or not run_id.isascii() or not run_id[0].isalnum()
                or any(char not in allowed for char in run_id)):
            _fail("agent_recovery_run_id_invalid")
    epoch = _scope_epoch(session, organization_id, store_id, exclusive=True)
    now = _db_clock(session)
    query = select(AgentExecution.run_id).where(
        AgentExecution.organization_id == organization_id,
        AgentExecution.store_id == store_id,
        AgentExecution.mode == "worker_v2",
        runtime_candidate_clause(session, LEGACY_RUNTIME),
        AgentExecution.phase.in_(("queued", "claimed")),
    )
    if run_id is not None:
        query = query.where(AgentExecution.run_id == run_id)
    changed = 0
    for candidate_id in _execution_candidate_ids(session, query):
        if changed >= limit:
            break
        snapshot = execution_for(session, organization_id, store_id, candidate_id)
        if snapshot is None or snapshot.phase not in ("queued", "claimed"):
            continue
        run = get_run(session, organization_id, store_id, candidate_id)
        if run is None or run.state in _TERMINAL_RUN_STATES:
            continue
        if runtime_identity(snapshot.frozen_context) != LEGACY_RUNTIME:
            continue
        live_person = _live_person(session, snapshot, now)
        row = execution_for(session, organization_id, store_id, candidate_id,
                            for_update=True)
        if row is None or row.phase not in ("queued", "claimed"):
            continue
        run = get_run(session, organization_id, store_id, candidate_id)
        if run is None or run.state in _TERMINAL_RUN_STATES:
            continue
        if runtime_identity(row.frozen_context) != LEGACY_RUNTIME:
            continue
        if not live_person or not _live_person(session, row, now):
            _close_authorization_unknown(session, row, run, now)
            changed += 1
            continue
        if row.authorization_epoch is None or row.authorization_epoch != epoch:
            _close_authorization_unknown(session, row, run, now)
            changed += 1
            continue
        grant = _grant_pending(row)
        grant_expired = False
        if grant is not None:
            expires = _grant_time(grant, "expires_at")
            grant_expired = expires is None or expires <= now
        lease_lost = (row.phase == "claimed" and row.lease_until is not None
                      and row.lease_until <= now)
        expired = row.authorization_expires_at <= now or run.deadline_at <= now
        cancelled = row.cancel_requested_at is not None
        if row.dispatch_state == "not_dispatched" and lease_lost and not (
                expired or cancelled or grant_expired):
            # Died before dispatch: the next claim may re-fence and re-claim.
            continue
        if not (expired or cancelled or grant_expired or lease_lost):
            continue
        if row.dispatch_state in ("dispatched", "unknown"):
            _close_dispatch_unknown(session, row, run, now)
            changed += 1
            continue
        if row.dispatch_state == "settled":
            # Nothing is in flight, but the settled attempt has no live process.
            # Close the execution CAS first, then the Run it terminates.
            _close_control(session, row, phase="cancelled" if cancelled else "finished",
                           dispatch_state="settled", effect_status="none",
                           resolution="cancelled" if cancelled else None, now=now)
            refreshed = get_run(session, organization_id, store_id, row.run_id)
            if refreshed is not None:
                _end_run(session, refreshed,
                         RunState.CANCELLED if cancelled else RunState.FAILED, now=now)
            _close_executing_confirmation(session, row.run_id, dispatched=False,
                                          now=now)
            changed += 1
            continue
        # not_dispatched: definite local close, no external effect exists. The
        # execution row is closed first; Run/confirmation follow in the same
        # transaction.
        _close_control(session, row, phase="cancelled",
                       dispatch_state="not_dispatched", effect_status="none",
                       resolution="cancelled" if cancelled else None, now=now)
        confirmation = get_confirmation(session, organization_id, store_id, row.run_id)
        if confirmation is not None and confirmation.state in ("pending", "executing"):
            set_confirmation_state(session, confirmation,
                "rejected" if confirmation.state == "pending" else "failed",
                reason=("requester_cancelled" if cancelled
                        else confirmation.decision_reason))
        refreshed = get_run(session, organization_id, store_id, row.run_id)
        if refreshed is not None:
            _end_run(session, refreshed,
                     RunState.CANCELLED if cancelled else RunState.FAILED, now=now)
        changed += 1
    return changed


# AGT-028 receipt writers never create a root, commit, claim or resume a Run.
# Callers use them inside the guarded model/intent/result root. Failure must
# roll back that whole root, not commit a partial receipt independently.
def _pi_receipt_execution(session: Session, claim: AgentClaim) -> AgentExecution:
    row = require_worker_claim(session, claim, expected_runtime=SDK_RUNTIME)
    context = row.frozen_context
    if (type(context) is not dict
            or type(context.get("pi_readonly_binding")) is not dict
            or context["pi_readonly_binding"].get("profile") != pi_record_tool_profile(context.get("pi_runtime_record"))
            or type(context.get("pi_runtime_record")) is not dict):
        _fail("agent_pi_profile_invalid")
    validate_pi_runtime_record(context['pi_runtime_record'])
    root = session.get_transaction()
    if root is None or root.parent is not None:
        _fail("agent_pi_receipt_root_required")
    return row


def pi_tool_receipts(session: Session, organization_id: str, store_id: str,
                     run_id: str) -> list[AgentPiToolReceipt]:
    """Scoped factual read. Authorization belongs to the original GET guard."""
    return list(session.scalars(select(AgentPiToolReceipt).where(
        AgentPiToolReceipt.organization_id == organization_id,
        AgentPiToolReceipt.store_id == store_id,
        AgentPiToolReceipt.run_id == run_id,
    ).order_by(AgentPiToolReceipt.created_at, AgentPiToolReceipt.id)))


def _pi_receipt_query(claim: AgentClaim, native_call_id: str):
    return select(AgentPiToolReceipt).where(
        AgentPiToolReceipt.organization_id == claim.organization_id,
        AgentPiToolReceipt.store_id == claim.store_id,
        AgentPiToolReceipt.run_id == claim.run_id,
        AgentPiToolReceipt.native_call_id == native_call_id,
    ).execution_options(populate_existing=True)


def accept_pi_tool_receipt(session: Session, claim: AgentClaim, *,
                           model_step_id: str, pi_name: str, decision: dict,
                           now: datetime) -> AgentPiToolReceipt:
    """Persist raw native ID in the same root as model success and settlement."""
    row = _pi_receipt_execution(session, claim)
    if (type(decision) is not dict
            or set(decision) != {"tool_key", "version", "arguments", "native_call_id", "toolset_version"}
            or type(decision["native_call_id"]) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", decision["native_call_id"]) is None):
        _fail("agent_pi_call_invalid")
    run = _require_run(session, claim.organization_id, claim.store_id, claim.run_id)
    model_step = _load_run_step(session, run, model_step_id)
    binding = row.frozen_context["pi_readonly_binding"]
    entries = binding.get("tools")
    if (model_step.kind != "model" or model_step.state != "succeeded"
            or decision["toolset_version"] != run.toolset_version
            or type(entries) is not list
            or not any(type(e) is dict and (e.get("name"), e.get("tool_key"), e.get("version"))
                       == (pi_name, decision["tool_key"], decision["version"]) for e in entries)):
        _fail("agent_pi_call_invalid")
    input_digest = _canonical_digest(decision["arguments"])
    if input_digest is None:
        _fail("agent_pi_call_invalid")
    if session.scalar(_pi_receipt_query(claim, decision["native_call_id"])) is not None:
        _fail("agent_pi_call_replay")
    receipt = AgentPiToolReceipt(
        id=str(uuid4()), contract_version=1, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, model_step_id=model_step.id,
        model_kind="model", native_call_id=decision["native_call_id"],
        pi_name=pi_name, tool_key=decision["tool_key"], tool_version=decision["version"],
        toolset_version=run.toolset_version, input_digest=input_digest,
        tool_step_id=None, tool_kind="tool", state="accepted", output_ref=None,
        error_code=None, created_at=_timestamp(now), updated_at=_timestamp(now))
    session.add(receipt)
    # The outer model-root owner handles IntegrityError and rollback. Do not
    # recover a duplicate by querying or dispatching the existing receipt.
    session.flush()
    return receipt


def require_pi_tool_receipt(session: Session, claim: AgentClaim, *,
                            pi_name: str, decision: dict,
                            state: str, tool_step_id: str | None = None) -> AgentPiToolReceipt:
    _pi_receipt_execution(session, claim)
    if (type(decision) is not dict
            or set(decision) != {"tool_key", "version", "arguments", "native_call_id", "toolset_version"}):
        _fail("agent_pi_call_invalid")
    receipt = session.scalar(_pi_receipt_query(claim, decision["native_call_id"]))
    if (receipt is None or receipt.contract_version != 1 or receipt.state != state
            or receipt.model_kind != "model" or receipt.tool_kind != "tool"
            or receipt.tool_step_id != tool_step_id
            or (receipt.pi_name, receipt.tool_key, receipt.tool_version, receipt.toolset_version)
            != (pi_name, decision["tool_key"], decision["version"], decision["toolset_version"])
            or receipt.input_digest != _canonical_digest(decision["arguments"])):
        _fail("agent_pi_call_invalid")
    return receipt


def bind_pi_tool_intent(session: Session, claim: AgentClaim, *,
                        pi_name: str, decision: dict, step: AgentStep,
                        now: datetime) -> AgentPiToolReceipt:
    """Bind the actual 027 ToolCall, never replace its established call key."""
    receipt = require_pi_tool_receipt(session, claim, pi_name=pi_name,
                                      decision=decision, state="accepted")
    call = session.scalar(select(AgentToolCall).where(
        AgentToolCall.organization_id == claim.organization_id,
        AgentToolCall.store_id == claim.store_id,
        AgentToolCall.run_id == claim.run_id, AgentToolCall.step_id == step.id))
    expected_key = "pi:" + str(_canonical_digest([claim.run_id, decision["native_call_id"]]))
    if ((step.organization_id, step.store_id, step.run_id, step.kind, step.state)
            != (claim.organization_id, claim.store_id, claim.run_id, "tool", "running")
            or call is None or call.idempotency_key != expected_key
            or (call.tool_key, call.tool_version, call.input_digest)
            != (receipt.tool_key, receipt.tool_version, receipt.input_digest)):
        _fail("agent_pi_call_invalid")
    result = session.execute(update(AgentPiToolReceipt).where(
        AgentPiToolReceipt.id == receipt.id,
        AgentPiToolReceipt.organization_id == claim.organization_id,
        AgentPiToolReceipt.store_id == claim.store_id,
        AgentPiToolReceipt.run_id == claim.run_id,
        AgentPiToolReceipt.state == "accepted", AgentPiToolReceipt.tool_step_id.is_(None),
    ).values(tool_step_id=step.id, state="dispatch_intent", updated_at=_timestamp(now))
     .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        _fail("agent_conflict")
    session.flush()
    return require_pi_tool_receipt(session, claim, pi_name=pi_name, decision=decision,
                                   state="dispatch_intent", tool_step_id=step.id)


def succeed_pi_tool_receipt(session: Session, claim: AgentClaim, *,
                           pi_name: str, decision: dict, step: AgentStep,
                           now: datetime) -> AgentPiToolReceipt:
    receipt = require_pi_tool_receipt(session, claim, pi_name=pi_name, decision=decision,
                                      state="dispatch_intent", tool_step_id=step.id)
    if ((step.organization_id, step.store_id, step.run_id, step.kind, step.state)
            != (claim.organization_id, claim.store_id, claim.run_id, "tool", "succeeded")
            or not step.output_ref):
        _fail("agent_pi_call_invalid")
    result = session.execute(update(AgentPiToolReceipt).where(
        AgentPiToolReceipt.id == receipt.id,
        AgentPiToolReceipt.organization_id == claim.organization_id,
        AgentPiToolReceipt.store_id == claim.store_id,
        AgentPiToolReceipt.run_id == claim.run_id,
        AgentPiToolReceipt.state == "dispatch_intent", AgentPiToolReceipt.tool_step_id == step.id,
    ).values(state="succeeded", output_ref=step.output_ref, updated_at=_timestamp(now))
     .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        _fail("agent_conflict")
    session.flush()
    committed = require_pi_tool_receipt(session, claim, pi_name=pi_name, decision=decision,
                                   state="succeeded", tool_step_id=step.id)
    # Ephemeral proof that this receipt transition belongs to THIS root, not
    # a later transaction attaching an already committed success.
    session.info.setdefault('_pi_tool_success_roots', {})[committed.id] = session.get_transaction()
    return committed


def validate_pi_runtime_record(value: object) -> dict:
    """Validate a Host-selected non-secret creation record, never authenticate it."""
    from .pi_runtime_contract import contract_for_record
    try:
        contract_for_record(value)
    except ValueError:
        _fail("agent_pi_runtime_record_invalid")
    if (set(value) != {"contract_version", "wire_protocol", "launch_profile", "source_manifest_sha256"}
            or type(value["source_manifest_sha256"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", value["source_manifest_sha256"]) is None):
        _fail("agent_pi_runtime_record_invalid")
    return dict(value)


def pi_record_tool_profile(record):
    validated = validate_pi_runtime_record(record)
    from .pi_runtime_contract import contract_for_record
    return contract_for_record(validated).tool_profile


def pi_provider_receipt(session: Session, claim: AgentClaim, step_id: str):
    return session.scalar(select(AgentPiProviderReceipt).where(
        AgentPiProviderReceipt.organization_id == claim.organization_id,
        AgentPiProviderReceipt.store_id == claim.store_id,
        AgentPiProviderReceipt.run_id == claim.run_id,
        AgentPiProviderReceipt.model_step_id == step_id,
        AgentPiProviderReceipt.epoch == claim.epoch))


def pi_campaign_facts(session: Session, claim: AgentClaim, *, require_completed=False):
    """Internal complete history; no epoch or success filter can reset limits."""
    from backend.app.modules.agents.models import CostLedger
    rows = list(session.scalars(select(AgentPiProviderReceipt).where(
        AgentPiProviderReceipt.organization_id == claim.organization_id,
        AgentPiProviderReceipt.store_id == claim.store_id,
        AgentPiProviderReceipt.run_id == claim.run_id)))
    steps = [s for s in list_steps(session, claim.organization_id, claim.store_id, claim.run_id)
             if s.kind == 'model']
    ledgers = list(session.scalars(select(CostLedger).where(
        CostLedger.organization_id == claim.organization_id,
        CostLedger.store_id == claim.store_id, CostLedger.run_id == claim.run_id)))
    if (len(steps) != len(rows) or len(ledgers) != len(rows)
            or {s.id for s in steps} != {r.model_step_id for r in rows}
            or {r.model_step_id for r in rows} != {l.step_id for l in ledgers}):
        _fail('agent_pi_campaign_facts')
    if require_completed and any(step.state != 'succeeded' for step in steps):
        _fail('agent_pi_campaign_unfinished')
    by_step = {l.step_id: l for l in ledgers}
    return [(r, by_step[r.model_step_id]) for r in rows]


def bind_pi_campaign(session: Session, claim: AgentClaim, snapshot):
    execution = _pi_receipt_execution(session, claim)
    frozen = dict(execution.frozen_context)
    if 'pi_online_campaign' in frozen or pi_campaign_facts(session, claim):
        _fail('agent_pi_campaign_immutable')
    frozen['pi_online_campaign'] = snapshot
    _cas_execution(session, execution, values={'frozen_context': frozen}, require_live=True)


def permit_pi_provider(session: Session, claim: AgentClaim, *, step, pending,
                       input_digest, request_digest, runtime_record, run, now):
    """Stage in the model Step/reservation root. Raw permit never enters DB."""
    record = validate_pi_runtime_record(runtime_record)
    if (record['contract_version'] not in (2, 3) or run.model != 'deepseek-v4.1-flash'
            or step.kind != 'model' or step.state != 'running'):
        _fail('agent_pi_provider_binding')
    receipt = AgentPiProviderReceipt(id=str(uuid4()), organization_id=claim.organization_id,
        store_id=claim.store_id, run_id=claim.run_id, model_step_id=step.id, model_kind='model',
        epoch=claim.epoch, request_id=pending['request_id'], permit_digest=sha256(pending['permit'].encode()).hexdigest(),
        input_digest=input_digest, request_digest=request_digest, toolset_version=run.toolset_version,
        model=run.model, wire_protocol=record['wire_protocol'], manifest_digest=record['source_manifest_sha256'],
        state='permitted', created_at=now, updated_at=now)
    session.add(receipt)
    session.flush()
    return receipt


def consume_pi_provider_permit(session: Session, claim: AgentClaim, *, step_id,
                               permit, input_digest, request_digest, now):
    """CAS is the single-send linearization point, not an in-memory flag.

    Caller must commit normally before entering transport. A lost commit result
    grants no send and no retry, even if a subsequent query finds this intent.
    """
    result = session.execute(update(AgentPiProviderReceipt).where(
        AgentPiProviderReceipt.organization_id == claim.organization_id,
        AgentPiProviderReceipt.store_id == claim.store_id,
        AgentPiProviderReceipt.run_id == claim.run_id,
        AgentPiProviderReceipt.model_step_id == step_id,
        AgentPiProviderReceipt.epoch == claim.epoch,
        AgentPiProviderReceipt.permit_digest == sha256(permit.encode()).hexdigest(),
        AgentPiProviderReceipt.input_digest == input_digest,
        AgentPiProviderReceipt.request_digest == request_digest,
        AgentPiProviderReceipt.state == 'permitted',
    ).values(state='send_intent', updated_at=now).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        _fail('agent_pi_provider_permit_consumed')
    session.flush()
    return pi_provider_receipt(session, claim, step_id)



def prepare_pi_receipt_close(session: Session, claim: AgentClaim, execution,
                             steps: list[AgentStep]) -> list[AgentPiToolReceipt] | None:
    """Validate formal receipt facts before the existing exact-claim close writes.

    None is the old experimental SDK path. A malformed formal marker never
    falls back to that path. No claim, transaction, dispatch or repair here.
    """
    context = execution.frozen_context
    if type(context) is not dict or "pi_runtime_record" not in context:
        return None
    validate_pi_runtime_record(context["pi_runtime_record"])
    binding = context.get("pi_readonly_binding")
    if type(binding) is not dict or binding.get("profile") != pi_record_tool_profile(context["pi_runtime_record"]):
        _fail("agent_pi_profile_invalid")
    run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
    if (run is None or context.get("toolset_version") != run.toolset_version
            or "sha256:" + str(_canonical_digest(binding)) != run.toolset_version
            or (binding.get("run_id"), binding.get("organization_id"), binding.get("store_id"),
                binding.get("requester_id"))
            != (run.id, run.organization_id, run.store_id, run.requester_id)):
        _fail("agent_pi_profile_invalid")
    rows = pi_tool_receipts(session, claim.organization_id, claim.store_id, claim.run_id)
    by_id = {step.id: step for step in steps}
    tool_ids = set()
    names = {(e["name"], e["tool_key"], e["version"]) for e in binding["tools"]}
    for row in rows:
        model = by_id.get(row.model_step_id)
        if (row.contract_version != 1 or row.model_kind != "model" or row.tool_kind != "tool"
                or model is None or model.kind != "model" or model.state != "succeeded"
                or row.toolset_version != run.toolset_version
                or (row.pi_name, row.tool_key, row.tool_version) not in names):
            _fail("agent_pi_receipt_inconsistent")
        if row.state == "accepted":
            if row.tool_step_id is not None or row.output_ref is not None or row.error_code is not None:
                _fail("agent_pi_receipt_inconsistent")
            continue
        tool = by_id.get(row.tool_step_id)
        call = get_tool_call(session, claim.organization_id, claim.store_id,
                             claim.run_id, row.tool_step_id)
        if (tool is None or tool.kind != "tool" or tool.sequence_no <= model.sequence_no
                or tool.id in tool_ids or call is None
                or call.idempotency_key != "pi:" + str(_canonical_digest([claim.run_id, row.native_call_id]))
                or (call.tool_key, call.tool_version, call.input_digest)
                != (row.tool_key, row.tool_version, row.input_digest)):
            _fail("agent_pi_receipt_inconsistent")
        tool_ids.add(tool.id)
        if row.state == "succeeded":
            if tool.state != "succeeded" or row.output_ref != tool.output_ref or row.error_code is not None:
                _fail("agent_pi_receipt_inconsistent")
        elif row.state == "dispatch_intent":
            if tool.state != "running" or row.output_ref is not None or row.error_code is not None:
                _fail("agent_pi_receipt_inconsistent")
        else:
            _fail("agent_pi_receipt_inconsistent")
    if tool_ids != {step.id for step in steps if step.kind == "tool"}:
        _fail("agent_pi_receipt_inconsistent")
    return rows


def close_pi_receipts(session: Session, claim: AgentClaim, rows: list[AgentPiToolReceipt],
                      *, code: str, now: datetime) -> None:
    """Called ONLY inside the existing verified close root, before its commit.

    Accepted has no tool intent and becomes failed. Committed dispatch intent
    with no authoritative result stays unknown. Succeeded facts never change.
    """
    for row in rows:
        if row.state == "succeeded":
            continue
        if row.state not in ("accepted", "dispatch_intent"):
            _fail("agent_pi_receipt_inconsistent")
        old_state = row.state
        result = session.execute(update(AgentPiToolReceipt).where(
            AgentPiToolReceipt.id == row.id,
            AgentPiToolReceipt.organization_id == claim.organization_id,
            AgentPiToolReceipt.store_id == claim.store_id,
            AgentPiToolReceipt.run_id == claim.run_id,
            AgentPiToolReceipt.state == old_state,
        ).values(state="failed" if old_state == "accepted" else "unknown",
                 error_code=code, updated_at=_timestamp(now))
         .execution_options(synchronize_session=False))
        if result.rowcount != 1:
            _fail("agent_conflict")
    session.flush()


def require_pi_content_binding(session: Session, claim: AgentClaim, *, pending, message, runtime_record):
    """Read durable Host observations, never trust a child-provided digest."""
    from hashlib import sha256
    from backend.app.modules.agents.models import CostLedger
    from backend.app.modules.agents.pi_online_response import canonical_message_bytes
    row = pi_provider_receipt(session, claim, pending['step_id'])
    presence_keys = {'prompt_tokens', 'completion_tokens', 'cached_tokens', 'cache_write_tokens'}
    basis_valid = False
    if row is not None and row.usage_basis is not None:
        from backend.app.modules.agents.pi_online_response import validate_go_usage_basis
        try:
            basis = validate_go_usage_basis(row.usage_basis)
            basis_valid = (basis['response_digest'] == row.response_digest
                and basis['model'] == row.model
                and basis['prompt_tokens'] == row.usage_input + row.cache_read
                and basis['completion_tokens'] == row.usage_output
                and basis['cache_read'] == row.cache_read and row.cache_write == 0
                and row.usage_presence in (
                    {'prompt_tokens':True,'completion_tokens':True,'cached_tokens':True,'cache_write_tokens':False},
                    {'prompt_tokens':True,'completion_tokens':True,'cached_tokens':True,'cache_write_tokens':True}))
        except Exception:
            basis_valid = False
        if not basis_valid:
            from backend.app.modules.agents.runtime_bridge import reject
            reject('pi_online_content_binding')
    if (row is None or row.state != 'responded' or row.send_started is not True
            or row.permit_digest != sha256(pending['permit'].encode()).hexdigest()
            or row.request_id != pending['request_id'] or not row.response_digest
            or row.response_digest != pending.get('response_digest')
            or row.input_digest != pending['input_digest'] or row.request_digest != pending['request_digest']
            or row.toolset_version != pending['toolset_version']
            or row.wire_protocol != runtime_record['wire_protocol']
            or row.manifest_digest != runtime_record['source_manifest_sha256']
            or row.model != 'deepseek-v4.1-flash'
            or type(row.usage_presence) is not dict or set(row.usage_presence) != presence_keys
            or any(type(value) is not bool for value in row.usage_presence.values())
            or (not basis_valid and any(value is not True for value in row.usage_presence.values()))
            or row.message_digest != sha256(canonical_message_bytes(message)).hexdigest()
            or row.usage != message['usage']):
        from backend.app.modules.agents.runtime_bridge import reject
        reject('pi_online_content_binding')
    ledger = session.scalar(select(CostLedger).where(
        CostLedger.organization_id == claim.organization_id, CostLedger.store_id == claim.store_id,
        CostLedger.run_id == claim.run_id, CostLedger.step_id == pending['step_id']))
    if (ledger is None or ledger.status != 'settled'
            or ledger.usage_snapshot != {'contract': 'disjoint-v1', 'quantities': [
                ['input', row.usage_input + row.cache_read + row.cache_write], ['output', row.usage_output]]}
            or ledger.model != row.model):
        from backend.app.modules.agents.runtime_bridge import reject
        reject('pi_usage_unsettled')
    return ledger


def pi_provider_receipt_facts(session: Session, organization_id: str, store_id: str, run_id: str):
    """Read scoped durable facts without manufacturing a worker claim.

    Return only projection inputs; amounts, snapshots and raw identifiers remain
    inside persistence. Ambiguous or broken Step/ledger relations fail closed.
    """
    from backend.app.modules.agents.models import CostLedger
    statement = select(AgentPiProviderReceipt, CostLedger).select_from(AgentPiProviderReceipt).outerjoin(
        CostLedger, and_(
            CostLedger.organization_id == AgentPiProviderReceipt.organization_id,
            CostLedger.store_id == AgentPiProviderReceipt.store_id,
            CostLedger.run_id == AgentPiProviderReceipt.run_id,
            CostLedger.step_id == AgentPiProviderReceipt.model_step_id,
        ),
    ).where(
        AgentPiProviderReceipt.organization_id == organization_id,
        AgentPiProviderReceipt.store_id == store_id, AgentPiProviderReceipt.run_id == run_id,
    ).order_by(AgentPiProviderReceipt.created_at, AgentPiProviderReceipt.id)
    rows = {}
    # Do not filter or deduplicate matches by call key or authority: every
    # physical ledger on this step counts, and the LEFT JOIN preserves zero.
    for row, ledger in session.execute(statement):
        _, ledgers = rows.setdefault(row.id, (row, []))
        if ledger is not None:
            ledgers.append(ledger)
    steps = list_steps(session, organization_id, store_id, run_id)
    by_id = {step.id: step for step in steps}
    run = get_run(session, organization_id, store_id, run_id)
    execution = execution_for(session, organization_id, store_id, run_id)
    if run is None or execution is None or type(execution.frozen_context) is not dict:
        _fail('agent_runtime_receipt_inconsistent')
    record = validate_pi_runtime_record(execution.frozen_context.get('pi_runtime_record'))
    if record['contract_version'] not in (2, 3) or run.provider_key != 'opencode-go':
        _fail('agent_runtime_receipt_inconsistent')
    result, seen = [], set()
    for row, ledgers in rows.values():
        step = by_id.get(row.model_step_id)
        if (step is None or step.kind != 'model' or row.model_kind != 'model'
                or row.model_step_id in seen or len(ledgers) != 1
                or row.wire_protocol != record['wire_protocol']
                or row.model != run.model or row.toolset_version != run.toolset_version
                or row.manifest_digest != record['source_manifest_sha256']
                or ledgers[0].requester_id != run.requester_id
                or ledgers[0].model != row.model or ledgers[0].provider_key != run.provider_key
                or ledgers[0].call_key != f'{run_id}:{step.sequence_no}'):
            _fail('agent_runtime_receipt_inconsistent')
        ledger = ledgers[0]
        if row.state == 'responded':
            dimensions = (row.usage_input, row.usage_output, row.cache_read, row.cache_write)
            if (ledger.status not in ('settled', 'reconciliation_required')
                    or any(type(value) is not int or not 0 <= value <= 9007199254740991 for value in dimensions)
                    or row.cache_write != 0
                    or ledger.usage_snapshot != {'contract': 'disjoint-v1', 'quantities': [
                        ['input', row.usage_input + row.cache_read + row.cache_write], ['output', row.usage_output]]}):
                _fail('agent_runtime_receipt_inconsistent')
        elif row.state in ('unknown', 'not_sent'):
            if ledger.status != 'unknown':
                _fail('agent_runtime_receipt_inconsistent')
        elif row.state in ('permitted', 'send_intent'):
            # A failed result commit leaves send_intent while A-close marks
            # the reservation unknown. Preserve that durable H1 ambiguity.
            closed = step.state in ('unknown', 'failed', 'cancelled') or run.state in _TERMINAL_RUN_STATES
            if not (ledger.status == 'reserved' or ledger.status == 'unknown' and closed):
                _fail('agent_runtime_receipt_inconsistent')
        else:
            _fail('agent_runtime_receipt_inconsistent')
        seen.add(row.model_step_id)
        result.append({'receipt_id': row.id, 'model_step_id': row.model_step_id,
            'attempt_id': row.request_id, 'provider_request_id_sha256':
                sha256(row.provider_request_id.encode()).hexdigest() if row.provider_request_id is not None else None,
            'response_digest': row.response_digest, 'message_digest': row.message_digest,
            'usage_presence': row.usage_presence, 'ledger_id': ledgers[0].id,
            'state': row.state, 'created_at': row.created_at, 'updated_at': row.updated_at})
    return result


def record_pi_argument_rejection(session: Session, claim: AgentClaim, *, step_id, message,
                                 toolset_version, invalid_arguments):
    """Append a value-free rejection in the model completion transaction."""
    from hashlib import sha256
    from .pi_online_response import canonical_message_bytes
    execution = _pi_receipt_execution(session, claim)
    row = pi_provider_receipt(session, claim, step_id)
    message_digest = sha256(canonical_message_bytes(message)).hexdigest()
    if (row is None or row.state != 'responded' or row.message_digest != message_digest
            or row.toolset_version != toolset_version or not invalid_arguments):
        _fail('pi_online_content_binding')
    calls = [{'call_id':item['id'], 'name':item['name'],
              'code':'tool_arguments_schema_invalid'} for item in message['content'] if item['type'] == 'toolCall']
    feedback = {'step_id':step_id, 'message_digest':message_digest,
                'toolset_version':toolset_version, 'calls':calls}
    frozen = dict(execution.frozen_context)
    records = dict(frozen.get('pi_argument_rejections_v1', {}))
    if step_id in records:
        _fail('pi_online_content_binding')
    prior_ids = {call['call_id'] for record in records.values() for call in record['calls']}
    if any(call['call_id'] in prior_ids for call in calls):
        _fail('pi_online_content_binding')
    records[step_id] = feedback
    frozen['pi_argument_rejections_v1'] = records
    _cas_execution(session, execution, values={'frozen_context':frozen}, require_live=True)
    return feedback


def pi_rejected_native_ids(session: Session, claim: AgentClaim) -> set[str]:
    """Scoped to the existing live claim; rejection never makes an ID reusable."""
    execution = _pi_receipt_execution(session, claim)
    records = execution.frozen_context.get('pi_argument_rejections_v1', {})
    return {call['call_id'] for record in records.values() for call in record['calls']}



def _canonical_tool_binding(session, claim, *, receipt_id, step_id, pi_name, decision, runtime_record, check_at=None):
    """One literal result root + live claim, not a historical recovery grant."""
    from . import canonical_result
    if check_at is None:
        control = _pi_receipt_execution(session, claim)
    else:
        # Commit-guard checks accept structured SELECTs only. Use its freshly
        # supplied DB instant, never a textual clock/locking/writing guard.
        at = _timestamp(check_at)
        control = execution_for(session, claim.organization_id, claim.store_id, claim.run_id)
        if (control is None or runtime_identity(control.frozen_context)!=SDK_RUNTIME
            or claim.runtime_identity!=SDK_RUNTIME or control.mode!='worker_v2' or control.phase!='claimed'
            or control.owner_machine_id!=claim.machine_id or control.epoch!=claim.epoch
            or control.lease_until is None or control.lease_until<=at
            or control.authorization_expires_at<=at or control.cancel_requested_at is not None):
            _fail('agent_execution_fenced')
    root = session.get_transaction()
    if session.in_nested_transaction() or root is None or root.parent is not None:
        _fail('agent_pi_receipt_root_required')
    record = validate_pi_runtime_record(runtime_record)
    if (record['launch_profile'] not in canonical_result.RESULT_PROFILES
            or control.frozen_context['pi_runtime_record'] != record):
        _fail('agent_pi_result_binding')
    run = _require_run(session, claim.organization_id, claim.store_id, claim.run_id)
    if check_at is not None and (control.dispatch_state != 'settled'
            or run.state != 'running' or _timestamp(run.deadline_at) <= at):
        _fail('agent_pi_result_binding')
    native = session.scalar(_pi_receipt_query(claim,decision['native_call_id']))
    if (native is None or native.contract_version!=1 or native.state!='succeeded'
            or native.model_kind!='model' or native.tool_kind!='tool' or native.tool_step_id!=step_id
            or (native.pi_name,native.tool_key,native.tool_version,native.toolset_version)
            !=(pi_name,decision['tool_key'],decision['version'],decision['toolset_version'])
            or native.input_digest!=_canonical_digest(decision['arguments'])):
        _fail('agent_pi_result_binding')
    if native.id != receipt_id: _fail('agent_pi_result_receipt_mismatch')
    tool = _load_run_step(session, run, step_id)
    model = _load_run_step(session, run, native.model_step_id)
    call = session.scalar(select(AgentToolCall).where(
        AgentToolCall.organization_id==claim.organization_id,AgentToolCall.store_id==claim.store_id,
        AgentToolCall.run_id==claim.run_id,AgentToolCall.step_id==step_id).execution_options(populate_existing=True))
    expected_input = _canonical_digest(decision['arguments'])
    call_key = 'pi:' + _canonical_digest([run.id,native.native_call_id])
    tool_input = 'sha256:' + _canonical_digest({'toolset_version':native.toolset_version,'call_key':call_key,
        'tool_key':native.tool_key,'version':native.tool_version,'input_digest':native.input_digest})
    if (tool.kind!='tool' or tool.state!='succeeded' or model.kind!='model' or model.state!='succeeded'
            or model.sequence_no>=tool.sequence_no or tool.output_ref!=native.output_ref
            or native.toolset_version!=run.toolset_version or decision['toolset_version']!=run.toolset_version
            or native.input_digest!=expected_input or tool.input_ref!=tool_input or call is None
            or (call.step_kind,call.tool_key,call.tool_version,call.input_digest,call.idempotency_key)
            != ('tool',native.tool_key,native.tool_version,native.input_digest,call_key)):
        _fail('agent_pi_result_binding')
    if session.info.get('_pi_tool_success_roots',{}).get(native.id) is not root:
        _fail('agent_pi_result_same_root_required')
    return control,run,native,tool,model


def record_pi_tool_result(session, claim, *, receipt_id, step_id, pi_name, decision,
                          runtime_record, payload, source_refs, result_kind, now):
    from . import canonical_result
    from .models import AgentPiToolResult
    control,run,native,tool,model=_canonical_tool_binding(session,claim,receipt_id=receipt_id,
        step_id=step_id,pi_name=pi_name,decision=decision,runtime_record=runtime_record)
    if control.dispatch_state!='dispatched' or native.output_ref!='sha256:'+_canonical_digest(payload):
        _fail('agent_pi_result_binding')
    envelope=canonical_result.make_envelope(runtime_record=runtime_record,
        scope={'organization_id':run.organization_id,'store_id':run.store_id,'requester_id':run.requester_id,
               'agent_id':control.frozen_context['pi_readonly_binding']['agent_id']},
        binding={'run_id':run.id,'model_step_id':model.id,'tool_step_id':tool.id,'pi_receipt_id':native.id,
            'native_call_id':native.native_call_id,'tool_key':native.tool_key,'tool_name':native.pi_name,
            'tool_version':native.tool_version,'toolset_version':native.toolset_version,
            'input_digest':native.input_digest,'execution_epoch':claim.epoch},
        result_kind=result_kind,payload=payload,source_refs=source_refs,captured_at=_timestamp(now).isoformat())
    value=AgentPiToolResult(id=str(uuid4()),**canonical_result.columns_for_envelope(envelope))
    session.add(value);session.flush()
    canonical_result.validate_columns(value)
    return value


def require_pi_tool_result(session, claim, *, receipt_id, step_id, pi_name, decision,
                           runtime_record, expected_ref, check_at=None):
    from . import canonical_result
    from .models import AgentPiToolResult
    control,run,native,tool,model=_canonical_tool_binding(session,claim,receipt_id=receipt_id,
        step_id=step_id,pi_name=pi_name,decision=decision,runtime_record=runtime_record,check_at=check_at)
    value=session.scalar(select(AgentPiToolResult).where(
        AgentPiToolResult.organization_id==run.organization_id,AgentPiToolResult.store_id==run.store_id,
        AgentPiToolResult.run_id==run.id,AgentPiToolResult.pi_receipt_id==native.id).execution_options(populate_existing=True))
    if value is None or value.canonical_ref!=expected_ref:_fail('agent_pi_result_binding')
    envelope=canonical_result.validate_columns(value)
    binding=envelope['binding']
    if (envelope['runtime_record']!=runtime_record or envelope['scope']!={'organization_id':run.organization_id,
        'store_id':run.store_id,'requester_id':run.requester_id,'agent_id':control.frozen_context['pi_readonly_binding']['agent_id']}
        or binding!={'run_id':run.id,'model_step_id':model.id,'tool_step_id':tool.id,'pi_receipt_id':native.id,
            'native_call_id':native.native_call_id,'tool_key':native.tool_key,'tool_name':native.pi_name,
            'tool_version':native.tool_version,'toolset_version':native.toolset_version,
            'input_digest':native.input_digest,'execution_epoch':claim.epoch}
        or envelope['output_ref']!=native.output_ref):_fail('agent_pi_result_binding')
    return value
