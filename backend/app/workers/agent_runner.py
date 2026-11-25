"""Bounded, deterministic recovery for the first local Agent task workflow.

No model or tool is called here. A committed task is identified by the same
idempotency key the registered task tool used; otherwise the result is unknown.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
from typing import Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from backend.app.modules.agents.models import AgentConfirmation, AgentRun, AgentStep, AgentToolCall
from backend.app.modules.agents import repository as runtime_repository
from backend.app.modules.agents.repository import (
    AgentPersistenceError, get_confirmation, get_run, list_steps,
    set_confirmation_state, transition_run, transition_step,
)
from backend.app.modules.agents.schemas import RunState, StepState, StepTransition
from backend.app.modules.agents.tool_registry import scoped_task_tool_key
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.models import Subject
from backend.app.modules.tasks.models import Task, TaskIdempotency


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _task_receipt(session: Session, run: AgentRun, confirmation: AgentConfirmation,
                  step: AgentStep) -> tuple[Task | None, dict | None, str | None]:
    call = session.scalar(select(AgentToolCall).where(
        AgentToolCall.organization_id == run.organization_id,
        AgentToolCall.store_id == run.store_id,
        AgentToolCall.run_id == run.id,
        AgentToolCall.step_id == confirmation.step_id,
    ))
    if call is None or call.tool_key != "tasks.suggest_review" or call.tool_version != "1":
        return None, None, None
    if call.input_digest != confirmation.input_digest or _digest(confirmation.arguments) != call.input_digest:
        return None, None, None
    if call.idempotency_key != f"{run.id}:{step.sequence_no}":
        return None, None, None
    arguments = confirmation.arguments
    agent_ids = session.scalars(select(Subject.id).where(
        Subject.organization_id == run.organization_id,
        Subject.subject_type == "agent",
    )).all()
    for agent_id in agent_ids:
        scoped_key = scoped_task_tool_key(run.requester_id, agent_id,
            call.tool_key, call.tool_version, call.idempotency_key)
        receipt = session.scalar(select(TaskIdempotency).where(
            TaskIdempotency.organization_id == run.organization_id,
            TaskIdempotency.store_id == run.store_id,
            TaskIdempotency.idempotency_key == scoped_key,
            TaskIdempotency.actor_id == agent_id,
            TaskIdempotency.operation == "create",
        ))
        if receipt is None:
            continue
        expected_payload = {"operation": "create", "actor_id": agent_id,
            "title": arguments.get("title"), "description": arguments.get("description"),
            "source": "agent", "proposal_ref": arguments.get("proposal_ref"),
            "reviewer_id": arguments.get("reviewer_id"), "reason": arguments.get("reason")}
        if receipt.payload_digest != _digest(expected_payload):
            continue
        task = session.scalar(select(Task).where(
            Task.organization_id == run.organization_id,
            Task.store_id == run.store_id,
            Task.id == receipt.task_id,
            Task.created_by == agent_id,
            Task.source == "agent",
        ))
        if (task is None or task.title != arguments.get("title") or
                task.description != arguments.get("description") or
                task.proposal_ref != arguments.get("proposal_ref") or
                task.reviewer_id != arguments.get("reviewer_id")):
            continue
        return task, {"task_id": task.id, "state": "suggested", "reviewer_id": task.reviewer_id}, agent_id
    return None, None, None


def _audit(session: Session, run: AgentRun, agent_id: str, worker_id: str,
           code: str) -> None:
    record_event(session, actor_id=agent_id, organization_id=run.organization_id,
        store_id=run.store_id, action="agent_run.recovered", target_type="agent_run",
        target_id=run.id, idempotency_key=f"agent_run:{run.id}:recovered",
        details={"code": code, "worker_id": worker_id})


def recover_one(*, session_factory: Callable[[], Session], worker_id: str,
                store_id: str,
                now: datetime | None = None) -> str:
    """Reconcile at most one expired run; return idle/recovered/blocked/conflict."""
    if not isinstance(worker_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", worker_id) is None:
        raise ValueError("invalid worker id")
    if not isinstance(store_id, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,36}", store_id) is None:
        raise ValueError("invalid store id")
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    with session_factory() as session:
        runtime_repository._reject_runtime_mutations(session)
        agent_id = os.environ.get("AGENT_SUBJECT_ID", "")
        subject = session.scalar(select(Subject).where(
            Subject.id == agent_id, Subject.subject_type == "agent",
            Subject.active.is_(True))) if agent_id else None
        if subject is None:
            return "blocked"
        run = session.scalar(select(AgentRun).outerjoin(AgentConfirmation,
            and_(AgentConfirmation.organization_id == AgentRun.organization_id,
                 AgentConfirmation.store_id == AgentRun.store_id,
                 AgentConfirmation.run_id == AgentRun.id)).where(
            AgentRun.organization_id == subject.organization_id,
            AgentRun.store_id == store_id, runtime_repository.legacy_run_clause(session), or_(
            and_(AgentRun.state == RunState.RUNNING.value, AgentRun.deadline_at <= current),
            and_(AgentRun.state == RunState.WAITING_FOR_HUMAN.value,
                 AgentConfirmation.state == "pending", AgentConfirmation.expires_at <= current),
        )).order_by(AgentRun.updated_at, AgentRun.id).limit(1))
        if run is None:
            return "idle"
        try:
            outcome = _recover(session, run, agent_id, worker_id, current)
            session.commit()
            return outcome
        except AgentPersistenceError:
            session.rollback()
            return "conflict"


def _recover(session: Session, run: AgentRun, agent_id: str,
             worker_id: str, current: datetime) -> str:
    runtime_repository.require_run_runtime(session, run)
    run = get_run(session, run.organization_id, run.store_id, run.id)
    confirmation = get_confirmation(session, run.organization_id, run.store_id, run.id)
    steps = list_steps(session, run.organization_id, run.store_id, run.id)
    if run.state == RunState.WAITING_FOR_HUMAN.value:
        if confirmation is None or confirmation.state != "pending" or _utc(confirmation.expires_at) > current:
            return "idle"
        step = next((item for item in steps if item.id == confirmation.step_id), None)
        if step is None or step.state != StepState.PENDING.value:
            return "conflict"
        transition_step(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, step_id=step.id,
            expected_run_version=run.version, expected_step_version=step.version,
            change=StepTransition(to_state=StepState.CANCELLED))
        run = get_run(session, run.organization_id, run.store_id, run.id)
        set_confirmation_state(session, confirmation, "expired")
        run = transition_run(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, expected_version=run.version,
            to_state=RunState.FAILED)
        _audit(session, run, agent_id, worker_id, "confirmation_expired")
        return "recovered"
    if run.state != RunState.RUNNING.value or _utc(run.deadline_at) > current:
        return "idle"
    active = next((item for item in steps if item.state == StepState.RUNNING.value), None)
    tool_step = next((item for item in steps if confirmation is not None and
                      item.id == confirmation.step_id), None)
    if (tool_step is not None and tool_step.kind == "tool" and
            tool_step.state in (StepState.RUNNING.value, StepState.SUCCEEDED.value) and
            confirmation is not None and confirmation.state == "executing"):
        task, receipt, original_agent_id = _task_receipt(session, run, confirmation, tool_step)
        if task is not None:
            change = StepTransition(to_state=StepState.SUCCEEDED,
                output_ref="sha256:" + _digest(receipt))
            code = "task_reconciled"
        else:
            change = StepTransition(to_state=StepState.UNKNOWN,
                error_code="agent_tool_result_unknown")
            code = "tool_result_unknown"
        if tool_step.state == StepState.RUNNING.value:
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=tool_step.id,
                expected_run_version=run.version, expected_step_version=tool_step.version,
                change=change)
            run = get_run(session, run.organization_id, run.store_id, run.id)
        set_confirmation_state(session, confirmation,
            "approved" if task is not None else "unknown",
            result_task_id=task.id if task is not None else None)
        state = RunState.COMPLETED if task is not None else RunState.FAILED
        if original_agent_id is not None:
            agent_id = original_agent_id
    else:
        if active is not None:
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=active.id,
                expected_run_version=run.version, expected_step_version=active.version,
                change=StepTransition(to_state=StepState.UNKNOWN,
                                      error_code="agent_interrupted"))
            run = get_run(session, run.organization_id, run.store_id, run.id)
        if confirmation is not None and confirmation.state == "pending":
            pending_step = next((item for item in steps if item.id == confirmation.step_id), None)
            if pending_step is not None and pending_step.state == StepState.PENDING.value:
                transition_step(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id, step_id=pending_step.id,
                    expected_run_version=run.version, expected_step_version=pending_step.version,
                    change=StepTransition(to_state=StepState.CANCELLED))
                run = get_run(session, run.organization_id, run.store_id, run.id)
            set_confirmation_state(session, confirmation, "expired")
        elif confirmation is not None and confirmation.state == "executing":
            set_confirmation_state(session, confirmation, "unknown")
        state = RunState.FAILED
        code = "run_interrupted"
    run = transition_run(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, expected_version=run.version,
        to_state=state)
    _audit(session, run, agent_id, worker_id, code)
    return "recovered"



# AGT-021 worker_v2 control plane. The original bounded AGT-006 inline recovery
# above is deliberately unchanged. All entrypoints below are OFF by default.
# Only a trusted, side-effect-free *synthetic* executor is accepted here until
# the AGT-020 security-fence and Provider/tool transaction adapters land.

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.agents import answer_receipts, conversation_service, skill_service
from backend.app.modules.agents.models import AgentExecution, AgentMessage
from backend.app.modules.agents.repository import (
    AgentClaim, _db_clock, claim_worker_execution, close_cancelled_claim,
    execution_for, get_run, heartbeat_worker_execution, record_answer,
    record_worker_dispatch, require_worker_claim, settle_worker_control,
    reap_worker_executions,
)
from backend.app.modules.agents.schemas import RunState
from backend.app.modules.identity.machine_service import authenticate_machine_token
from backend.app.modules.identity.service import authorize_scope


def _worker_identity(session: Session, token: str):
    principal = authenticate_machine_token(session, token)
    if principal.purpose != "worker":
        raise AgentPersistenceError("agent_machine_purpose_denied")
    return principal


def _bound_person(session: Session, row: AgentExecution, run: AgentRun) -> AuthenticatedSubject:
    """Reconstruct a human ONLY from the recorded real authenticated session."""
    if (row.organization_id, row.store_id) != (
            run.organization_id, run.store_id):
        raise AgentPersistenceError("agent_person_scope_denied")
    actor = AuthenticatedSubject(run.requester_id, run.organization_id, run.store_id,
                                 row.authorization_expires_at,
                                 account_id=row.account_id, session_id=row.session_id,
                                 account_version=row.account_version)
    now = _db_clock(session)
    validate_actor_session(session, actor, now=now)
    authorize_scope(session, actor, organization_id=row.organization_id,
                    store_id=row.store_id, permission="agents.run", now=now)
    return actor


def _claim_guarded(session: Session, token: str, claim: AgentClaim,
                   *, allow_cancel: bool = False) -> tuple[AgentExecution, AgentRun, AuthenticatedSubject]:
    principal = _worker_identity(session, token)
    if (principal.machine_id, principal.organization_id, principal.store_id) != (
            claim.machine_id, claim.organization_id, claim.store_id):
        raise AgentPersistenceError("agent_machine_scope_denied")
    row = require_worker_claim(session, claim, permit_cancel=allow_cancel)
    run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
    if run is None:
        raise AgentPersistenceError("agent_not_found")
    actor = _bound_person(session, row, run)
    pin = skill_service.runtime_pin(session, run)
    if pin is not None:
        # The only live Agent identity is the existing trusted store-scoped
        # subject. Never reconstruct a broader Agent from the skill version.
        from backend.app.modules.agents.service import _agent_actor, AgentRuntimeError
        try:
            agent = _agent_actor(session, actor,
                min(_utc(run.deadline_at), _utc(row.authorization_expires_at)))
            skill_service.verify_runtime_binding(session, actor, agent, run)
        except (AgentRuntimeError, skill_service.SkillReleaseError) as exc:
            raise AgentPersistenceError("skill_worker_binding_revoked") from exc
    return row, run, actor


def tick_agent_worker(*, session_factory: Callable[[], Session], machine_token: str,
                      executor: Callable[[str], str] | None = None,
                      lease_seconds: int = 30) -> str:
    """Process <=1 scoped control record; return idle/disabled/cancelled/finished/unknown.

    An injected executor can be exercised against a synthetic SQLite snapshot,
    but the *production* provider/tool dispatch is intentionally not enabled:
    the AGT-020 security-fence must cover every existing auth mutation first.
    Do not supply a side-effecting executor. No employee token is passed to it.
    """
    if os.environ.get("AGENT_WORKER_DISPATCH_ENABLED") != "1" or executor is None:
        return "disabled"
    with session_factory() as session:
        principal = _worker_identity(session, machine_token)
        # Reconcile unknown/expired control rows BEFORE choosing fresh work.
        # No uncertain dispatch is ever retried.
        reap_worker_executions(session, organization_id=principal.organization_id,
                               store_id=principal.store_id)
        claim = claim_worker_execution(session, organization_id=principal.organization_id,
                                       store_id=principal.store_id, machine_id=principal.machine_id,
                                       lease_seconds=lease_seconds)
        session.commit()
    if claim is None:
        return "idle"
    try:
        with session_factory() as session:
            row, run, actor = _claim_guarded(session, machine_token, claim, allow_cancel=True)
            if row.cancel_requested_at is not None:
                result = close_cancelled_claim(session, claim)
                session.commit()
                return result
            if run.state != RunState.CREATED.value or row.dispatch_state != "not_dispatched":
                raise AgentPersistenceError("agent_worker_invalid_state")
            origin = session.scalar(select(AgentMessage).where(
                AgentMessage.id == row.origin_message_id,
                AgentMessage.organization_id == row.organization_id,
                AgentMessage.store_id == row.store_id,
                AgentMessage.conversation_id == row.frozen_context["conversation_id"],
                AgentMessage.role == "user", AgentMessage.state == "active",
            ).execution_options(populate_existing=True))
            if origin is None or sha256(origin.content.encode("utf-8")).hexdigest() != row.input_digest:
                raise AgentPersistenceError("agent_worker_origin_changed")
            evidence = row.frozen_context.get("evidence", [])
            valid, _ = answer_receipts.validate_evidence(session, actor, evidence)
            if not valid:
                raise AgentPersistenceError("agent_worker_source_changed")
            text = origin.content
            pin = skill_service.runtime_pin(session, run)
            if pin is not None:
                # This callback is SYNTHETIC only; send no unverified skill body
                # and no extra tool/permission. The host-authored instruction is
                # digest-matched during _claim_guarded above.
                text = json.dumps({
                    "user_request": text,
                    "approved_skill": skill_service.host_runtime_spec(pin.skill_key),
                    "frozen_tools": pin.allowed_tools,
                    "release_tag": pin.release_tag,
                }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            # Persist the external/unknown boundary *before* invoking code which
            # might dispatch. If execution dies after here, never auto-redeliver.
            record_worker_dispatch(session, claim)
            session.commit()
        try:
            response = executor(text)
            if not isinstance(response, str) or not 1 <= len(response.strip()) <= 1000:
                raise ValueError("untrusted worker response")
        except BaseException:
            with session_factory() as session:
                _mark_worker_unknown(session, machine_token, claim)
                session.commit()
            return "unknown"
        with session_factory() as session:
            row, run, actor = _claim_guarded(session, machine_token, claim)
            evidence = row.frozen_context.get("evidence", [])
            valid, _ = answer_receipts.validate_evidence(session, actor, evidence)
            if not valid or run.state != RunState.CREATED.value:
                raise AgentPersistenceError("agent_worker_source_changed")
            # Same transaction holds all staged answer, Run, control and audit
            # writes. A late cancellation/epoch CAS forces rollback, not success.
            from backend.app.modules.agents.repository import transition_run
            run = transition_run(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, expected_version=run.version,
                to_state=RunState.RUNNING)
            run = transition_run(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, expected_version=run.version,
                to_state=RunState.COMPLETED)
            message = conversation_service.append_message(session, actor,
                conversation_id=row.frozen_context["conversation_id"],
                role="assistant", content=response.strip(), source_ref=f"run:{run.id}")
            record_answer(session, run=run, message=message, evidence=evidence)
            settle_worker_control(session, claim, outcome="finished")
            record_event(session, actor_id=actor.subject_id,
                organization_id=run.organization_id, store_id=run.store_id,
                action="agent_run.worker_finished", target_type="agent_run",
                target_id=run.id, idempotency_key=f"agent_worker_finished:{run.id}",
                details={"epoch": claim.epoch, "model_executed": False})
            session.commit()
            return "finished"
    except BaseException:
        # Crucially: never try to re-execute a dispatched/unknown request.
        with session_factory() as session:
            try:
                _mark_worker_unknown(session, machine_token, claim)
                session.commit()
            except Exception:
                session.rollback()
        return "unknown"


def _mark_worker_unknown(session: Session, token: str, claim: AgentClaim) -> None:
    from backend.app.modules.agents.repository import _cas_execution, _db_clock, transition_run
    try:
        row = require_worker_claim(session, claim, permit_cancel=True)
    except AgentPersistenceError:
        return  # late/lost lease: stale worker must not write diagnostics
    principal = _worker_identity(session, token)
    if (principal.organization_id, principal.store_id, principal.machine_id) != (
            claim.organization_id, claim.store_id, claim.machine_id):
        return
    run = get_run(session, claim.organization_id, claim.store_id, claim.run_id)
    _cas_execution(session, row, values={
        "phase": "unknown", "owner_machine_id": None, "lease_until": None,
        "dispatch_state": "unknown", "effect_status": "external_unknown",
        "cancel_resolution": "unknown" if row.cancel_requested_at is not None
                             else row.cancel_resolution,
    }, expected_epoch=claim.epoch)
    if run is not None and run.state == RunState.CREATED.value:
        transition_run(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, expected_version=run.version,
            to_state=RunState.FAILED, now=_db_clock(session))


def heartbeat_agent_worker(*, session_factory: Callable[[], Session], token: str,
                           claim: AgentClaim, lease_seconds: int = 30) -> None:
    """Machine revocation and epoch are rechecked during each heartbeat."""
    with session_factory() as session:
        _claim_guarded(session, token, claim)
        heartbeat_worker_execution(session, claim, lease_seconds=lease_seconds)
        session.commit()


async def tick_real_agent_worker(*, session_factory: Callable[[], Session],
                                 machine_token: str, provider_factory=None,
                                 lease_seconds: int = 30) -> str:
    """Explicit B2d entry: one real Provider loop for a live worker claim.

    The synthetic ``tick_agent_worker`` callback stays the default local path;
    this entry is the only bridge to ``backend.app.workers.agent_executor`` and
    performs no work of its own.
    """
    from backend.app.workers.agent_executor import (
        tick_real_agent_worker as run_real_worker,
    )
    return await run_real_worker(session_factory=session_factory,
                                 machine_token=machine_token,
                                 provider_factory=provider_factory,
                                 lease_seconds=lease_seconds)
