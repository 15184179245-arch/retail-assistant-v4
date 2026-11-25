"""Authenticated, bounded Agent loop over the fixed AGT-003 tool catalog.

The model returns untrusted JSON text. Personal messages and final-answer
references are durable; raw provider requests and tool results stay transient.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import os
import re
import threading
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased
from backend.app.application import commit_guard

from backend.app.core.model_provider import CallBudget, ProviderRequest, ProviderResponse
from backend.app.core.model_context import (
    MAX_MESSAGE_CHARS, ModelContextError, fit_prompt, input_capacity, run_input_budget,
)
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import conversation_service, answer_receipts, cost_ledger, skill_service, repository as runtime_repository

_NO_RUNTIME_BINDING = object()
from backend.app.modules.agents.models import AgentConfirmation, AgentExecution, AgentRun, AgentStep
from backend.app.modules.agents.provider_adapter import ChatCompletionsProvider, ProviderConfig, ProviderError
from backend.app.modules.agents.repository import (
    AgentPersistenceError, append_step, create_confirmation, create_run,
    record_answer, get_confirmation, get_run, get_tool_call, list_steps, renew_human_run_deadline,
    set_confirmation_state, transition_run, transition_step,
    current_authorization_epoch, enqueue_worker_execution, execution_for,
    freeze_approval_grant, reject_worker_confirmation, request_worker_cancel,
)
from backend.app.modules.agents.schemas import (
    RunCreate, RunState, StepCreate, StepKind, StepState, StepTransition, ToolCallCreate,
)
from backend.app.modules.agents.tool_registry import (
    ToolRegistryError, invoke_tool, legacy_code_tool, model_tool_descriptions, resolve_tool,
    toolset_digest, verify_toolset_digest, PI_READONLY_PROFILE, PI_PROFILES, PI_ALL_PROFILES,
    freeze_pi_binding, pi_binding_digest, PI_WORKBENCH_PROFILE, GUANJIA_CATEGORY_TOOLS,
)
from backend.app.modules.audit.service import record_event
from backend.app.modules.audit.repository import get_by_idempotency_key
from backend.app.modules.identity.models import Subject, Store
from backend.app.modules.identity.service import authorize_scope


@dataclass(frozen=True)
class _InlineTaskClaim:
    organization_id: str
    store_id: str
    run_id: str
    requester_id: str
    agent_id: str
    run_version: int
    step_id: str
    step_version: int
    sequence_no: int
    confirmation_version: int
    input_digest: str
    call_id: str
    call_key: str


def _capture_inline_task_claim(session, actor, agent, run, step, confirmation, definition):
    from backend.app.modules.agents import tool_registry as tools
    if not ((run.organization_id, run.store_id, run.requester_id) ==
                (actor.organization_id, actor.store_id, actor.subject_id)
            and (agent.organization_id, agent.store_id) == (actor.organization_id, actor.store_id)
            and agent.account_id is None and agent.session_id is None
            and definition is tools.TOOLS.get(("tasks.suggest_review", "1"))
            and (definition.key, definition.version, definition.handler_id) ==
                ("tasks.suggest_review", "1", "tasks.suggest_review.v1")
            and definition.input_model is tools.SuggestReviewInput
            and definition.output_model is tools.SuggestReviewOutput
            and definition.input_sha256 == tools._schema_sha256(tools.SuggestReviewInput)
            and definition.output_sha256 == tools._schema_sha256(tools.SuggestReviewOutput)
            and definition.requester_permission == "tasks.suggest"
            and definition.agent_permission == "agents.tasks.suggest_review"
            and definition.mode == "write" and definition.idempotency == "required"
            and definition.confirmation == "reviewer_decides_later"
            and tools._HANDLERS.get(definition.handler_id) is tools._invoke_suggest_review):
        return None
    call = get_tool_call(session, run.organization_id, run.store_id, run.id, step.id)
    if call is None:
        return None
    return _InlineTaskClaim(run.organization_id, run.store_id, run.id, actor.subject_id, agent.subject_id,
        run.version, step.id, step.version, step.sequence_no, confirmation.version,
        confirmation.input_digest, call.id, f"{run.id}:{step.sequence_no}")


def _known_inline_rejection(error):
    if isinstance(error, AgentRuntimeError):
        return error.code if isinstance(error.code, str) and error.code in {
            "agent_confirmation_expired", "agent_confirmation_conflict"} else None
    if isinstance(error, StarletteHTTPException):
        if not isinstance(error.detail, str):
            return None
        if (error.status_code in (401, 403) or error.status_code == 400 and error.detail in
                {"task_reviewer_invalid", "task_assignee_invalid", "task_due_date_invalid"}):
            return error.detail if _IDENTIFIER.fullmatch(error.detail) else None
    if isinstance(error, ToolRegistryError) and isinstance(error.code, str) and error.code in {
            "tool_not_registered", "tool_registry_inconsistent", "tool_login_invalid", "tool_agent_identity_invalid",
            "tool_scope_denied", "tool_agent_permission_denied", "tool_requester_permission_denied",
            "agent_worker_toolset_changed"}:
        return error.code
    if isinstance(error, commit_guard.CommitGuardError):
        original = error.__cause__ if str(error) == "command_transaction_aborted" else error
        if (isinstance(original, ToolRegistryError)
                and original.code == "agent_worker_toolset_changed"):
            return original.code
        if isinstance(original, commit_guard.CommitGuardError) and str(original) in {
                "command_check_write_forbidden", "command_check_mutated_session", "command_check_result_invalid",
                "command_flush_unsettled"}:
            return "agent_final_check_rejected"
    return None


def _close_inline_final_rejection(session, actor, claim, code):
    """One new control-only transaction; absence is checked on the raw unique key."""
    if (not isinstance(claim, _InlineTaskClaim) or session.in_transaction()
            or (actor.organization_id, actor.store_id, actor.subject_id) !=
                (claim.organization_id, claim.store_id, claim.requester_id)
            or not isinstance(code, str) or not _IDENTIFIER.fullmatch(code)):
        return False
    from backend.app.modules.identity.command_guard import lock_scope_security_fence
    from backend.app.modules.agents.tool_registry import scoped_task_tool_key
    from backend.app.modules.tasks import service as task_service
    from backend.app.modules.tasks.models import Task, TaskIdempotency
    with session.begin():
        lock_scope_security_fence(session, claim.organization_id, claim.store_id)
        # Ordinary Task writers take this Store lock before their domain rows.
        # Take it before Run too, so absence/receipt facts remain stable through
        # the control commit. NO KEY UPDATE permits audit FK KEY SHARE locks.
        # Inactive stores still need rejection cleanup.
        store = session.scalar(select(Store.id).where(Store.organization_id == claim.organization_id,
            Store.id == claim.store_id).with_for_update(key_share=True))
        if store is None:
            return False
        run = session.scalar(select(AgentRun).where(AgentRun.organization_id == claim.organization_id,
            AgentRun.store_id == claim.store_id, AgentRun.id == claim.run_id)
            .with_for_update().execution_options(populate_existing=True))
        if run is None or run.requester_id != claim.requester_id:
            return False
        if run.state in ("completed", "failed", "cancelled"):
            return True
        if execution_for(session, claim.organization_id, claim.store_id, claim.run_id) is not None:
            return False
        step = session.scalar(select(AgentStep).where(AgentStep.organization_id == claim.organization_id,
            AgentStep.store_id == claim.store_id, AgentStep.run_id == claim.run_id, AgentStep.id == claim.step_id)
            .with_for_update().execution_options(populate_existing=True))
        confirmation = session.scalar(select(AgentConfirmation).where(
            AgentConfirmation.organization_id == claim.organization_id, AgentConfirmation.store_id == claim.store_id,
            AgentConfirmation.run_id == claim.run_id).with_for_update().execution_options(populate_existing=True))
        call = get_tool_call(session, claim.organization_id, claim.store_id, claim.run_id, claim.step_id)
        if (run.state != "running" or run.version != claim.run_version or step is None or confirmation is None
                or (step.state, step.version, step.kind, step.sequence_no) !=
                    ("running", claim.step_version, "tool", claim.sequence_no)
                or (confirmation.state, confirmation.version, confirmation.step_id, confirmation.requester_id) !=
                    ("executing", claim.confirmation_version, claim.step_id, claim.requester_id)
                or (confirmation.tool_key, confirmation.tool_version, confirmation.input_digest) !=
                    ("tasks.suggest_review", "1", claim.input_digest)
                or _digest(_canonical(confirmation.arguments)) != claim.input_digest or call is None
                or (call.id, call.tool_key, call.tool_version, call.input_digest, call.idempotency_key) !=
                    (claim.call_id, "tasks.suggest_review", "1", claim.input_digest, claim.call_key)
                or session.scalar(select(Subject.id).where(Subject.organization_id == claim.organization_id,
                    Subject.id == claim.agent_id, Subject.subject_type == "agent")) is None):
            return False
        key = scoped_task_tool_key(claim.requester_id, claim.agent_id, "tasks.suggest_review", "1", claim.call_key)
        # Match the table's real unique constraint FIRST. Wrong actor/operation
        # is conflicting evidence, not proof that no receipt exists.
        receipt = session.scalar(select(TaskIdempotency).where(TaskIdempotency.organization_id == claim.organization_id,
            TaskIdempotency.store_id == claim.store_id, TaskIdempotency.idempotency_key == key))
        task_id = None
        if receipt is not None:
            args = confirmation.arguments
            expected = task_service._digest("create", dict(actor_id=claim.agent_id, source="agent",
                title=args.get("title"), description=args.get("description"), reason=args.get("reason"),
                proposal_ref=args.get("proposal_ref"), reviewer_id=args.get("reviewer_id")))
            if (receipt.actor_id != claim.agent_id or receipt.operation != "create" or receipt.payload_digest != expected):
                return False
            task = session.scalar(select(Task).where(Task.organization_id == claim.organization_id,
                Task.store_id == claim.store_id, Task.id == receipt.task_id))
            if (task is None or task.created_by != claim.agent_id or task.source != "agent"
                    or any(getattr(task, field) != args.get(field)
                           for field in ("title", "description", "proposal_ref", "reviewer_id"))):
                return False
            task_id = task.id
        change = StepTransition(to_state=StepState.FAILED, error_code=code)
        if task_id is not None:
            change = StepTransition(to_state=StepState.SUCCEEDED, output_ref="sha256:" + _digest(_canonical(
                dict(task_id=task_id, state="suggested", reviewer_id=confirmation.arguments.get("reviewer_id")))))
        transition_step(session, organization_id=claim.organization_id, store_id=claim.store_id, run_id=claim.run_id,
            step_id=claim.step_id, expected_run_version=run.version, expected_step_version=step.version, change=change)
        run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
        set_confirmation_state(session, confirmation, "approved" if task_id else "failed", result_task_id=task_id,
                               reason=confirmation.decision_reason)
        _transition(session, run, RunState.COMPLETED if task_id else RunState.FAILED)
        _audit(session, actor, claim.run_id, "agent_confirmation.final_receipt_reconciled" if task_id
               else "agent_confirmation.final_rejected", code)
    return True


# Cooperative cancellation is process-local and only a hint to the running
# loop; durable state transitions are still made with CAS in the service.
_CANCEL_EVENTS: dict[str, threading.Event] = {}


class AgentRuntimeError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


MAX_MODEL_CALLS = int(os.environ.get("AGENT_MAX_MODEL_CALLS", "3"))
MAX_TOOL_CALLS = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "2"))
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "5"))
# Optional aggregate spend/resource ceiling; otherwise use the model window
# per allowed call. Existing persisted Runs retain their original budgets.
MAX_INPUT_TOKENS = (int(os.environ["AGENT_MAX_INPUT_TOKENS"])
                    if os.environ.get("AGENT_MAX_INPUT_TOKENS") else None)
MAX_OUTPUT_TOKENS = int(os.environ.get("AGENT_MAX_OUTPUT_TOKENS", "16384"))
MAX_SECONDS = int(os.environ.get("AGENT_MAX_SECONDS", "180"))
MAX_ANSWER_LENGTH = int(os.environ.get("AGENT_MAX_ANSWER_LENGTH", "10000"))
CONFIRMATION_SECONDS = 15 * 60
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z", re.ASCII)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _provider_factory() -> ChatCompletionsProvider:
    from urllib.parse import urlsplit
    from backend.app.modules.agents.provider_adapter import DeepSeekJsonProvider
    config = ProviderConfig.from_env()
    parts = urlsplit(config.base_url)
    if (config.model == "deepseek-flash"
            and parts.scheme == "https" and parts.hostname == "api.deepseek.com"
            and parts.port in (None, 443)):
        return DeepSeekJsonProvider(config)
    return ChatCompletionsProvider(config)


def _scope(session: Session, actor: AuthenticatedSubject) -> None:
    if actor.account_id is None or actor.session_id is None:
        raise AgentRuntimeError("agent_login_required", 401)
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="agents.run")
    kind = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id))
    if kind != "person":
        raise AgentRuntimeError("agent_person_required", 403)


def _human_guard(session: Session, actor: AuthenticatedSubject) -> None:
    """Real human command order: fence, account/session locks, then re-verify.

    The pre-reads only locate the scope. After the guard the caller MUST re-read
    every Run/Confirmation/Step under the execution lock and keep this same
    session/transaction through the commit.
    """
    from backend.app.modules.auth.service import validate_actor_session
    from backend.app.modules.identity.command_guard import lock_employee_authority
    try:
        lock_employee_authority(session, actor)
    except HTTPException as error:
        raise AgentRuntimeError("agent_human_session_invalid", 403) from error
    validate_actor_session(session, actor)
    _scope(session, actor)


def _agent_actor(session: Session, actor: AuthenticatedSubject,
                 deadline: datetime) -> AuthenticatedSubject:
    agent_id = os.environ.get("AGENT_SUBJECT_ID", "")
    if not _IDENTIFIER.fullmatch(agent_id) or len(agent_id) > 36:
        raise AgentRuntimeError("agent_not_configured", 503)
    subject = session.scalar(select(Subject).where(
        Subject.organization_id == actor.organization_id, Subject.id == agent_id))
    if subject is None or subject.subject_type != "agent" or not subject.active:
        raise AgentRuntimeError("agent_not_configured", 503)
    agent = AuthenticatedSubject(agent_id, actor.organization_id, actor.store_id, deadline)
    authorize_scope(session, agent, organization_id=actor.organization_id,
                    store_id=actor.store_id)
    return agent


def _audit(session: Session, actor: AuthenticatedSubject, run_id: str,
           action: str, code: str) -> None:
    record_event(session, actor_id=actor.subject_id,
        organization_id=actor.organization_id, store_id=actor.store_id,
        action=action, target_type="agent_run", target_id=run_id,
        idempotency_key=f"agent_run:{run_id}:{action}", details={"code": code})


def _run_id(actor: AuthenticatedSubject, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, _canonical([
        "retail-agent-run-v1", actor.organization_id, actor.store_id,
        actor.subject_id, key,
    ])))


def _load_owned(session: Session, actor: AuthenticatedSubject, run_id: str) -> AgentRun:
    from backend.app.modules.assistant.principal import WorkPrincipal
    if type(actor) is WorkPrincipal and actor.run_id != run_id:
        raise AgentRuntimeError('agent_not_found', 404)
    _scope(session, actor)
    run = get_run(session, actor.organization_id, actor.store_id, run_id)
    if run is None or run.requester_id != actor.subject_id:
        raise AgentRuntimeError("agent_not_found", 404)
    return run


def read_run(session: Session, actor: AuthenticatedSubject, run_id: str) -> AgentRun:
    return _load_owned(session, actor, run_id)


def _pending_payload(confirmation: AgentConfirmation, run: AgentRun,
                     step: AgentStep) -> dict:
    return {"tool_key": confirmation.tool_key, "version": confirmation.tool_version,
            "arguments": confirmation.arguments, "input_digest": confirmation.input_digest,
            "expires_at": _utc(confirmation.expires_at).isoformat(),
            "run_version": run.version, "step_version": step.version}


def _steps_payload(session: Session, run: AgentRun) -> list[dict]:
    payload: list[dict] = []
    for step in list_steps(session, run.organization_id, run.store_id, run.id):
        item = {
            "id": step.id, "sequence_no": step.sequence_no, "kind": step.kind,
            "state": step.state, "input_ref": step.input_ref,
            "output_ref": step.output_ref, "error_code": step.error_code,
            "input_tokens": step.input_tokens, "output_tokens": step.output_tokens,
            "created_at": _utc(step.created_at).isoformat(),
            "updated_at": _utc(step.updated_at).isoformat(),
        }
        if step.kind == StepKind.TOOL.value:
            tool_call = get_tool_call(session, run.organization_id, run.store_id,
                                      run.id, step.id)
            if tool_call is not None:
                item["tool_key"] = tool_call.tool_key
                item["tool_version"] = tool_call.tool_version
        payload.append(item)
    return payload


_TERMINAL_RUN_STATES = {
    RunState.COMPLETED.value, RunState.FAILED.value, RunState.CANCELLED.value,
}
_CLOSED_CONTROL_PHASES = {"finished", "cancelled", "unknown"}
_CANCEL_RESOLUTIONS = {"requested", "cancelled", "too_late", "unknown"}
_EFFECT_STATUSES = {"none", "local_task_committed", "external_unknown"}


def _execution_inconsistent() -> AgentRuntimeError:
    return AgentRuntimeError("agent_execution_inconsistent", 409)


def _derive_phase(run_state: str, control: AgentExecution, steps: list[dict]) -> str:
    """Map durable Run/control/Step facts to the AGT-020 public phase.

    Inflight phases are derived, never stored: the control row only knows
    queued/claimed/terminal, so a RUNNING model/tool Step plus a dispatched
    attempt is what proves the current activity. No summary text is consulted.
    """
    if run_state in _TERMINAL_RUN_STATES or control.phase in _CLOSED_CONTROL_PHASES:
        return "terminal"
    if (run_state == RunState.WAITING_FOR_HUMAN.value and control.phase == "queued"
            and control.dispatch_state == "settled" and control.approval_grant is None):
        return "waiting_for_human"
    if (run_state == RunState.RUNNING.value and control.phase == "queued"
            and control.dispatch_state == "settled"):
        return "queued"
    if control.phase == "claimed":
        if control.dispatch_state == "dispatched":
            running = [item for item in steps
                       if item.get("state") == StepState.RUNNING.value]
            if running:
                kind = running[-1].get("kind")
                if kind == StepKind.MODEL.value:
                    return "model_inflight"
                if kind == StepKind.TOOL.value:
                    return "tool_inflight"
        return "claimed"
    if control.phase == "queued":
        return "queued"
    raise _execution_inconsistent()


def _execution_view(run_state: str, control: AgentExecution | None,
                    steps: list[dict]) -> dict | None:
    """The five public fields, or None for an inline run with no control row."""
    if control is None:
        return None
    requested = control.cancel_requested_at is not None
    if requested:
        resolution = control.cancel_resolution
        if resolution not in _CANCEL_RESOLUTIONS:
            raise _execution_inconsistent()
    else:
        # An internal reaper may persist an unknown outcome without a real
        # employee request; that must never surface as "the user cancelled".
        resolution = "not_requested"
    effect = control.effect_status
    if effect not in _EFFECT_STATUSES:
        raise _execution_inconsistent()
    if resolution == "cancelled" and effect != "none":
        raise _execution_inconsistent()
    return {
        "protocol_version": 1,
        "phase": _derive_phase(run_state, control, steps),
        "cancel_requested": requested,
        "cancel_resolution": resolution,
        "effect_status": effect,
    }


def _scoped_run_control(session: Session, actor: AuthenticatedSubject,
                        run_id: str) -> tuple[AgentRun, AgentExecution | None,
                                              AgentConfirmation | None, AgentStep | None]:
    """Read the Run and its control row from ONE scoped SQL statement.

    Two sequential reads could pair a created Run with a terminal control row
    (or the reverse) and publish a state that never existed. This adds no lock,
    no fence upgrade and no write: a GET stays a read.
    """
    confirmation_step = aliased(AgentStep)
    row = session.execute(
        select(AgentRun, AgentExecution, AgentConfirmation, confirmation_step)
        .outerjoin(AgentExecution, and_(
            AgentExecution.run_id == AgentRun.id,
            AgentExecution.organization_id == AgentRun.organization_id,
            AgentExecution.store_id == AgentRun.store_id,
        ))
        .outerjoin(AgentConfirmation, and_(
            AgentConfirmation.run_id == AgentRun.id,
            AgentConfirmation.organization_id == AgentRun.organization_id,
            AgentConfirmation.store_id == AgentRun.store_id,
        ))
        .outerjoin(confirmation_step, and_(
            confirmation_step.id == AgentConfirmation.step_id,
            confirmation_step.run_id == AgentRun.id,
            confirmation_step.organization_id == AgentRun.organization_id,
            confirmation_step.store_id == AgentRun.store_id,
        ))
        .where(AgentRun.organization_id == actor.organization_id,
               AgentRun.store_id == actor.store_id,
               AgentRun.id == run_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None or row[0].requester_id != actor.subject_id:
        raise AgentRuntimeError("agent_not_found", 404)
    return row[0], row[1], row[2], row[3]


def read_run_receipt(session: Session, actor: AuthenticatedSubject, run_id: str, *, _public_trace: bool = True) -> dict:
    _load_owned(session, actor, run_id)
    run, control, confirmation, confirmation_step = _scoped_run_control(session, actor, run_id)
    snapshot_state = run.state
    pending_snapshot = (_pending_payload(confirmation, run, confirmation_step)
                        if snapshot_state == RunState.WAITING_FOR_HUMAN.value
                        and confirmation is not None and confirmation.state == "pending"
                        and confirmation_step is not None else None)
    task_snapshot = (confirmation.result_task_id
                     if snapshot_state == RunState.COMPLETED.value
                     and confirmation is not None and confirmation.state == "approved" else None)
    result = _state_result(run, replayed=True)
    result["steps"] = _steps_payload(session, run)
    result["execution"] = _execution_view(snapshot_state, control, result["steps"])
    if pending_snapshot is not None:
        result["pending_action"] = pending_snapshot
    if task_snapshot is not None:
        result["task_id"] = task_snapshot
    if snapshot_state == RunState.COMPLETED.value:
        result.update(answer_receipts.read_answer(session, actor, run))
    # Internal submission/command receipts are not public authenticated GETs.
    # Always consult the reader even if a namespace disappeared: an existing
    # audit anchor must yield withheld, not a fake legacy record.
    if _public_trace and type(actor) is AuthenticatedSubject:
        from .public_trace_read import read_for_run
        trace = read_for_run(session, actor, run)
        if trace['state'] != 'not_recorded':
            result['public_trace'] = trace
    return result


def _cancel_active_run(session: Session, actor: AuthenticatedSubject,
                       run: AgentRun) -> AgentRun:
    run = get_run(session, run.organization_id, run.store_id, run.id) or run
    try:
        confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
        if confirmation is not None and confirmation.state == "pending":
            set_confirmation_state(session, confirmation, "rejected",
                                   reason="requester_cancelled")
        for step in list_steps(session, run.organization_id, run.store_id, run.id):
            if step.state == StepState.PENDING.value:
                change = StepTransition(to_state=StepState.CANCELLED)
            elif step.state == StepState.RUNNING.value:
                change = StepTransition(to_state=StepState.UNKNOWN,
                                        error_code="agent_request_cancelled")
            else:
                continue
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version,
                expected_step_version=step.version, change=change)
            run = get_run(session, run.organization_id, run.store_id, run.id) or run
        run = transition_run(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, expected_version=run.version,
            to_state=RunState.CANCELLED)
    except AgentPersistenceError as exc:
        session.rollback()
        raise AgentRuntimeError("agent_conflict", 409) from exc
    _audit(session, actor, run.id, "agent_run.cancelled", "requested")
    session.commit()
    return run


def _cancelled(run_id: str) -> bool:
    event = _CANCEL_EVENTS.get(run_id)
    return event is not None and event.is_set()


def _inline_cancel_receipt(session: Session, run: AgentRun) -> dict:
    """Inline cancel keeps the old semantics and has no control row.

    Cancellation acknowledges control only; it does not fetch a separate
    answer/source receipt. Identity was already checked by the command path.
    """
    result = _state_result(run)
    result["steps"] = _steps_payload(session, run)
    result["execution"] = None
    return result


def _command_receipt(session: Session, actor: AuthenticatedSubject,
                     run_id: str) -> dict:
    result = read_run_receipt(session, actor, run_id, _public_trace=False)
    result["replayed"] = False
    return result


def cancel_run(session: Session, actor: AuthenticatedSubject, run_id: str) -> dict:
    run = _load_owned(session, actor, run_id)
    control = execution_for(session, run.organization_id, run.store_id, run.id)
    if control is not None:
        if control.mode != "worker_v2":
            raise AgentRuntimeError("agent_execution_invalid_mode", 409)
        # Even an idempotent worker cancel (closed resolution, or a Run that is
        # already CANCELLED) must prove the REAL acting employee session and
        # permissions first, then re-read durable state under that guard.
        _human_guard(session, actor)
        run = _load_owned(session, actor, run_id)
        control = execution_for(session, run.organization_id, run.store_id, run.id)
        if control is None:
            raise AgentRuntimeError("agent_conflict", 409)
        # A closed execution row is only idempotent when a cancel was already
        # decided. A finished run can still be "too late": the local Task
        # receipt is kept and reported, never faked as cancelled.
        if (control.phase in ("cancelled", "unknown")
                or control.cancel_resolution in ("cancelled", "unknown", "too_late")
                or run.state == RunState.CANCELLED.value):
            return _command_receipt(session, actor, run.id)
        try:
            control = request_worker_cancel(session, run, actor.subject_id)
            if control.cancel_resolution == "too_late":
                _audit(session, actor, run.id, "agent_run.cancel_too_late",
                       control.effect_status)
            elif control.cancel_resolution == "unknown":
                _audit(session, actor, run.id, "agent_run.cancel_unknown",
                       "external_unknown")
            else:
                _audit(session, actor, run.id, "agent_run.cancel_requested", "durable")
            session.commit()
        except AgentPersistenceError as exc:
            session.rollback()
            raise AgentRuntimeError("agent_conflict", 409) from exc
        # The canonical snapshot of the durable outcome, never the pre-command
        # Run mixed with a freshly closed control row.
        return _command_receipt(session, actor, run.id)
    if run.state == RunState.CANCELLED.value:
        return _inline_cancel_receipt(session, run)
    if run.state in (RunState.COMPLETED.value, RunState.FAILED.value):
        raise AgentRuntimeError("agent_conflict", 409)
    try:
        if run.state == RunState.RUNNING.value:
            event = _CANCEL_EVENTS.get(run.id)
            if event is None:
                # No live execution (e.g. process restart): cancel is best-effort
                # only when no running step blocks the terminal transition.
                return _inline_cancel_receipt(session, _cancel_active_run(session, actor, run))
            event.set()
            _audit(session, actor, run.id, "agent_run.cancel_requested", "ok")
            session.commit()
            return _inline_cancel_receipt(session, run)
        if run.state == RunState.WAITING_FOR_HUMAN.value:
            confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
            if confirmation is None or confirmation.state != "pending":
                raise AgentRuntimeError("agent_conflict", 409)
            step = next((item for item in list_steps(session, run.organization_id,
                run.store_id, run.id) if item.id == confirmation.step_id), None)
            if step is None or step.state != StepState.PENDING.value:
                raise AgentRuntimeError("agent_conflict", 409)
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version, expected_step_version=step.version,
                change=StepTransition(to_state=StepState.CANCELLED))
            run = get_run(session, run.organization_id, run.store_id, run.id)
            set_confirmation_state(session, confirmation, "rejected", reason="requester_cancelled")
        run = transition_run(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, expected_version=run.version,
            to_state=RunState.CANCELLED)
    except AgentPersistenceError as exc:
        session.rollback()
        raise AgentRuntimeError("agent_conflict", 409) from exc
    _audit(session, actor, run.id, "agent_run.cancelled", "ok")
    session.commit()
    return _inline_cancel_receipt(session, run)


def _decode_decision(text: str) -> dict:
    if not isinstance(text, str):
        raise AgentRuntimeError("agent_decision_invalid")

    def pairs(items: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        if not 1 <= len(text.encode("utf-8")) <= 16_384:
            raise ValueError
        data = json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        # JSON escapes can decode to strings that are not valid UTF-8.
        pending = [data]
        while pending:
            value = pending.pop()
            if isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, dict):
                pending.extend(value)
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    except (ValueError, TypeError, RecursionError):
        raise AgentRuntimeError("agent_decision_invalid") from None
    if not isinstance(data, dict):
        raise AgentRuntimeError("agent_decision_invalid")
    if data.get("action") == "final" and set(data) == {"action", "answer"}:
        answer = data["answer"]
        if isinstance(answer, str) and 1 <= len(answer.strip()) <= MAX_ANSWER_LENGTH:
            return {"action": "final", "answer": answer.strip()}
    if data.get("action") == "tool" and set(data) == {
        "action", "tool_key", "version", "arguments",
    }:
        if (isinstance(data["tool_key"], str) and isinstance(data["version"], str)
                and isinstance(data["arguments"], dict)):
            return data
    raise AgentRuntimeError("agent_decision_invalid")


def _model_schema(value):
    # Pydantic UI titles consume context but add no executable constraints.
    if isinstance(value,dict):
        return {key:_model_schema(child) for key,child in value.items() if not (key == "title" and isinstance(child,str))}
    if isinstance(value,list):
        return [_model_schema(child) for child in value]
    return value


def _catalog_model_view(result: dict) -> dict:
    data = result["data"]
    fields = ("product_id", "external_code", "name", "spec", "unit", "match_type", "source_version")
    view = {key:data[key] for key in ("query", "ambiguous", "message", "exact_total",
        "candidate_total", "has_more", "scan_truncated", "truncated")}
    remaining = 3
    shown = 0
    for kind in ("exact", "candidates"):
        view[kind] = [{key:item[key] for key in fields} for item in data[kind][:remaining]]
        for compact, original in zip(view[kind],data[kind]):
            clues=original.get("search_evidence", [])
            if clues:
                clue=clues[0]
                compact["source_clue"]={"field":clue["source_field"],"value":clue["value"][:60],
                                        "certainty":clue["certainty"]}
        shown += len(view[kind])
        remaining -= len(view[kind])
    view["model_view_truncated"] = shown < len(data["exact"]) + len(data["candidates"])
    return {"key":result["key"], "version":result["version"], "data":view}


CURRENT_PROMPT_VERSION = "agt040-json-v4"


def supported_prompt(run: AgentRun, *, pi_runtime_record=None) -> bool:
    if pi_runtime_record is not None:
        try:
            record = runtime_repository.validate_pi_runtime_record(pi_runtime_record)
        except AgentPersistenceError:
            return False
        if record['contract_version'] in (2, 3):
            return (run.provider_key, run.model, run.prompt_version, run.agent_key, run.agent_version) == (
                'opencode-go', 'deepseek-v4.1-flash', CURRENT_PROMPT_VERSION, 'shop-assistant', '1')
    return (run.provider_key, run.prompt_version, run.agent_key, run.agent_version) == (
        "chat-completions", CURRENT_PROMPT_VERSION, "shop-assistant", "1")


def _prompt(user_text: str, tool_result: dict | None = None,
            context: dict | None = None, tools: list[dict] | None = None,
            approved_skill: dict | None = None) -> str:
    rules = (
        "你是五金店经营助手。请拆解任务并使用 available_tools 中当前已授权的固定工具；"
        "工具结果与来源不足时明确说明，保留人工操作路径。\n"
        "只选择 available_tools 中的工具及其精确版本，严格遵守输入 Schema。"
        "工具可见不代表额外权限；每次调用由服务器重新检查身份、范围及权限。"
        "涉及写入的工具必须遵守其确认要求，模型不能代替人工审批。\n"
        "当前不提供进程内 Python、任意 SQL 或动态代码工具创建。需要这些能力时说明当前限制"
        "并提出可由现有固定工具或人工完成的步骤；真正隔离计算与版本发布待后续能力验收。\n"
        '只输出单层 JSON 对象：回复用户为 {"action":"final","answer":"..."}；'
        '调用工具为 {"action":"tool","tool_key":"从available_tools选择",'
        '"version":"精确版本","arguments":{...}}。寒暄可直接简短回复。'
    )
    visible_tools = [{**tool, "input_schema":_model_schema(tool["input_schema"])} for tool in (tools or [])]
    payload = {"user_request": user_text, "available_tools": visible_tools}
    if approved_skill is not None:
        # Only the host's hash-matched fixed template; never arbitrary skill text.
        payload["approved_skill"] = approved_skill
    if tool_result is not None:
        payload["tool_result"] = (_catalog_model_view(tool_result)
            if tool_result.get("key") == "catalog.search" else tool_result)
    prefix = ""
    if context is not None and (context.get("summary") or context.get("messages")):
        prefix = conversation_service.format_context(context) + "\n"
    return prefix + rules + "\n" + _canonical(payload)


def _state_result(run: AgentRun, *, answer: str | None = None,
                  error_code: str | None = None, replayed: bool = False) -> dict:
    if error_code is None and run.state == RunState.RUNNING.value and _now() >= _utc(run.deadline_at):
        error_code = "agent_recovery_pending"
    return {"run_id": run.id, "state": run.state, "answer": answer,
            "answer_status": "not_recorded", "answer_unavailable_reason": None, "citations": [],
            "error_code": error_code, "replayed": replayed}


def _transition(session: Session, run: AgentRun, state: RunState) -> AgentRun:
    return transition_run(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, expected_version=run.version,
        to_state=state)


def _fail_run(session: Session, actor: AuthenticatedSubject,
              run: AgentRun, code: str, *, failed_step: AgentStep | None = None) -> dict:
    run = get_run(session, run.organization_id, run.store_id, run.id)
    if run is None:
        raise AgentRuntimeError("agent_conflict", 409)
    if run.state not in (RunState.CREATED.value, RunState.RUNNING.value):
        return _state_result(run, replayed=True)
    try:
        if failed_step is not None:
            # Only the caller-owned, known-denied step is closed. Keep this and
            # the Run terminal transition in one transaction; unknown effects
            # retain their separate recovery path.
            run = _finish_step(session, actor, run, failed_step,
                StepTransition(to_state=StepState.FAILED, error_code=code), commit=False)
        run = _transition(session, run, RunState.FAILED)
        _audit(session, actor, run.id, "agent_run.failed", code)
        session.commit()
    except AgentPersistenceError as exc:
        session.rollback()
        raise AgentRuntimeError("agent_conflict", 409) from exc
    return _state_result(run, error_code=code)


def _start_step(session: Session, run: AgentRun, kind: StepKind,
                sequence_no: int, *, tool_call: ToolCallCreate | None = None,
                input_ref: str | None = None) -> tuple[AgentRun, AgentStep]:
    step = append_step(session, StepCreate(id=str(uuid4()),
        organization_id=run.organization_id, store_id=run.store_id,
        run_id=run.id, sequence_no=sequence_no, kind=kind, input_ref=input_ref,
        tool_call=tool_call), expected_run_version=run.version)
    run = get_run(session, run.organization_id, run.store_id, run.id)
    step = transition_step(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, step_id=step.id,
        expected_run_version=run.version, expected_step_version=step.version,
        change=StepTransition(to_state=StepState.RUNNING))
    session.commit()
    return get_run(session, run.organization_id, run.store_id, run.id), step


def _finish_step(session: Session, actor: AuthenticatedSubject,
                 run: AgentRun, step: AgentStep, change: StepTransition,
                 *, commit: bool = True) -> AgentRun:
    transition_step(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, step_id=step.id,
        expected_run_version=run.version, expected_step_version=step.version,
        change=change)
    _audit(session, actor, run.id, f"agent_step.{step.id}.{change.to_state.value}",
           change.error_code or "ok")
    if commit:
        session.commit()
    return get_run(session, run.organization_id, run.store_id, run.id)


def _close_inline_memory_timeout(session, actor, root, scope, run_id, run_version,
                                 step_id, step_version):
    # A released memory savepoint has not finished the caller-owned root.
    # Never write timeout metadata into that effect root or retry the call.
    if (root is None or session.get_transaction() is not root
            or not root.is_active or session.in_nested_transaction()):
        raise AgentRuntimeError("agent_conflict", 409)
    try:
        session.rollback()
    except Exception:
        raise AgentRuntimeError("agent_conflict", 409) from None
    if session.in_transaction() or scope != (actor.organization_id, actor.store_id, actor.subject_id):
        raise AgentRuntimeError("agent_conflict", 409)

    organization_id, store_id, requester_id = scope
    _human_guard(session, actor)
    run = get_run(session, organization_id, store_id, run_id)
    if (run is None or (run.requester_id, run.version, run.state) !=
            (requester_id, run_version, RunState.RUNNING.value)
            or execution_for(session, organization_id, store_id, run_id) is not None):
        raise AgentRuntimeError("agent_conflict", 409)
    step = session.scalar(select(AgentStep).where(
        AgentStep.organization_id == organization_id, AgentStep.store_id == store_id,
        AgentStep.run_id == run_id, AgentStep.id == step_id,
    ).execution_options(populate_existing=True))
    if (step is None or (step.version, step.state, step.kind) !=
            (step_version, StepState.RUNNING.value, StepKind.TOOL.value)):
        raise AgentRuntimeError("agent_conflict", 409)

    code = "agent_tool_timeout"
    try:
        run = _finish_step(session, actor, run, step, StepTransition(
            to_state=StepState.UNKNOWN, error_code=code), commit=False)
    except AgentPersistenceError as error:
        session.rollback()
        raise AgentRuntimeError("agent_conflict", 409) from error
    return _fail_run(session, actor, run, code)


def _worker_queued_receipt(session: Session, actor: AuthenticatedSubject,
                           run: AgentRun, *, replayed: bool) -> dict:
    """The canonical receipt of an approved-but-not-yet-executed worker write."""
    result = read_run_receipt(session, actor, run.id, _public_trace=False)
    result["replayed"] = replayed
    return result


def _validate_approved_tool(session, actor, run, confirmation):
    if not supported_prompt(run):
        raise AgentRuntimeError("agent_prompt_version_unsupported", 409)
    try:
        definition = resolve_tool(confirmation.tool_key,
                                  confirmation.tool_version,
                                  store_id=run.store_id)
    except ToolRegistryError:
        raise AgentRuntimeError("agent_confirmation_conflict", 409) from None
    if definition.mode != "write":
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    agent = _agent_actor(session, actor, actor.auth_expires_at)
    authorize_scope(session, actor, organization_id=run.organization_id,
                    store_id=run.store_id, permission=definition.requester_permission)
    authorize_scope(session, agent, organization_id=run.organization_id,
                    store_id=run.store_id, permission=definition.agent_permission)
    if _digest(_canonical(confirmation.arguments)) != confirmation.input_digest:
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    return definition, agent


def _decide_worker_confirmation(session: Session, actor: AuthenticatedSubject,
                                run: AgentRun, confirmation: AgentConfirmation, *,
                                decision: str, reason: str, input_digest: str,
                                run_version: int, step_version: int) -> dict:
    """Approve/reject one worker confirmation WITHOUT running any tool.

    Approval only freezes an exact one-shot grant into the queued control row;
    the local Task can be produced later solely by the single consuming worker
    transaction. A repeated approval returns the original receipt and never
    re-signs, re-freezes or extends an earlier grant.
    """
    # Scope fence FIRST, then the execution lock domain, then a fresh re-read of
    # Run/Confirmation/Step. Nothing below writes from the pre-lock snapshot.
    epoch = current_authorization_epoch(session, run.organization_id, run.store_id)
    control = execution_for(session, run.organization_id, run.store_id, run.id,
                            for_update=True)
    if control is None or control.mode != "worker_v2":
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    runtime_repository.require_run_runtime(session, run)
    run = get_run(session, run.organization_id, run.store_id, run.id)
    confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
    if run is None or confirmation is None:
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    # The presented digest is re-checked against THIS locked re-read, not the
    # pre-lock snapshot.
    if (not isinstance(input_digest, str)
            or not compare_digest(confirmation.input_digest, input_digest)):
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    grant = control.approval_grant if isinstance(control.approval_grant, dict) else None
    if confirmation.state == "approved" and decision == "approve":
        # The durable original receipt of THIS grant, even after the control row
        # closed. A cancel-first cannot reach this branch: it rejects/fails the
        # confirmation, so the decision below is a conflict.
        if (grant is None or grant.get("input_digest") != confirmation.input_digest
                or grant.get("request_run_version") != run_version
                or grant.get("request_step_version") != step_version):
            raise AgentRuntimeError("agent_confirmation_conflict", 409)
        result = read_run_receipt(session, actor, run.id, _public_trace=False)
        result["replayed"] = True
        result["task_id"] = confirmation.result_task_id
        return result
    if confirmation.state == "rejected" and decision == "reject":
        # Cancellation also rejects the pending row. Only the scoped original
        # manual decision's audit, never caller-supplied reason text, proves replay.
        if ((run.organization_id, run.store_id, run.requester_id)
                != (actor.organization_id, actor.store_id, actor.subject_id)
                or confirmation.requester_id != actor.subject_id):
            raise AgentRuntimeError("agent_confirmation_conflict", 409)
        action = "agent_confirmation.rejected"
        rejection = get_by_idempotency_key(session, run.organization_id, run.store_id,
            f"agent_run:{run.id}:{action}")
        if (rejection is None or rejection.details != {"code": "worker_v2"}
                or (rejection.actor_id, rejection.action, rejection.target_type, rejection.target_id)
                != (actor.subject_id, action, "agent_run", run.id)):
            raise AgentRuntimeError("agent_confirmation_conflict", 409)
        result = read_run_receipt(session, actor, run.id, _public_trace=False)
        result["replayed"] = True
        return result
    if (control.phase in ("finished", "cancelled", "unknown")
            or control.cancel_resolution in ("cancelled", "unknown", "too_late")):
        # A cancel committed before this approval: it can never be approved.
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    if confirmation.state == "executing":
        # Replay of an already-frozen grant: same seen versions and digest or
        # nothing. It is never re-signed, extended or re-frozen.
        if (grant is None or grant.get("input_digest") != confirmation.input_digest
                or grant.get("request_run_version") != run_version
                or grant.get("request_step_version") != step_version):
            raise AgentRuntimeError("agent_confirmation_conflict", 409)
        if decision != "approve":
            raise AgentRuntimeError("agent_confirmation_conflict", 409)
        return _worker_queued_receipt(session, actor, run, replayed=True)
    if (run.state != RunState.WAITING_FOR_HUMAN.value
            or confirmation.state != "pending" or run.version != run_version):
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    step = next((item for item in list_steps(session, run.organization_id,
        run.store_id, run.id) if item.id == confirmation.step_id), None)
    if (step is None or step.state != StepState.PENDING.value
            or step.version != step_version):
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    if decision == "reject":
        try:
            reject_worker_confirmation(session, run=run, confirmation=confirmation,
                                       step=step, reason=reason)
            _audit(session, actor, run.id, "agent_confirmation.rejected", "worker_v2")
            session.commit()
        except AgentPersistenceError as exc:
            session.rollback()
            raise AgentRuntimeError("agent_conflict", 409) from exc
        return _command_receipt(session, actor, run.id)
    _validate_approved_tool(session, actor, run, confirmation)
    context = control.frozen_context
    evidence = context.get("evidence") if isinstance(context, dict) else None
    valid_sources, _ = answer_receipts.validate_evidence(session, actor, evidence)
    if not valid_sources:
        raise AgentRuntimeError("agent_evidence_changed", 409)
    try:
        # The fence epoch was frozen under the guard above; this explicit human
        # approval is the only thing allowed to write it into a new grant.
        run = renew_human_run_deadline(session, run)
        run = _transition(session, run, RunState.RUNNING)
        confirmation = set_confirmation_state(session, confirmation, "executing",
                                              reason=reason)
        # The Step stays PENDING: the consuming worker transaction owns its
        # RUNNING transition and the single approved tool call.
        freeze_approval_grant(session, run=run, step=step, confirmation=confirmation,
                              actor=actor, authorization_epoch=epoch,
                              request_run_version=run_version,
                              request_step_version=step_version)
        _audit(session, actor, run.id, "agent_confirmation.approved_queued", "worker_v2")
        session.commit()
    except AgentPersistenceError as exc:
        session.rollback()
        raise AgentRuntimeError("agent_conflict", 409) from exc
    updated = get_run(session, actor.organization_id, actor.store_id, run.id)
    return _worker_queued_receipt(session, actor, updated, replayed=False)


def decide_confirmation(session: Session, actor: AuthenticatedSubject, run_id: str,
                        *, decision: str, input_digest: str, reason: str,
                        run_version: int, step_version: int) -> dict:
    run = _load_owned(session, actor, run_id)
    confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
    if confirmation is None:
        raise AgentRuntimeError("agent_confirmation_not_found", 404)
    if not compare_digest(confirmation.input_digest, input_digest):
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    # Every real HTTP decision takes the human command guard first, then the
    # execution lock domain, then re-reads Run/Confirmation/Step and keeps this
    # session through the commit.
    _human_guard(session, actor)
    run = _load_owned(session, actor, run_id)
    confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
    if confirmation is None:
        raise AgentRuntimeError("agent_confirmation_not_found", 404)
    control = execution_for(session, run.organization_id, run.store_id, run.id)
    if control is not None:
        if control.mode != "worker_v2":
            raise AgentRuntimeError("agent_execution_invalid_mode", 409)
        return _decide_worker_confirmation(session, actor, run, confirmation,
            decision=decision, reason=reason, input_digest=input_digest,
            run_version=run_version, step_version=step_version)
    if confirmation.state == "approved" and decision == "approve":
        return {**_state_result(run, replayed=True), "task_id": confirmation.result_task_id}
    if confirmation.state == "rejected" and decision == "reject":
        # Cancellation is not a human rejection receipt. Use its scoped audit
        # identity rather than the caller-controlled rejection reason.
        cancellation = get_by_idempotency_key(session, run.organization_id,
            run.store_id, f"agent_run:{run.id}:agent_run.cancelled")
        if (cancellation is not None
                and (cancellation.action, cancellation.target_type, cancellation.target_id)
                == ("agent_run.cancelled", "agent_run", run.id)):
            raise AgentRuntimeError("agent_confirmation_conflict", 409)
        return _state_result(run, replayed=True)
    if run.state != RunState.WAITING_FOR_HUMAN.value or confirmation.state != "pending":
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    step = next((item for item in list_steps(session, run.organization_id,
        run.store_id, run.id) if item.id == confirmation.step_id), None)
    if step is None or step.state != StepState.PENDING.value:
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    if run.version != run_version or step.version != step_version:
        raise AgentRuntimeError("agent_confirmation_conflict", 409)
    if _now() >= _utc(confirmation.expires_at):
        try:
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version, expected_step_version=step.version,
                change=StepTransition(to_state=StepState.CANCELLED))
            run = get_run(session, run.organization_id, run.store_id, run.id)
            set_confirmation_state(session, confirmation, "expired")
            run = _transition(session, run, RunState.FAILED)
            _audit(session, actor, run.id, "agent_confirmation.expired", "expired")
            session.commit()
        except AgentPersistenceError as exc:
            session.rollback()
            raise AgentRuntimeError("agent_confirmation_conflict", 409) from exc
        raise AgentRuntimeError("agent_confirmation_expired", 410)
    if decision == "reject":
        try:
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version, expected_step_version=step.version,
                change=StepTransition(to_state=StepState.CANCELLED))
            run = get_run(session, run.organization_id, run.store_id, run.id)
            set_confirmation_state(session, confirmation, "rejected", reason=reason)
            run = _transition(session, run, RunState.CANCELLED)
            _audit(session, actor, run.id, "agent_confirmation.rejected", "ok")
            session.commit()
        except AgentPersistenceError as exc:
            session.rollback()
            raise AgentRuntimeError("agent_confirmation_conflict", 409) from exc
        return _state_result(run)
    definition, agent = _validate_approved_tool(session, actor, run, confirmation)
    # B0: policy refusal is not a human rejection and cannot fail a pending Step.
    with session.no_autoflush:
        if skill_service.runtime_pin(session, run) is not None:
            raise AgentRuntimeError("agent_confirmation_skill_pin_unsupported", 409)
        try:
            _verify_inline_approval_pin(session, actor, agent, run)
        except ToolRegistryError as error:
            if error.code == "agent_worker_toolset_changed":
                raise AgentRuntimeError(error.code, 409) from error
            raise
    try:
        run = renew_human_run_deadline(session, run)
        run = _transition(session, run, RunState.RUNNING)
        transition_step(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, step_id=step.id,
            expected_run_version=run.version, expected_step_version=step.version,
            change=StepTransition(to_state=StepState.RUNNING))
        run = get_run(session, run.organization_id, run.store_id, run.id)
        confirmation = set_confirmation_state(session, confirmation, "executing", reason=reason)
        _audit(session, actor, run.id, "agent_confirmation.claimed", "ok")
        task_claim = _capture_inline_task_claim(session, actor, agent, run, step, confirmation, definition)
        session.commit()
    except AgentPersistenceError as exc:
        session.rollback()
        raise AgentRuntimeError("agent_confirmation_conflict", 409) from exc
    # B1: this real claim commit returned, but this invocation has not started.
    # That is NOT a before-commit proof and does not prove an old receipt absent.
    try:
        _verify_inline_approval_pin(session, actor, agent, run)
    except ToolRegistryError as error:
        if error.code != "agent_worker_toolset_changed":
            raise
        known_not_invoked = task_claim is not None
        try:
            session.rollback()
        except Exception:
            known_not_invoked = False
        if known_not_invoked:
            try:
                _close_inline_final_rejection(session, actor, task_claim, error.code)
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                # Keep the original pin error and actual unresolved claim facts.
        raise AgentRuntimeError(error.code, 403) from error
    call_key = f"{run.id}:{step.sequence_no}"
    start = time.monotonic()
    try:
        result = invoke_tool(session, requester=actor, agent=agent,
            tool_key=confirmation.tool_key, version=confirmation.tool_version,
            arguments=confirmation.arguments, idempotency_key=call_key)
    except ToolRegistryError as exc:
        session.rollback()
        run = get_run(session, actor.organization_id, actor.store_id, run_id)
        step = next(item for item in list_steps(session, run.organization_id,
            run.store_id, run.id) if item.id == confirmation.step_id)
        _audit(session, actor, run.id, "agent_confirmation.denied", exc.code)
        run = _finish_step(session, actor, run, step, StepTransition(
            to_state=StepState.FAILED, error_code=exc.code))
        set_confirmation_state(session, confirmation, "failed", reason=reason)
        return _fail_run(session, actor, run, exc.code)
    except Exception:
        session.rollback()
        run = get_run(session, actor.organization_id, actor.store_id, run_id)
        step = next(item for item in list_steps(session, run.organization_id,
            run.store_id, run.id) if item.id == confirmation.step_id)
        run = _finish_step(session, actor, run, step, StepTransition(
            to_state=StepState.UNKNOWN, error_code="agent_tool_error"))
        set_confirmation_state(session, confirmation, "unknown", reason=reason)
        return _fail_run(session, actor, run, "agent_tool_error")
    # The claim transaction above has ended. Only the successful invoke has
    # established the real effect root whose before-commit failure can prove
    # that THIS attempted effect was rejected without a commit.
    captured_root = session.get_transaction()
    if time.monotonic() - start > definition.timeout_seconds or _now() >= _utc(run.deadline_at):
        # This first write tool only changes the caller-owned DB transaction.
        # Discard its staged task/audit before recording an unknown result.
        session.rollback()
        run = get_run(session, actor.organization_id, actor.store_id, run_id)
        step = next(item for item in list_steps(session, run.organization_id,
            run.store_id, run.id) if item.id == confirmation.step_id)
        run = _finish_step(session, actor, run, step, StepTransition(
            to_state=StepState.UNKNOWN, error_code="agent_tool_timeout"))
        set_confirmation_state(session, confirmation, "unknown", reason=reason)
        return _fail_run(session, actor, run, "agent_tool_timeout")
    try:
        # This success path owns ONE local effect transaction. _finish_step
        # commits, so use its existing transition/audit primitives directly.
        transition_step(session, organization_id=run.organization_id, store_id=run.store_id,
            run_id=run.id, step_id=step.id, expected_run_version=run.version,
            expected_step_version=step.version, change=StepTransition(
                to_state=StepState.SUCCEEDED, output_ref="sha256:" + _digest(_canonical(result))))
        _audit(session, actor, run.id, f"agent_step.{step.id}.{StepState.SUCCEEDED.value}", "ok")
        run = get_run(session, run.organization_id, run.store_id, run.id)
        confirmation = set_confirmation_state(session, confirmation, "approved",
                                               reason=reason, result_task_id=result.get("task_id"))
        run = _transition(session, run, RunState.COMPLETED)
        _audit(session, actor, run.id, "agent_confirmation.approved", "ok")
        session.flush()
        def final(db, instant):
            if (run.state != RunState.COMPLETED.value or confirmation.state != "approved"
                    or confirmation.result_task_id != result.get("task_id")):
                raise AgentRuntimeError("agent_confirmation_conflict", 409)
            if instant >= _utc(confirmation.expires_at) or instant >= _utc(run.deadline_at):
                raise AgentRuntimeError("agent_confirmation_expired", 410)
            if _digest(_canonical(confirmation.arguments)) != confirmation.input_digest:
                raise AgentRuntimeError("agent_confirmation_conflict", 409)
            current = resolve_tool(confirmation.tool_key, confirmation.tool_version, store_id=run.store_id)
            if (current.input_sha256, current.output_sha256, current.handler_id) != (
                    definition.input_sha256, definition.output_sha256, definition.handler_id):
                raise AgentRuntimeError("agent_confirmation_conflict", 409)
            for principal, permission in ((actor, "agents.run"), (actor, definition.requester_permission),
                                         (agent, definition.agent_permission)):
                authorize_scope(db, principal, organization_id=run.organization_id,
                                store_id=run.store_id, permission=permission, now=instant)
            _verify_inline_approval_pin(db, actor, agent, run)
        commit_guard.enlist(session, action="agent.inline_approved_effect", check=final)
        session.commit()
    except Exception as error:
        code = _known_inline_rejection(error)
        proven_rejection = code is not None and commit_guard.rejected_before_commit(
            session, captured_root, error)
        can_close = task_claim is not None and proven_rejection
        try:
            session.rollback()
        except Exception:
            # Do not turn a rollback/connection failure into "no effect" or
            # replace the original public error with a cleanup error.
            can_close = False
        if can_close:
            try:
                _close_inline_final_rejection(session, actor, task_claim, code)
            except Exception:
                try:
                    session.rollback()
                except Exception:
                    pass
                # Durable claim + real deadline remain for controlled recovery.
        if isinstance(error, ToolRegistryError):
            raise AgentRuntimeError(error.code, 403) from error
        # Rollback eligibility cannot erase the already proven rejection source.
        # Only the exact pin error from this root gets the wrapper mapping.
        if proven_rejection and code == "agent_worker_toolset_changed":
            raise AgentRuntimeError(code, 403) from error
        raise
    return {**_state_result(run), "task_id": confirmation.result_task_id}


def _verify_inline_approval_pin(session, actor, agent, run):
    """Pure approval check: never flush domain work or acquire new authority locks."""
    from backend.app.modules.identity.command_guard import refresh_authority
    with session.no_autoflush:
        refresh_authority(session)
        _verify_ordinary_toolset(session, actor, agent, run)


def _verify_ordinary_toolset(session, actor, agent, run):
    """Revalidate the original pin against the currently visible executable catalog.

    This is only the ordinary synchronous path. Skill and approval-resume pins
    retain their separate contracts; a mismatch never repins an existing Run.
    """
    verify_toolset_digest(model_tool_descriptions(session, actor, agent),
                          run.toolset_version, store_id=run.store_id)


async def create_and_execute(session: Session, actor: AuthenticatedSubject,
                             text: str, key: str,
                             requested_run_id: str | None = None) -> dict:
    runtime_repository._reject_runtime_mutations(session)
    _scope(session, actor)
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_MESSAGE_CHARS:
        raise AgentRuntimeError("agent_input_invalid")
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 200:
        raise AgentRuntimeError("agent_idempotency_invalid")
    if requested_run_id is not None:
        if (not isinstance(requested_run_id, str)
                or not _IDENTIFIER.fullmatch(requested_run_id)
                or len(requested_run_id) > 36):
            raise AgentRuntimeError("agent_run_id_invalid")
        run_id = requested_run_id
    else:
        run_id = _run_id(actor, key.strip())
    input_ref = "sha256:" + _digest(text)
    existing = get_run(session, actor.organization_id, actor.store_id, run_id)
    if existing is not None:
        runtime_repository.require_run_runtime(session, existing)
        if existing.requester_id != actor.subject_id or existing.input_ref != input_ref:
            raise AgentRuntimeError("agent_idempotency_conflict", 409)
        return read_run_receipt(session, actor, run_id, _public_trace=False)
    conversation = conversation_service.get_or_create_conversation(session, actor)
    context = conversation_service.context_for_agent(session, actor,
        budget_tokens=input_capacity(output_tokens=MAX_OUTPUT_TOKENS,
                                     remaining_input=MAX_INPUT_TOKENS))
    evidence = context["evidence"]
    user_message = conversation_service.append_message(
        session, actor, conversation_id=conversation.id, role="user", content=text)
    evidence = answer_receipts.merge_evidence(
        evidence, [answer_receipts.message_reference(user_message)])
    own_tool_evidence: list[dict] = []
    deadline = min(_now() + timedelta(seconds=MAX_SECONDS), actor.auth_expires_at)
    agent = _agent_actor(session, actor, deadline)
    model = os.environ.get("AGENT_PROVIDER_MODEL", "unconfigured")
    if (not isinstance(model, str) or not model.isascii() or
            not 1 <= len(model) <= 200 or any(not "!" <= ch <= "~" for ch in model)):
        model = "unconfigured"
    tool_descriptions = model_tool_descriptions(session, actor, agent)
    choice, selection_reason = skill_service.choose_runtime_skill(
        session, actor, agent, text, provider_key="chat-completions",
        model=model, prompt_version=CURRENT_PROMPT_VERSION)
    if choice is not None:
        # The skill can REMOVE tools and context, never introduce a grant or
        # reuse prior private conversation messages in a specialist role.
        tool_descriptions = choice["tool_descriptions"]
        evidence = [answer_receipts.message_reference(user_message)]
    try:
        run = create_run(session, RunCreate(id=run_id,
            organization_id=actor.organization_id, store_id=actor.store_id,
            requester_id=actor.subject_id, agent_key="shop-assistant",
            agent_version="1", provider_key="chat-completions", model=model,
            prompt_version=CURRENT_PROMPT_VERSION,
            toolset_version=(choice["binding"]["toolset_version"] if choice is not None
                             else toolset_digest(tool_descriptions,
                                                 store_id=actor.store_id)),
            input_ref=input_ref, max_input_tokens=run_input_budget(model,
                model_calls=MAX_MODEL_CALLS, output_tokens=MAX_OUTPUT_TOKENS,
                explicit_limit=MAX_INPUT_TOKENS),
            max_output_tokens=MAX_OUTPUT_TOKENS, max_model_calls=MAX_MODEL_CALLS,
            max_tool_calls=MAX_TOOL_CALLS, max_steps=MAX_STEPS,
            deadline_at=deadline))
        if choice is not None:
            skill_service.freeze_run_skill(session, actor, run, choice)
    except AgentPersistenceError as exc:
        session.rollback()
        existing = get_run(session, actor.organization_id, actor.store_id, run_id)
        if existing is not None and existing.requester_id == actor.subject_id and existing.input_ref == input_ref:
            return read_run_receipt(session, actor, existing.id, _public_trace=False)
        raise AgentRuntimeError("agent_idempotency_conflict", 409) from exc
    _audit(session, actor, run.id, "agent_run.created", "ok")
    if os.environ.get("AGENT_SKILL_RUNTIME_ENABLED") == "1":
        _audit(session, actor, run.id, "agent_skill.selection", (
            choice["binding"]["skill_key"] if choice is not None else selection_reason))
    run = _transition(session, run, RunState.RUNNING)
    session.commit()
    cancel_event = threading.Event()
    try:
        _CANCEL_EVENTS[run_id] = cancel_event
        if not supported_prompt(run):
            return _fail_run(session, actor, run, "agent_prompt_version_unsupported")
        try:
            provider = _provider_factory()
        except ProviderError as exc:
            return _fail_run(session, actor, run, exc.code)
        # A real network adapter must use the identity frozen in this Run, even
        # if deployment configuration changed between submission and dispatch.
        if isinstance(provider, ChatCompletionsProvider) and provider.config.model != run.model:
            return _fail_run(session, actor, run, "agent_provider_configuration_invalid")

        tool_result: dict | None = None
        skill_prompt = ({
            "skill_key": choice["binding"]["skill_key"],
            "release_tag": choice["binding"]["release_tag"],
            "spec_sha256": choice["binding"]["spec_sha256"],
            "instruction": choice["instruction"],
        } if choice is not None else None)
        sequence_no = 1
        while sequence_no <= run.max_steps:
            if _cancelled(run_id):
                run = _cancel_active_run(session, actor, run)
                return _state_result(run)
            run = get_run(session, actor.organization_id, actor.store_id, run_id)
            if run is None or run.state != RunState.RUNNING.value:
                raise AgentRuntimeError("agent_conflict", 409)
            if choice is None:
                try:
                    _verify_ordinary_toolset(session, actor, agent, run)
                except ToolRegistryError as exc:
                    return _fail_run(session, actor, run, exc.code)
            if not supported_prompt(run):
                return _fail_run(session, actor, run, "agent_prompt_version_unsupported")
            if _now() >= _utc(run.deadline_at):
                return _fail_run(session, actor, run, "agent_deadline_exceeded")
            steps = list_steps(session, actor.organization_id, actor.store_id, run.id)
            if sum(s.kind == StepKind.MODEL.value for s in steps) >= run.max_model_calls:
                return _fail_run(session, actor, run, "agent_budget_exceeded")
            remaining_in = run.max_input_tokens - sum(s.input_tokens or 0 for s in steps)
            remaining_out = run.max_output_tokens - sum(s.output_tokens or 0 for s in steps)
            if remaining_in <= 0 or remaining_out <= 0:
                return _fail_run(session, actor, run, "agent_budget_exceeded")
            if choice is not None:
                try:
                    skill_service.verify_runtime_binding(session, actor, agent, run)
                except (skill_service.SkillReleaseError, HTTPException):
                    return _fail_run(session, actor, run, "skill_runtime_revoked")
            try:
                remaining_in = input_capacity(run.model, output_tokens=remaining_out,
                                              remaining_input=remaining_in)
                prompt_text = fit_prompt(
                    lambda history: _prompt(text, tool_result, history,
                        tool_descriptions, approved_skill=skill_prompt),
                    None if choice is not None else context, remaining_in)
            except ModelContextError as exc:
                return _fail_run(session, actor, run, exc.code)
            try:
                run, step = _start_step(session, run, StepKind.MODEL, sequence_no,
                                        input_ref="sha256:" + _digest(prompt_text))
            except AgentPersistenceError as exc:
                session.rollback()
                return _fail_run(session, actor, run, exc.code)
            # Reserve before the request leaves the process. Any configuration,
            # budget or boundedness failure stops here with zero model calls.
            try:
                cost_ledger.reserve_cost(
                    session, actor, run, step,
                    max_input_tokens=remaining_in, max_output_tokens=remaining_out)
            except cost_ledger.CostLedgerError as exc:
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.FAILED, error_code=exc.code))
                return _fail_run(session, actor, run, exc.code)
            if _cancelled(run_id):
                # Explicit pre-call rejection is the only release path.
                try:
                    cost_ledger.release_cost(session, actor, run, step,
                                             reason="agent_request_cancelled")
                except cost_ledger.CostLedgerError:
                    session.rollback()
                run = _cancel_active_run(session, actor, run)
                return _state_result(run)
            run = get_run(session, actor.organization_id, actor.store_id, run_id)
            if not supported_prompt(run):
                cost_ledger.release_cost(session, actor, run, step,
                                        reason="agent_prompt_version_unsupported")
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.FAILED, error_code="agent_prompt_version_unsupported"))
                return _fail_run(session, actor, run, "agent_prompt_version_unsupported")
            # A1: reservation is durable but this physical request has not left yet.
            if choice is None:
                try:
                    _verify_ordinary_toolset(session, actor, agent, run)
                except ToolRegistryError as exc:
                    code = exc.code
                    try:
                        cost_ledger.release_cost(session, actor, run, step, reason=code)
                    except cost_ledger.CostLedgerError as release_error:
                        session.rollback()
                        code = release_error.code
                    run = _finish_step(session, actor, run, step, StepTransition(
                        to_state=StepState.FAILED, error_code=code))
                    return _fail_run(session, actor, run, code)
            try:
                response = await provider.complete(ProviderRequest(
                    organization_id=actor.organization_id, store_id=actor.store_id,
                    requester_id=actor.subject_id, task_id=run.id,
                    purpose="shop_assistant", input_text=prompt_text,
                    budget=CallBudget(remaining_in, remaining_out),
                    deadline_at=_utc(run.deadline_at)))
                if not isinstance(response, ProviderResponse):
                    raise ProviderError("provider_invalid_response")
            except ProviderError as exc:
                if _cancelled(run_id):
                    try:
                        cost_ledger.mark_cost_unknown(
                            session, actor, run, step, reason="agent_request_cancelled")
                    except cost_ledger.CostLedgerError:
                        session.rollback()
                    run = _cancel_active_run(session, actor, run)
                    return _state_result(run)
                try:
                    if exc.input_tokens is not None and exc.output_tokens is not None:
                        cost_ledger.settle_cost(session, actor, run, step,
                            input_tokens=exc.input_tokens, output_tokens=exc.output_tokens)
                    else:
                        cost_ledger.mark_cost_unknown(session, actor, run, step,
                                                      reason=exc.code)
                except cost_ledger.CostLedgerError:
                    session.rollback()
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.FAILED, error_code=exc.code,
                    input_tokens=exc.input_tokens, output_tokens=exc.output_tokens))
                return _fail_run(session, actor, run, exc.code)
            except asyncio.CancelledError:
                try:
                    cost_ledger.mark_cost_unknown(session, actor, run, step,
                                                  reason="agent_request_cancelled")
                except cost_ledger.CostLedgerError:
                    session.rollback()
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.UNKNOWN, error_code="agent_request_cancelled"))
                _fail_run(session, actor, run, "agent_request_cancelled")
                raise
            except Exception:
                if _cancelled(run_id):
                    try:
                        cost_ledger.mark_cost_unknown(
                            session, actor, run, step, reason="agent_request_cancelled")
                    except cost_ledger.CostLedgerError:
                        session.rollback()
                    run = _cancel_active_run(session, actor, run)
                    return _state_result(run)
                try:
                    cost_ledger.mark_cost_unknown(session, actor, run, step,
                                                  reason="agent_provider_error")
                except cost_ledger.CostLedgerError:
                    session.rollback()
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.FAILED, error_code="agent_provider_error"))
                return _fail_run(session, actor, run, "agent_provider_error")

            run = _finish_step(session, actor, run, step, StepTransition(
                to_state=StepState.SUCCEEDED,
                input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                output_ref="sha256:" + _digest(response.suggestion)))
            try:
                cost_ledger.settle_cost(session, actor, run, step,
                    input_tokens=response.input_tokens, output_tokens=response.output_tokens)
            except cost_ledger.CostLedgerError as exc:
                return _fail_run(session, actor, run, exc.code)
            if _cancelled(run_id):
                run = _cancel_active_run(session, actor, run)
                return _state_result(run)
            if response.input_tokens > remaining_in or response.output_tokens > remaining_out:
                return _fail_run(session, actor, run, "agent_budget_exceeded")
            if choice is None:
                try:
                    _verify_ordinary_toolset(session, actor, agent, run)
                except ToolRegistryError as exc:
                    return _fail_run(session, actor, run, exc.code)
            if not supported_prompt(run):
                return _fail_run(session, actor, run, "agent_prompt_version_unsupported")
            if choice is not None:
                try:
                    skill_service.verify_runtime_binding(session, actor, agent, run)
                except (skill_service.SkillReleaseError, HTTPException):
                    return _fail_run(session, actor, run, "skill_runtime_revoked")
            try:
                decision = _decode_decision(response.suggestion)
            except AgentRuntimeError as exc:
                return _fail_run(session, actor, run, exc.code)
            if decision["action"] == "final":
                if choice is not None:
                    try:
                        skill_service.verify_runtime_binding(session, actor, agent, run)
                    except (skill_service.SkillReleaseError, HTTPException):
                        return _fail_run(session, actor, run, "skill_runtime_revoked")
                try:
                    run = _transition(session, run, RunState.COMPLETED)
                    message = conversation_service.append_message(
                        session, actor, conversation_id=conversation.id, role="assistant",
                        content=decision["answer"], source_ref=f"run:{run.id}")
                    record_answer(session, run=run, message=message, evidence=evidence,
                        own_evidence=own_tool_evidence)
                    _audit(session, actor, run.id, "agent_run.completed", "ok")
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                result = read_run_receipt(session, actor, run.id, _public_trace=False)
                result["replayed"] = False
                return result

            tool_key, version, arguments = (decision["tool_key"],
                                            decision["version"], decision["arguments"])
            if not _IDENTIFIER.fullmatch(tool_key) or not _IDENTIFIER.fullmatch(version):
                return _fail_run(session, actor, run, "agent_decision_invalid")
            if legacy_code_tool(tool_key, version, store_id=actor.store_id):
                return _fail_run(session, actor, run, "tool_legacy_code_execution_disabled")
            try:
                definition = resolve_tool(tool_key, version, store_id=actor.store_id)
            except ToolRegistryError:
                return _fail_run(session, actor, run, "tool_not_registered")
            if choice is not None and (tool_key, version) not in choice["plan"].allowed_tools:
                return _fail_run(session, actor, run, "skill_tool_not_allowed")
            if sequence_no >= run.max_steps or sum(s.kind == StepKind.TOOL.value for s in steps) >= run.max_tool_calls:
                return _fail_run(session, actor, run, "agent_budget_exceeded")
            if _cancelled(run_id):
                run = _cancel_active_run(session, actor, run)
                return _state_result(run)
            if definition is not None and definition.mode == "write" and definition.confirmation != "none":
                try:
                    validated = definition.input_model.model_validate(arguments)
                    arguments = validated.model_dump(mode="json")
                    authorize_scope(session, actor, organization_id=run.organization_id,
                                    store_id=run.store_id, permission=definition.requester_permission)
                    authorize_scope(session, agent, organization_id=run.organization_id,
                                    store_id=run.store_id, permission=definition.agent_permission)
                except (ValidationError, HTTPException):
                    session.rollback()
                    return _fail_run(session, actor, run, "agent_tool_confirmation_denied")
                if _cancelled(run_id):
                    run = _cancel_active_run(session, actor, run)
                    return _state_result(run)
                call_key = f"{run.id}:{sequence_no + 1}"
                digest = _digest(_canonical(arguments))
                try:
                    step = append_step(session, StepCreate(id=str(uuid4()),
                        organization_id=run.organization_id, store_id=run.store_id,
                        run_id=run.id, sequence_no=sequence_no + 1, kind=StepKind.TOOL,
                        tool_call=ToolCallCreate(id=str(uuid4()), tool_key=tool_key,
                            tool_version=version, idempotency_key=call_key,
                            input_digest=digest)), expected_run_version=run.version)
                    run = get_run(session, run.organization_id, run.store_id, run.id)
                    now = _now()
                    confirmation = create_confirmation(session, AgentConfirmation(
                        run_id=run.id, organization_id=run.organization_id,
                        store_id=run.store_id, step_id=step.id,
                        requester_id=actor.subject_id, tool_key=tool_key,
                        tool_version=version, arguments=arguments,
                        input_digest=digest, state="pending", version=1,
                        expires_at=now + timedelta(seconds=CONFIRMATION_SECONDS),
                        created_at=now, updated_at=now))
                    run = _transition(session, run, RunState.WAITING_FOR_HUMAN)
                    _audit(session, actor, run.id, "agent_confirmation.requested", "pending")
                    session.commit()
                except AgentPersistenceError as exc:
                    session.rollback()
                    return _fail_run(session, actor, run, exc.code)
                if _cancelled(run_id):
                    run = _cancel_active_run(session, actor, run)
                    return _state_result(run)
                return {**_state_result(run), "pending_action": _pending_payload(confirmation, run, step)}
            call_key = f"{run.id}:{sequence_no + 1}"
            call = ToolCallCreate(id=str(uuid4()), tool_key=tool_key,
                tool_version=version, idempotency_key=call_key,
                input_digest=_digest(_canonical(arguments)))
            try:
                run, step = _start_step(session, run, StepKind.TOOL,
                                        sequence_no + 1, tool_call=call)
            except AgentPersistenceError as exc:
                session.rollback()
                return _fail_run(session, actor, run, exc.code)
            limit = definition.timeout_seconds
            start = time.monotonic()
            if _now() >= _utc(run.deadline_at):
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.UNKNOWN, error_code="agent_tool_timeout"))
                return _fail_run(session, actor, run, "agent_tool_timeout")
            if _cancelled(run_id):
                run = _cancel_active_run(session, actor, run)
                return _state_result(run)
            try:
                if choice is None:
                    # A3: the tool Step is committed; rejection must close that Step.
                    _verify_ordinary_toolset(session, actor, agent, run)
                if choice is not None:
                    # The same platform tool gateway enforces grants on each call.
                    skill_service.verify_runtime_binding(session, actor, agent, run)
                memory_timeout_claim = None
                if (version == "1" and tool_key in ("memory.save", "memory.correct", "memory.withdraw")
                        and definition.mode == "write" and definition.handler_id == f"{tool_key}.v1"):
                    memory_timeout_claim = (
                        session.get_transaction(), (run.organization_id, run.store_id, run.requester_id),
                        run.id, run.version, step.id, step.version)
                result = invoke_tool(session, requester=actor, agent=agent,
                    tool_key=tool_key, version=version, arguments=arguments,
                    idempotency_key=call_key if definition.idempotency == "required" else None)
            except (skill_service.SkillReleaseError, HTTPException):
                # Discard any uncommitted handler writes before recording the known
                # rejection against the already committed exact tool Step.
                session.rollback()
                return _fail_run(session, actor, run, "skill_runtime_revoked", failed_step=step)
            except ToolRegistryError as exc:
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.FAILED, error_code=exc.code))
                return _fail_run(session, actor, run, exc.code)
            if time.monotonic() - start > limit or _now() >= _utc(run.deadline_at):
                if memory_timeout_claim is not None:
                    return _close_inline_memory_timeout(session, actor, *memory_timeout_claim)
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.UNKNOWN, error_code="agent_tool_timeout"))
                return _fail_run(session, actor, run, "agent_tool_timeout")
            try:
                captured = answer_receipts.capture_tool_evidence(session, actor, tool_key=tool_key,
                        permission=definition.requester_permission, result=result)
                evidence = answer_receipts.merge_evidence(evidence, captured)
                own_tool_evidence = answer_receipts.merge_evidence(own_tool_evidence, captured)
            except answer_receipts.EvidenceError as exc:
                run = _finish_step(session, actor, run, step, StepTransition(
                    to_state=StepState.FAILED, error_code=str(exc)))
                return _fail_run(session, actor, run, str(exc))
            run = _finish_step(session, actor, run, step, StepTransition(
                to_state=StepState.SUCCEEDED,
                output_ref="sha256:" + _digest(_canonical(result))))
            tool_result = {"key": tool_key, "version": version, "data": result}
            # A model step follows every tool step. Sequence numbers count both.
            sequence_no += 2

        return _fail_run(session, actor, run, "agent_budget_exceeded")
    finally:
        if _CANCEL_EVENTS.get(run_id) is cancel_event:
            del _CANCEL_EVENTS[run_id]


def submit_worker_run(session: Session, actor: AuthenticatedSubject, text: str,
                      key: str, requested_run_id: str | None = None, *,
                      runtime_binding=_NO_RUNTIME_BINDING,
                      tool_profile: str | None = None,
                      pi_runtime_record: dict | None = None,
                      category_source: dict | None = None,
                      category_products_contract: str | None = None, research_source: dict | None = None,
                      sales_source: dict | None = None,
                      tool_selection: list | None = None) -> dict:
    """Persist a bounded queued run; never dispatch a Provider in HTTP.

    Controlled feature gate is OFF until all AGT-020 security-fence writers and
    real multi-process PostgreSQL acceptance have been independently verified.
    """
    runtime_context = ({} if runtime_binding is _NO_RUNTIME_BINDING
                       else {"runtime_binding": runtime_binding})
    expected_runtime = runtime_repository.runtime_identity(runtime_context)
    # Trusted Host-only opt-in, not an HTTP/model-configurable tool export.
    # Existing routes and experimental bridge keep their original default.
    if tool_profile is not None and (
            type(tool_profile) is not str or tool_profile not in PI_ALL_PROFILES
            or expected_runtime != runtime_repository.SDK_RUNTIME):
        raise AgentRuntimeError("agent_pi_profile_invalid", 409)
    # Workbench runs must carry an explicit configured tool_selection upper
    # bound; omission never defaults to all tools, and other profiles never
    # accept one.
    if tool_profile == 'pi-workbench-v1':
        if (type(tool_selection) is not list or not tool_selection
                or any(type(item) is not dict or set(item) != {"tool_key", "version"}
                       or type(item.get("tool_key")) is not str or type(item.get("version")) is not str
                       for item in tool_selection)
                or sorted({(item["tool_key"], item["version"]) for item in tool_selection})
                    != [(item["tool_key"], item["version"]) for item in tool_selection]):
            raise AgentRuntimeError("agent_pi_toolset_invalid", 409)
    elif tool_selection is not None:
        raise AgentRuntimeError("agent_pi_profile_invalid", 409)
    if pi_runtime_record is not None:
        if tool_profile not in PI_ALL_PROFILES:
            raise AgentRuntimeError("agent_pi_profile_invalid", 409)
        try:
            pi_runtime_record = runtime_repository.validate_pi_runtime_record(pi_runtime_record)
            if runtime_repository.pi_record_tool_profile(pi_runtime_record) != tool_profile:
                raise AgentRuntimeError("agent_pi_profile_invalid", 409)
        except AgentPersistenceError as exc:
            raise AgentRuntimeError(exc.code, 409) from exc
    if research_source is not None:
        from . import research_host
        try:research_source=research_host.current(session.get_bind(),actor,pin=research_source).pin()
        except (ValueError,TypeError,KeyError,OSError):raise AgentRuntimeError('agent_pi_research_source_invalid',409) from None
        if tool_profile not in ('pi-source-v1', 'pi-workbench-v1') or pi_runtime_record is None or pi_runtime_record['wire_protocol'] not in ('v4.pi-source/2', 'v4.pi-workbench/2'):
            raise AgentRuntimeError('agent_pi_research_source_invalid',409)
    if category_source is not None:
        from backend.app.modules.integrations.guanjia import category_host
        try:
            current = category_host.current(session.get_bind(),actor,pin=category_source)
            category_source = current.pin()
        except (ValueError,TypeError,KeyError,OSError):
            raise AgentRuntimeError('agent_pi_category_source_invalid',409) from None
        if tool_profile not in ('pi-source-v1', 'pi-workbench-v1') or pi_runtime_record is None or pi_runtime_record['wire_protocol'] not in ('v4.pi-source/2', 'v4.pi-workbench/2'):
            raise AgentRuntimeError('agent_pi_category_source_invalid',409)
    if category_products_contract is not None and (
            type(category_products_contract) is not str
            or category_products_contract != 'v4.guanjia-category/2'
            or tool_profile not in ('pi-source-v1', 'pi-workbench-v1') or category_source is None):
        raise AgentRuntimeError('agent_pi_category_source_invalid', 409)
    if sales_source is not None:
        from backend.app.modules.integrations.guanjia import sales_ranking_source
        try:
            sales_source = sales_ranking_source.current(session.get_bind(), actor,
                                                         pin=sales_source).pin_dict()
        except (ValueError, TypeError, KeyError, OSError):
            raise AgentRuntimeError('agent_pi_sales_source_invalid', 409) from None
        if (tool_profile not in ('pi-source-v1', 'pi-workbench-v1') or pi_runtime_record is None
                or pi_runtime_record['wire_protocol'] not in ('v4.pi-source/2', 'v4.pi-workbench/2')):
            raise AgentRuntimeError('agent_pi_sales_source_invalid', 409)
    runtime_repository._reject_runtime_mutations(session)
    # Copy the exact accepted value before any transaction or caller mutation.
    runtime_context = ({} if expected_runtime == runtime_repository.LEGACY_RUNTIME
                       else {"runtime_binding": runtime_repository.sdk_runtime_binding()})
    if pi_runtime_record is not None:
        runtime_context["pi_runtime_record"] = pi_runtime_record
    if os.environ.get("AGENT_WORKER_SUBMIT_ENABLED") != "1":
        raise AgentRuntimeError("agent_worker_submission_disabled", 503)
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_MESSAGE_CHARS:
        raise AgentRuntimeError("agent_input_invalid")
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 200:
        raise AgentRuntimeError("agent_idempotency_invalid")
    if requested_run_id is not None:
        if (not isinstance(requested_run_id, str)
                or not _IDENTIFIER.fullmatch(requested_run_id)
                or len(requested_run_id) > 36):
            raise AgentRuntimeError("agent_run_id_invalid")
        run_id = requested_run_id
    else:
        run_id = _run_id(actor, key.strip())
    ref = "sha256:" + _digest(text.strip())
    # Real HTTP submission: fence -> Store/Account/Session locks -> live session
    # and action permission re-verified -> then the durable queue write. The
    # pre-reads above only located the scope; nothing is written from them.
    from backend.app.modules.assistant import principal as work_principal
    delegated = type(actor) is work_principal.WorkPrincipal
    if delegated:
        if (expected_runtime != runtime_repository.SDK_RUNTIME or tool_profile not in PI_ALL_PROFILES
                or actor.run_id != run_id or key != run_id or pi_runtime_record is None):
            raise AgentRuntimeError('assistant_work_submission_denied', 403)
        work_principal.check(session, actor, lock=True)
        from backend.app.modules.assistant.models import AssistantWork, AssistantTurn, AssistantTurnDispatch
        from backend.app.modules.tasks.models import Task
        if actor.kind=='turn':
            from backend.app.modules.assistant.turn_execution import binding
            work=session.get(AssistantTurn,actor.work_id)
            intent=session.get(AssistantTurnDispatch,actor.work_id)
            if work is None or intent is None:raise AgentRuntimeError('assistant_work_submission_denied',403)
            original_text=binding(session,work,intent)['text']
        else:
            work=session.get(AssistantWork,actor.work_id)
            task=session.get(Task,actor.work_id)
            original_text=task.description if task is not None else None
        if work is None or original_text is None or original_text.strip()!=text.strip():
            raise AgentRuntimeError('assistant_work_submission_denied',403)
        runtime_context[work_principal.CONTEXT_KEY] = work_principal.frozen(actor)
    else:
        _human_guard(session, actor)
    found = get_run(session, actor.organization_id, actor.store_id, run_id)
    if found is not None:
        try:
            runtime_repository.require_run_runtime(session, found, expected_runtime)
        except AgentPersistenceError as exc:
            raise AgentRuntimeError("agent_idempotency_runtime_conflict", 409) from exc
        if found.requester_id != actor.subject_id or found.input_ref != ref:
            raise AgentRuntimeError("agent_idempotency_conflict", 409)
        execution = runtime_repository.execution_for(
            session, actor.organization_id, actor.store_id, found.id)
        frozen = execution.frozen_context if execution is not None else {}
        if execution is not None:
            bound = work_principal.from_execution(session, execution, found)
            if delegated and bound != actor:
                raise AgentRuntimeError('agent_idempotency_runtime_conflict', 409)
        has_profile = isinstance(frozen, dict) and "pi_readonly_binding" in frozen
        if (has_profile != (tool_profile is not None)
                or (has_profile and (not isinstance(frozen["pi_readonly_binding"], dict)
                    or frozen["pi_readonly_binding"].get("profile") != tool_profile))):
            raise AgentRuntimeError("agent_idempotency_runtime_conflict", 409)
        has_record = isinstance(frozen, dict) and "pi_runtime_record" in frozen
        if (has_record != (pi_runtime_record is not None)
                or has_record and frozen["pi_runtime_record"] != pi_runtime_record):
            raise AgentRuntimeError("agent_idempotency_runtime_conflict", 409)
        if has_profile and (
                frozen['pi_readonly_binding'].get('category_source') != category_source
                or frozen['pi_readonly_binding'].get('category_products_contract') != category_products_contract
                or frozen['pi_readonly_binding'].get('research_source') != research_source
                or frozen['pi_readonly_binding'].get('tool_selection') != tool_selection):
            raise AgentRuntimeError('agent_idempotency_runtime_conflict',409)
        return read_run_receipt(session, actor, run_id, _public_trace=False)
    # The worker does not receive the employee token. The exact authenticated
    # account/session version is frozen in an independently scoped FK row.
    deadline = min(_now() + timedelta(minutes=10), actor.auth_expires_at)
    if deadline <= _now() + timedelta(seconds=2):
        raise AgentRuntimeError("agent_session_expiring", 401)
    # Freeze the REAL scope-fence epoch inside this same transaction, so the
    # queued row carries the authorization it was submitted under. A NULL value
    # is never written here: unknown authorization fails shut at claim time.
    try:
        authorization_epoch = (actor.work_epoch if delegated else current_authorization_epoch(
            session, actor.organization_id, actor.store_id))
    except AgentPersistenceError as exc:
        session.rollback()
        raise AgentRuntimeError("agent_execution_authorization_unknown", 409) from exc
    def freeze_binding(agent):
        return freeze_pi_binding(
            session, actor, agent, run_id=run_id, profile=tool_profile, category_source=category_source,
            category_products_contract=category_products_contract, research_source=research_source,
            sales_source=sales_source, tool_selection=tool_selection,
            **({'sales_contract':'3'} if sales_source is not None and sales_source.get('version')==3 else {}))

    agent = readonly_binding = None
    if tool_profile == PI_WORKBENCH_PROFILE and category_source is not None:
        # Only the authorized, frozen tool entries determine whether this
        # staged combination needs unsupported category readback.
        agent = _agent_actor(session, actor, deadline)
        readonly_binding = freeze_binding(agent)
        if any((entry['tool_key'], entry['version']) in GUANJIA_CATEGORY_TOOLS
               for entry in readonly_binding['tools']):
            raise AgentRuntimeError('agent_pi_category_workbench_unsupported', 409)
    conversation = conversation_service.get_or_create_conversation(session, actor)
    try:
        capacity = input_capacity(output_tokens=MAX_OUTPUT_TOKENS,
                                  remaining_input=MAX_INPUT_TOKENS)
    except ModelContextError as exc:
        raise AgentRuntimeError(exc.code, 503) from None
    context = conversation_service.context_for_agent(session, actor, budget_tokens=capacity)
    evidence = context["evidence"]
    message = conversation_service.append_message(session, actor,
        conversation_id=conversation.id, role="user", content=text)
    evidence = answer_receipts.merge_evidence(
        evidence, [answer_receipts.message_reference(message)])
    if agent is None:
        agent = _agent_actor(session, actor, deadline)
    descriptions = model_tool_descriptions(session, actor, agent)
    model = os.environ.get("AGENT_PROVIDER_MODEL", "unconfigured")
    if (not isinstance(model, str) or not model.isascii() or
            not 1 <= len(model) <= 200 or any(not "!" <= ch <= "~" for ch in model)):
        model = "unconfigured"
    # A validated Host v2 profile names the actual provider, independent of
    # mutable legacy environment defaults. Never accept client provider input.
    online_pi = (pi_runtime_record is not None
                 and pi_runtime_record['contract_version'] in (2, 3)
                 and tool_profile in PI_ALL_PROFILES)
    provider_key = 'opencode-go' if online_pi else 'chat-completions'
    if online_pi:
        model = 'deepseek-v4.1-flash'
    if tool_profile in PI_ALL_PROFILES:
        if readonly_binding is None:
            readonly_binding = freeze_binding(agent)
        choice, selection_reason = None, tool_profile
    else:
        choice, selection_reason = skill_service.choose_runtime_skill(
            session, actor, agent, text, provider_key="chat-completions",
            model=model, prompt_version=CURRENT_PROMPT_VERSION)
    if choice is not None:
        descriptions = choice["tool_descriptions"]
        evidence = [answer_receipts.message_reference(message)]
    try:
        run = create_run(session, RunCreate(
            id=run_id, organization_id=actor.organization_id, store_id=actor.store_id,
            requester_id=actor.subject_id, agent_key="shop-assistant", agent_version="1",
            provider_key=provider_key, model=model, prompt_version=CURRENT_PROMPT_VERSION,
            toolset_version=(pi_binding_digest(readonly_binding)
                             if readonly_binding is not None else
                             choice["binding"]["toolset_version"] if choice is not None
                             else toolset_digest(descriptions,
                                                 store_id=actor.store_id)),
            input_ref=ref, max_input_tokens=run_input_budget(model,
                model_calls=MAX_MODEL_CALLS, output_tokens=MAX_OUTPUT_TOKENS,
                explicit_limit=MAX_INPUT_TOKENS),
            max_output_tokens=MAX_OUTPUT_TOKENS, max_model_calls=MAX_MODEL_CALLS,
            max_tool_calls=MAX_TOOL_CALLS, max_steps=MAX_STEPS, deadline_at=deadline))
        if choice is not None:
            skill_service.freeze_run_skill(session, actor, run, choice)
        from . import public_trace_capture
        trace_context = ({public_trace_capture.KEY: public_trace_capture.new_namespace(run.max_tool_calls)}
                         if readonly_binding is not None else {})
        enqueue_worker_execution(session, run, actor=actor,
            authorization_epoch=authorization_epoch,
            origin_message_id=message.id,
            frozen_context={**runtime_context, **trace_context,
                            **({"pi_conversation_context": context} if readonly_binding is not None else {}),
                            **({"pi_readonly_binding": readonly_binding}
                               if readonly_binding is not None else {}), "evidence": evidence,
                            "conversation_id": conversation.id,
                            "summary_version": (0 if choice is not None
                                                else context["summary_version"]),
                            "context_message_ids": ([] if choice is not None else [
                                item.get("id") for item in context.get("messages", [])
                                if isinstance(item, dict) and "id" in item]),
                            "toolset_version": run.toolset_version})
        _audit(session, actor, run.id, "agent_run.queued", "worker_v2")
        if os.environ.get("AGENT_SKILL_RUNTIME_ENABLED") == "1":
            _audit(session, actor, run.id, "agent_skill.selection", (
                choice["binding"]["skill_key"] if choice is not None else selection_reason))
        session.commit()
    except Exception:
        session.rollback()
        raise
    result = read_run_receipt(session, actor, run.id, _public_trace=False)
    result["replayed"] = False
    return result


def read_pi_runtime_receipt(session: Session, actor: AuthenticatedSubject, run_id: str) -> dict:
    """Read Host facts only. No claim, source fallback or implied SDK execution.

    Uses the existing person/requester/scope gate and source revalidation. This
    endpoint requires a clean request Session; it never flushes caller writes.
    Separate ordinary receipt and runtime-receipt GETs are not an atomic pair.
    """
    from backend.app.modules.agents.schemas import PiRunRuntimeReceipt, PiOnlineRunRuntimeReceipt, PiStreamRunRuntimeReceipt
    if session.new or session.dirty or session.deleted:
        raise AgentRuntimeError("agent_receipt_session_not_clean", 409)
    with session.no_autoflush:
        _load_owned(session, actor, run_id)
        run, execution, _, _ = _scoped_run_control(session, actor, run_id)
        # Keep the original full dependency/current-permission recheck. No
        # source title, content, count or hidden source ID enters this DTO.
        if run.state == RunState.COMPLETED.value:
            answer_receipts.read_answer(session, actor, run)
        context = execution.frozen_context if execution is not None else None
        runtime = {"status": "not_recorded", "basis": None, "reason": "binding_absent",
                   "engine": None, "version": None, "adapter_contract": None,
                   "execution_proof": "not_proven"}
        if execution is not None:
            if type(context) is not dict:
                runtime.update(status="unsupported", reason="binding_unsupported")
            elif "runtime_binding" in context:
                try:
                    identity = runtime_repository.runtime_identity(context)
                    if identity != runtime_repository.SDK_RUNTIME:
                        raise AgentPersistenceError("agent_runtime_binding_invalid")
                except AgentPersistenceError:
                    runtime.update(status="unsupported", reason="binding_unsupported")
                else:
                    runtime.update(status="recorded", basis="run_runtime_binding", reason=None,
                                   **runtime_repository.sdk_runtime_binding())
        launch = {"status": "not_recorded", "reason": "launch_record_absent", "wire_protocol": None,
                  "tool_profile": None, "launch_profile": None, "source_manifest_sha256": None,
                  "toolset_version": None}
        entries = []
        contract_version = 1
        if execution is not None and type(context) is not dict:
            launch.update(status="unsupported", reason="launch_record_unsupported")
        elif type(context) is dict and "pi_runtime_record" in context:
            try:
                record = runtime_repository.validate_pi_runtime_record(context["pi_runtime_record"])
                contract_version = record["contract_version"]
                binding = context.get("pi_readonly_binding")
                if (runtime["status"] != "recorded" or type(binding) is not dict
                        or binding.get("profile") != runtime_repository.pi_record_tool_profile(record)
                        or (binding.get("organization_id"), binding.get("store_id"), binding.get("run_id"), binding.get("requester_id"))
                        != (run.organization_id, run.store_id, run.id, run.requester_id)
                        or type(binding.get("tools")) is not list
                        or run.toolset_version != "sha256:" + _digest(_canonical(binding))):
                    raise AgentPersistenceError("agent_pi_runtime_record_invalid")
                entries = binding["tools"]
                launch.update(status="recorded", reason=None,
                    wire_protocol=record["wire_protocol"], tool_profile=binding["profile"],
                    launch_profile=record["launch_profile"],
                    source_manifest_sha256=record["source_manifest_sha256"], toolset_version=run.toolset_version)
            except (AgentPersistenceError, TypeError, ValueError, UnicodeError, RecursionError):
                launch.update(status="unsupported", reason="launch_record_unsupported")
        steps = list_steps(session, run.organization_id, run.store_id, run.id)
        calls = []
        for receipt in runtime_repository.pi_tool_receipts(session, run.organization_id, run.store_id, run.id):
            if (receipt.contract_version != 1 or receipt.model_kind != "model" or receipt.tool_kind != "tool"
                    or launch["status"] != "recorded"
                    or not any(type(entry) is dict and
                        (entry.get("name"), entry.get("tool_key"), entry.get("version"))
                        == (receipt.pi_name, receipt.tool_key, receipt.tool_version) for entry in entries)):
                raise AgentRuntimeError("agent_runtime_receipt_inconsistent", 409)
            if receipt.tool_step_id is not None:
                call = get_tool_call(session, run.organization_id, run.store_id, run.id, receipt.tool_step_id)
                if (call is None or call.step_kind != "tool"
                        or call.idempotency_key != "pi:" + _digest(_canonical([run.id, receipt.native_call_id]))
                        or (call.tool_key, call.tool_version, call.input_digest)
                        != (receipt.tool_key, receipt.tool_version, receipt.input_digest)):
                    raise AgentRuntimeError("agent_runtime_receipt_inconsistent", 409)
            calls.append({"receipt_id": receipt.id, "model_step_id": receipt.model_step_id,
                "tool_step_id": receipt.tool_step_id, "native_call_id": receipt.native_call_id,
                "pi_name": receipt.pi_name, "tool_key": receipt.tool_key, "tool_version": receipt.tool_version,
                "toolset_version": receipt.toolset_version, "state": receipt.state,
                "output_ref": receipt.output_ref, "error_code": receipt.error_code})
        data = {"contract_version": contract_version, "run_id": run.id, "state": run.state, "runtime": runtime,
            "launch": launch, "steps": [{"step_id": s.id, "kind": s.kind, "state": s.state,
                "sequence_no": s.sequence_no, "output_ref": s.output_ref, "error_code": s.error_code} for s in steps],
            "native_calls": calls, "recovery": "A_stop_only", "result_replay": "unsupported",
            "step_citation_association": "unsupported"}
        try:
            dto = PiRunRuntimeReceipt
            if contract_version == 2:
                data['provider_calls'] = runtime_repository.pi_provider_receipt_facts(
                    session, run.organization_id, run.store_id, run.id)
                dto = PiOnlineRunRuntimeReceipt
            elif contract_version == 3:
                data['provider_calls'] = runtime_repository.pi_provider_receipt_facts(
                    session, run.organization_id, run.store_id, run.id)
                dto = PiStreamRunRuntimeReceipt
            result = dto.model_validate(data).model_dump(mode="json")
        except (ValidationError, AgentPersistenceError, ValueError, TypeError):
            raise AgentRuntimeError("agent_runtime_receipt_inconsistent", 409) from None
        if session.new or session.dirty or session.deleted:
            raise AgentRuntimeError("agent_receipt_session_not_clean", 409)
        return result


def read_pi_tool_result(session: Session, actor: AuthenticatedSubject, run_id: str, tool_step_id: str) -> dict:
    from .canonical_result_read import read_tool_result
    return read_tool_result(session, actor, run_id, tool_step_id)
