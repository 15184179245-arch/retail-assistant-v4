"""AGT-026 B2d: one live worker claim drives a real, bounded Agent loop.

The executor is deliberately thin around existing services:

* every state write happens in a short ``guard_execution`` transaction that
  re-checks the live machine, the bound person/agent, the frozen sources, the
  skill pin and the lease with the database clock;
* a dispatch intent (plus its cost reservation) is committed *before* any
  Provider or tool call leaves the process, so nothing is ever replayed;
* provider/tool results are never persisted verbatim; only stable refs, the
  final answer, receipts and audits are.

This module never commits outside the transaction it owns, never calls
``service._start_step``/``_finish_step``/``_fail_run`` (they commit), and never
runs a fixed write tool outside the one-shot approval consumption transaction.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import re
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.model_provider import CallBudget, ProviderRequest, ProviderResponse
from backend.app.core.model_context import ModelContextError, fit_prompt, input_capacity
from backend.app.modules.agents.provider_adapter import ProviderError
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import (answer_receipts, conversation_service,
                                        cost_ledger, repository, service, skill_service,
                                        tool_registry as capability_registry)
from backend.app.workers.agent_guard import (ExecutionContext, guard_execution,
                                            guard_recovery)
from backend.app.modules.agents.models import (AgentConfirmation, AgentMessage, AgentStep,
                                              AgentToolCall, CostLedger)
from backend.app.modules.agents.repository import (AgentClaim, AgentPersistenceError,
                                                   close_cancelled_claim, get_confirmation,
                                                   get_run, list_steps, record_answer,
                                                   settle_worker_attempt, settle_worker_control,
                                                   consume_approval_grant, create_confirmation,
                                                   record_worker_dispatch, heartbeat_worker_execution,
                                                   transition_run, transition_step, append_step)
from backend.app.modules.agents.schemas import (RunState, StepCreate, StepKind, StepState,
                                               StepTransition, ToolCallCreate)
from backend.app.modules.agents.tool_registry import (TOOLS, ToolRegistryError, invoke_tool, legacy_code_tool,
                                                     model_tool_descriptions, resolve_tool,
                                                     verify_toolset_digest)
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity import machine_service
from backend.app.modules.identity.command_guard import (ScopeSecurityFenceError,
                                                       lock_scope_security_fence)
from backend.app.application import commit_guard
from backend.app.modules.identity.service import authorize_scope

EXECUTE_ACTION = "worker.agent.execute"
RECOVER_ACTION = "worker.agent.recover"
RUNTIME_ENV = "AGENT_WORKER_RUNTIME_ENABLED"
DISPATCH_ENV = "AGENT_WORKER_DISPATCH_ENABLED"
_FROZEN_RUN_IDENTITY = ("chat-completions", service.CURRENT_PROMPT_VERSION, "shop-assistant", "1")


class AgentExecutorError(RuntimeError):
    """Stable, content-free executor error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise AgentExecutorError(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _enabled() -> bool:
    return os.environ.get(RUNTIME_ENV) == "1" and os.environ.get(DISPATCH_ENV) == "1"


def _audit(session: Session, ctx: ExecutionContext, action: str, code: str,
           *, details: dict | None = None) -> None:
    record_event(session, actor_id=ctx.person.subject_id,
                 organization_id=ctx.run.organization_id,
                 store_id=ctx.run.store_id, action=action,
                 target_type="agent_run", target_id=ctx.run.id,
                 idempotency_key=f"agent_worker:{ctx.run.id}:{action}:{code}",
                 details=details or {"machine_id": ctx.principal.machine_id})


def _machine_actor(principal) -> AuthenticatedSubject:
    return AuthenticatedSubject(subject_id=principal.subject_id,
                                organization_id=principal.organization_id,
                                store_id=principal.store_id,
                                auth_expires_at=principal.auth_expires_at)


def _claim(*, session_factory, machine_token: str, lease_seconds: int,
           expected_runtime: tuple = repository.LEGACY_RUNTIME,
           run_id: str | None = None) -> AgentClaim | None:
    """Exclusive entry fence, then a fresh DB-time re-check, then one claim."""
    with session_factory() as session:
        principal = machine_service.authenticate_machine_token(session, machine_token)
        lock_scope_security_fence(session, principal.organization_id, principal.store_id,
                                  exclusive=True)
        session.expire_all()
        now = repository._db_clock(session)
        principal = machine_service.authenticate_machine_token(session, machine_token, now=now)
        for action in (EXECUTE_ACTION, RECOVER_ACTION):
            machine_service.authorize_machine_action(
                session, principal, organization_id=principal.organization_id,
                store_id=principal.store_id, action=action, now=now)
        claim = repository.claim_worker_execution(
            session, organization_id=principal.organization_id,
            store_id=principal.store_id, machine_id=principal.machine_id,
            lease_seconds=lease_seconds, expected_runtime=expected_runtime, run_id=run_id)
        session.commit()
    return claim


def _scoped_messages(session: Session, run_id: str, org: str, store: str) -> list[AgentStep]:
    return list_steps(session, org, store, run_id)


def _origin_text(session: Session, execution, run) -> str:
    frozen = execution.frozen_context if isinstance(execution.frozen_context, dict) else {}
    conversation_id = frozen.get("conversation_id")
    message = session.scalar(select(AgentMessage).where(
        AgentMessage.id == execution.origin_message_id,
        AgentMessage.organization_id == run.organization_id,
        AgentMessage.store_id == run.store_id,
        AgentMessage.conversation_id == conversation_id,
        AgentMessage.role == "user", AgentMessage.state == "active",
    ).execution_options(populate_existing=True))
    if message is None or sha256(message.content.encode("utf-8")).hexdigest() != execution.input_digest:
        _fail("agent_worker_origin_changed")
    return message.content


def _history_context(session: Session, execution, run, origin_id: str) -> dict:
    """Rebuild only the frozen refs; never join newer conversation messages."""
    frozen = execution.frozen_context if isinstance(execution.frozen_context, dict) else {}
    conversation_id = frozen.get("conversation_id")
    evidence = frozen.get("evidence")
    context_ids = frozen.get("context_message_ids")
    if (not isinstance(conversation_id, str) or not conversation_id
            or not isinstance(evidence, list) or not evidence
            or len(evidence) > answer_receipts.MAX_EVIDENCE
            or not isinstance(context_ids, list)
            or len(context_ids) > answer_receipts.MAX_CONTEXT_EVIDENCE):
        _fail("agent_worker_source_changed")
    refs: dict[str, int] = {}
    for item in evidence:
        if not isinstance(item, dict):
            _fail("agent_worker_source_changed")
        if item.get("kind") != "message":
            continue
        message_id, version = item.get("id"), item.get("version")
        if (not isinstance(message_id, str) or not message_id
                or type(version) is not int or version <= 0
                or item.get("conversation_id") != conversation_id
                or (message_id in refs and refs[message_id] != version)):
            _fail("agent_worker_source_changed")
        refs[message_id] = version
    # The ID list is an index into versioned evidence, never an unversioned
    # alternate source. Keep this invariant even without the outer guard.
    if any(not isinstance(message_id, str) or message_id not in refs
           for message_id in context_ids):
        _fail("agent_worker_source_changed")
    rows: list[AgentMessage] = []
    for message_id, version in refs.items():
        if message_id == origin_id:
            continue
        message = session.scalar(select(AgentMessage).where(
            AgentMessage.id == message_id,
            AgentMessage.organization_id == run.organization_id,
            AgentMessage.store_id == run.store_id,
            AgentMessage.conversation_id == conversation_id,
            AgentMessage.state == "active",
        ).execution_options(populate_existing=True))
        if message is None or message.version != version:
            _fail("agent_worker_source_changed")
        rows.append(message)
    rows.sort(key=lambda item: (item.created_at, item.id))
    messages = [{"role": item.role, "content": item.content} for item in rows]
    return {"messages": messages, "summary_version": frozen.get("summary_version"), "summary": None}


def _tool_views(session: Session, ctx: ExecutionContext, text: str) -> tuple[list[dict], dict | None]:
    """Tool descriptions must round-trip the Run's frozen binding exactly.

    A skill Run never re-selects a release: its pin already froze the host
    recipe, role plan digest and tool contract. Normal Runs keep the legacy
    toolset SHA check.
    """
    frozen = ctx.execution.frozen_context
    pi_record = (frozen.get('pi_runtime_record')
                 if _pi_binding_present(ctx)
                 and repository.runtime_identity(frozen) == repository.SDK_RUNTIME else None)
    if not service.supported_prompt(ctx.run, pi_runtime_record=pi_record):
        _fail("agent_prompt_version_unsupported")
    pin = skill_service.runtime_pin(session, ctx.run)
    if _pi_binding_present(ctx):
        if (pin is not None or repository.runtime_identity(ctx.execution.frozen_context)
                != repository.SDK_RUNTIME):
            _fail("agent_pi_profile_invalid")
        try:
            return capability_registry.verify_pi_binding(
                session, ctx.person, ctx.agent, run_id=ctx.run.id,
                binding=ctx.execution.frozen_context["pi_readonly_binding"],
                expected_digest=ctx.run.toolset_version, now=ctx.now), None
        except ToolRegistryError as exc:
            _fail(exc.code)
    approved = None
    if pin is not None:
        from backend.app.modules.agents.routing import (RoleRoutingError,
                                                       select_role)
        host = skill_service.host_runtime_spec(pin.skill_key)
        entry = skill_service._RUNTIME_DOMAINS.get(pin.skill_key)
        if entry is None:
            _fail("agent_worker_toolset_changed")
        try:
            plan = select_role(session, requester=ctx.person, agent=ctx.agent,
                               role_key=pin.role_key, specialty=pin.specialty,
                               task_kind=entry["task_kind"])
        except (RoleRoutingError, HTTPException):
            _fail("agent_worker_toolset_changed")
        frozen_tool = (host["tool_key"], host["tool_version"])
        if (pin.allowed_tools != [{"key": frozen_tool[0], "version": frozen_tool[1]}]
                or tuple(plan.allowed_tools) != (frozen_tool,)
                or "sha256:" + pin.tool_contract_sha256
                != "sha256:" + _digest(list(plan.tool_descriptions))
                or pin.role_spec_sha256 != plan.config_digest
                or pin.spec_sha256 != skill_service.host_runtime_digest(pin.skill_key)
                or (pin.role_key, pin.specialty) != ("specialist", plan.specialty)):
            _fail("agent_worker_toolset_changed")
        descriptions = list(plan.tool_descriptions)
        approved = host
    else:
        descriptions = model_tool_descriptions(session, ctx.person, ctx.agent)
        try:
            verify_toolset_digest(descriptions, ctx.run.toolset_version,
                                  store_id=ctx.run.store_id)
        except ToolRegistryError:
            _fail("agent_worker_toolset_changed")
    return descriptions, approved


def _remaining_budget(run, steps: list[AgentStep], *, phase: str = "model") -> dict:
    """Judge the next protocol step against the run's own frozen budgets.

    A model call needs token room and a step slot; a new tool step also needs a
    free tool slot and enough remaining steps for the model turn that follows
    (``phase="tool"``). Exhausting the last allowed tool never blocks a final
    answer that the protocol still permits.
    """
    models = [s for s in steps if s.kind == "model"]
    tools = [s for s in steps if s.kind == "tool"]
    used_in = sum((s.input_tokens or 0) for s in models)
    used_out = sum((s.output_tokens or 0) for s in models)
    remaining_in = run.max_input_tokens - used_in
    remaining_out = run.max_output_tokens - used_out
    if phase == "tool":
        if (len(tools) >= run.max_tool_calls
                or len(steps) + 2 > run.max_steps):
            _fail("agent_budget_exceeded")
    elif (len(models) >= run.max_model_calls or len(steps) + 1 > run.max_steps
          or remaining_in <= 0 or remaining_out <= 0):
        _fail("agent_budget_exceeded")
    return {"input": remaining_in, "output": remaining_out}


def _response_is_valid(response: object) -> ProviderResponse:
    if not isinstance(response, ProviderResponse):
        _fail("agent_provider_invalid")
    return response


def _close_with_usage(session_factory, machine_token: str, claim: AgentClaim,
                     *, step_id: str, run_version: int, step_version: int,
                     input_tokens: int, output_tokens: int, code: str) -> str | None:
    """Fail a provider error while atomically keeping its real known usage.

    A reconciliation-required or already-unknown ledger is NEVER rolled back
    into a fake refund: the failed Run keeps the truthful usage fact and the
    control row records that the cost is unknown.
    """
    try:
        with session_factory() as session:
            ctx = guard_execution(session, machine_token, claim, step_id=step_id,
                                  expected_run_version=run_version,
                                  expected_step_version=step_version)
            run, step = ctx.run, ctx.step
            ledger = cost_ledger.settle_cost(session, ctx.person, run, step,
                input_tokens=input_tokens, output_tokens=output_tokens,
                now=ctx.now, commit=False)
            if ledger is None:
                return None
            usage_unknown = ledger.status in ("reconciliation_required", "unknown")
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version, expected_step_version=step.version,
                change=StepTransition(to_state=StepState.FAILED,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    error_code=code), now=ctx.now)
            run = get_run(session, run.organization_id, run.store_id, run.id)
            transition_run(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id,
                expected_version=run.version, to_state=RunState.FAILED, now=ctx.now)
            _audit(session, ctx, "agent_run.worker_failed", code)
            settle_worker_control(session, claim,
                outcome="unknown" if usage_unknown else "finished")
            session.commit()
            return "unknown" if usage_unknown else "failed"
    except (AgentExecutorError, AgentPersistenceError, HTTPException,
            ScopeSecurityFenceError, cost_ledger.CostLedgerError):
        return None


async def _call_provider(*, provider, request: ProviderRequest, session_factory,
                         machine_token: str, claim: AgentClaim, lease_seconds: int) -> ProviderResponse:
    """Await one provider call with a heartbeat and a hard total deadline.

    The deadline is derived from the database clock (never the host clock) and
    never later than the request deadline. Every exit path cancels and joins
    both the provider task and the heartbeat so no real network task outlives
    the call, including an outer coroutine cancellation.
    """
    with session_factory() as session:
        now = repository._db_clock(session)
    remaining = (request.deadline_at - now).total_seconds()
    timeout = max(0.0, min(remaining, float(service.MAX_SECONDS)))
    if timeout <= 0.0:
        _fail("agent_worker_deadline_exceeded")

    task: asyncio.Task | None = None
    beat: asyncio.Task | None = None
    lost: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def pulse() -> None:
        interval = max(lease_seconds / 3.0, 0.05)
        while True:
            await asyncio.sleep(interval)
            if task is None or task.done():
                return
            try:
                with session_factory() as session:
                    guard_execution(session, machine_token, claim)
                    heartbeat_worker_execution(session, claim, lease_seconds=lease_seconds)
                    session.commit()
            except Exception:  # revocation or lost lease: stop waiting immediately
                if not lost.done():
                    lost.set_result(True)
                return

    task = asyncio.create_task(provider.complete(request))
    beat = asyncio.create_task(pulse())
    try:
        done, _ = await asyncio.wait({task, beat}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            _fail("agent_worker_provider_timeout")
        if lost.done() and lost.result():
            _fail("agent_worker_lease_lost")
        if task.done():
            return _response_is_valid(task.result())
        _fail("agent_worker_lease_lost")
    finally:
        for pending in (task, beat):
            if pending is not None and not pending.done():
                pending.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*(t for t in (task, beat) if t is not None),
                                 return_exceptions=True)


def _append_frozen_evidence(session: Session, ctx: ExecutionContext,
                            refs: list[dict], *, supersede_memory=False) -> None:
    """Durably widen the frozen context with real read-tool source refs only."""
    frozen = dict(ctx.execution.frozen_context or {})
    current = list(frozen.get("evidence") or [])
    widened = (answer_receipts.advance_memory_evidence(current, refs) if supersede_memory
               else answer_receipts.merge_evidence(current, refs))
    if widened == current:
        return
    frozen["evidence"] = widened
    repository._cas_execution(session, ctx.execution,
                              values={"frozen_context": frozen})


def _close_known_failure(session: Session, ctx: ExecutionContext, claim: AgentClaim,
                         code: str, *, step: AgentStep | None = None,
                         ledger_step: AgentStep | None = None,
                         input_tokens: int | None = None,
                         output_tokens: int | None = None) -> None:
    """Close a definite local failure inside the caller's transaction."""
    run = ctx.run
    if step is not None and step.state == StepState.RUNNING.value:
        transition_step(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, step_id=step.id,
            expected_run_version=run.version, expected_step_version=step.version,
            change=StepTransition(to_state=StepState.FAILED, error_code=code),
            now=ctx.now)
        run = get_run(session, run.organization_id, run.store_id, run.id)
    if ledger_step is not None:
        cost_ledger.settle_cost(session, ctx.person, run, ledger_step,
            input_tokens=input_tokens, output_tokens=output_tokens,
            now=ctx.now, commit=False)
    run = get_run(session, run.organization_id, run.store_id, run.id)
    if run.state == RunState.RUNNING.value:
        transition_run(session, organization_id=run.organization_id,
            store_id=run.store_id, run_id=run.id, expected_version=run.version,
            to_state=RunState.FAILED, now=ctx.now)
    _audit(session, ctx, "agent_run.worker_failed", code)
    settle_worker_control(session, claim, outcome="finished")


def _dispatch_state_of(session_factory, claim: AgentClaim) -> str | None:
    """The durable dispatch fact decides release vs unknown, never an error code."""
    try:
        with session_factory() as session:
            row = repository.execution_for(session, claim.organization_id,
                                           claim.store_id, claim.run_id)
            return None if row is None else row.dispatch_state
    except Exception:
        return None


def _recovery_close(session_factory, machine_token: str, claim: AgentClaim,
                    *, code: str, definite: bool, settled_ok: bool = False,
                    expected_runtime: tuple = repository.LEGACY_RUNTIME) -> str:
    """Abort exactly this old attempt inside the shared-fence recovery domain.

    The recovery guard re-locks ScopeSecurityFence -> Store -> execution -> Run
    with the real machine credential and the DB clock. Every write requires the
    locked row to still be the claim's own attempt (run/org/store/machine/epoch).
    A dispatched attempt keeps its cost facts as unknown; only a provably
    never-dispatched reservation may be released.
    """
    repository.validate_runtime_identity(expected_runtime)
    if claim.runtime_identity != expected_runtime:
        return "preserved"
    try:
        with session_factory() as session:
            try:
                ctx = guard_recovery(session, machine_token,
                                     organization_id=claim.organization_id,
                                     store_id=claim.store_id,
                                     run_id=claim.run_id, expected_runtime=expected_runtime)
            except (HTTPException, ScopeSecurityFenceError, AgentPersistenceError):
                session.rollback()
                return "preserved"
            row, run = ctx.execution, ctx.run
            same_attempt = (row.mode == "worker_v2"
                            and row.phase in ("claimed", "unknown", "cancelled")
                            and row.run_id == claim.run_id
                            and row.organization_id == claim.organization_id
                            and row.store_id == claim.store_id)
            exact = (same_attempt and row.phase == "claimed"
                     and ctx.principal.machine_id == claim.machine_id
                     and row.owner_machine_id == claim.machine_id
                     and row.epoch == claim.epoch)
            # A cancel may have advanced the attempt epoch before this abort.
            # Only the old attempt's own cost facts may still be closed; no
            # owner/epoch/terminal receipt is rewritten in that case.
            cancelled_attempt = (same_attempt and not exact
                                 and row.cancel_requested_at is not None)
            if not exact and not cancelled_attempt:
                session.rollback()
                return "preserved"
            now = ctx.now
            actor = _machine_actor(ctx.principal)
            steps = list_steps(session, run.organization_id, run.store_id, run.id)
            release = (definite and exact
                       and row.dispatch_state == "not_dispatched")
            reserved = [step for step in steps if step.kind == "model"
                        and any(ledger.status == "reserved" for ledger in
                                session.scalars(select(CostLedger).where(
                                    CostLedger.organization_id == run.organization_id,
                                    CostLedger.store_id == run.store_id,
                                    CostLedger.run_id == run.id,
                                    CostLedger.step_id == step.id)))]
            in_flight = reserved or any(step.state in (StepState.RUNNING.value,
                                                       StepState.PENDING.value)
                                        for step in steps)
            local_failure = (definite and exact and (
                row.dispatch_state == "not_dispatched"
                or (settled_ok and row.dispatch_state == "settled" and not in_flight)))
            for step in steps:
                if step.state == StepState.PENDING.value:
                    transition_step(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id, step_id=step.id,
                        expected_run_version=run.version,
                        expected_step_version=step.version,
                        change=StepTransition(to_state=StepState.CANCELLED), now=now)
                    run = get_run(session, run.organization_id, run.store_id, run.id)
                elif step.state == StepState.RUNNING.value:
                    transition_step(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id, step_id=step.id,
                        expected_run_version=run.version,
                        expected_step_version=step.version,
                        change=StepTransition(
                            to_state=StepState.FAILED if local_failure else StepState.UNKNOWN,
                            error_code=code), now=now)
                    run = get_run(session, run.organization_id, run.store_id, run.id)
                if step.kind != "model":
                    continue
                ledger = session.scalar(select(CostLedger).where(
                    CostLedger.organization_id == run.organization_id,
                    CostLedger.store_id == run.store_id,
                    CostLedger.run_id == run.id,
                    CostLedger.step_id == step.id))
                if ledger is None or ledger.status != "reserved":
                    continue
                if release:
                    cost_ledger.release_cost(session, actor, run, step,
                        reason="agent_worker_pre_dispatch_failed", now=now,
                        commit=False)
                else:
                    cost_ledger.mark_cost_unknown(session, actor, run, step,
                        reason="agent_worker_unknown", now=now, commit=False)
            if run.state in (RunState.RUNNING.value, RunState.CREATED.value):
                transition_run(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id,
                    expected_version=run.version, to_state=RunState.FAILED, now=now)
            if exact:
                repository._cas_execution(session, row, values={
                    "phase": "finished" if local_failure else "unknown",
                    "owner_machine_id": None, "lease_until": None,
                    "dispatch_state": ("settled" if row.dispatch_state == "settled"
                                       or release else "unknown"),
                    "effect_status": "none" if local_failure else "external_unknown",
                    "epoch": row.epoch + 1,
                }, expected_epoch=claim.epoch)
                repository._close_executing_confirmation(
                    session, run.id, dispatched=not local_failure, now=now)
            record_event(session, actor_id=ctx.principal.subject_id,
                organization_id=run.organization_id, store_id=run.store_id,
                action=("agent_run.worker_failed" if local_failure
                        else "agent_run.worker_unknown"),
                target_type="agent_run", target_id=run.id,
                idempotency_key=f"agent_worker_abort:{run.id}:{claim.epoch}",
                details={"machine_id": ctx.principal.machine_id, "code": code})
            session.commit()
            return "failed" if local_failure else "unknown"
    except (AgentPersistenceError, HTTPException, ScopeSecurityFenceError):
        return "preserved"
    except cost_ledger.CostLedgerError:
        # A managed cost write failed inside the guarded transaction: roll the
        # whole close back so a later recovery retries the same durable facts.
        return "preserved"


def _sdk_exception_detail(error: BaseException) -> dict:
    """Host-local diagnostic metadata; never exception text, locals or parameters."""
    seen = set()
    def describe(exc, depth=0):
        if id(exc) in seen or depth >= 8:
            return {"truncated": True}
        seen.add(id(exc))
        frames, tb = [], exc.__traceback__
        while tb is not None:
            frames.append({"file": tb.tb_frame.f_code.co_filename,
                           "function": tb.tb_frame.f_code.co_name, "line": tb.tb_lineno})
            tb = tb.tb_next
        value = {"type": type(exc).__name__, "module": type(exc).__module__,
                 "frames": frames, "suppress_context": bool(exc.__suppress_context__)}
        if exc.__cause__ is not None:
            value["cause"] = describe(exc.__cause__, depth + 1)
        if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
            value["context"] = describe(exc.__context__, depth + 1)
        return value
    return describe(error)


def _close_sdk_bound_attempt(session_factory, machine_token: str, claim: AgentClaim,
                             *, code: str, errors: list | None = None) -> str:
    """Close only this Host-issued SDK claim; never grant execution or reveal content.

    Classify locked durable facts, not a callback error or in-memory pending flag.
    All failures are unresolved. The caller must already close its memory binding.
    Legacy recovery and its cancellation exception deliberately remain separate.
    """
    from backend.app.modules.agents.models import AgentAnswer
    from backend.app.modules.audit.models import AuditEvent
    if not isinstance(claim, AgentClaim) or claim.runtime_identity != repository.SDK_RUNTIME:
        return "unresolved"
    try:
        with session_factory() as session:
            ctx = guard_recovery(session, machine_token,
                organization_id=claim.organization_id, store_id=claim.store_id,
                run_id=claim.run_id, expected_runtime=repository.SDK_RUNTIME)
            row, run = ctx.execution, ctx.run
            if (ctx.principal.machine_id != claim.machine_id
                    or row.cancel_requested_at is not None):
                return "unresolved"
            steps = list_steps(session, run.organization_id, run.store_id, run.id)
            ledgers = list(session.scalars(select(CostLedger).where(
                CostLedger.organization_id == run.organization_id,
                CostLedger.store_id == run.store_id, CostLedger.run_id == run.id)))
            answer_ids = list(session.scalars(select(AgentAnswer.run_id).where(
                AgentAnswer.organization_id == run.organization_id,
                AgentAnswer.store_id == run.store_id, AgentAnswer.run_id == run.id)))
            audit_key = f"agent_sdk_abort:{run.id}:{claim.epoch}"
            abort = session.scalar(select(AuditEvent).where(
                AuditEvent.organization_id == run.organization_id,
                AuditEvent.store_id == run.store_id, AuditEvent.target_id == run.id,
                AuditEvent.target_type == "agent_run", AuditEvent.idempotency_key == audit_key))
            # A prior exact close is recognized by its own durable audit, never
            # generic Run.failed, memory state, or the legacy cancelled exception.
            if (run.state == "failed" and not answer_ids
                    and row.owner_machine_id is None and row.lease_until is None
                    and row.epoch == claim.epoch + 1 and abort is not None
                    and abort.actor_id == ctx.principal.subject_id
                    and abort.details.get("machine_id") == claim.machine_id
                    and abort.details.get("claim_epoch") == claim.epoch
                    and abort.details.get("runtime_binding") == repository.sdk_runtime_binding()):
                outcome = abort.details.get("outcome")
                if ((outcome == "failed" and abort.action == "agent_run.worker_failed"
                     and row.phase == "finished" and row.dispatch_state == "settled" and row.effect_status == "none")
                    or (outcome == "unknown" and abort.action == "agent_run.worker_unknown"
                        and row.phase == "unknown" and row.dispatch_state == "unknown" and row.effect_status == "external_unknown")):
                    return "already_closed"
                return "unresolved"
            # Validate all Step/ledger pairs before calling anything definite.
            if [step.sequence_no for step in steps] != list(range(1, len(steps) + 1)):
                return "unresolved"
            by_step = {}
            for ledger in ledgers:
                if ledger.step_id in by_step:
                    return "unresolved"
                by_step[ledger.step_id] = ledger
            models = [step for step in steps if step.kind == "model"]
            if set(by_step) != {step.id for step in models}:
                return "unresolved"
            observed_v2 = set()
            for step in models:
                ledger = by_step[step.id]
                if (ledger.call_key != f"{run.id}:{step.sequence_no}"
                        or ledger.requester_id != run.requester_id
                        or ledger.provider_key != run.provider_key or ledger.model != run.model):
                    return "unresolved"
                if (step.state == "running" and ledger.status in
                        ("settled", "reconciliation_required", "unknown")):
                    # v2 persists the provider result/accounting BEFORE child
                    # completion. A lost/diagnostic child therefore leaves an
                    # active model with a known bill or occupied unknown bill.
                    # This is only a close permission, never model completion.
                    from backend.app.modules.agents import runtime_protocol as wire
                    frozen = row.frozen_context
                    record = repository.validate_pi_runtime_record(
                        frozen.get("pi_runtime_record") if type(frozen) is dict else None)
                    if (record["contract_version"] not in (2, 3) or run.provider_key != "opencode-go"
                            or run.model != "deepseek-v4.1-flash"):
                        return "unresolved"
                    facts = repository.pi_provider_receipt_facts(session,
                        run.organization_id, run.store_id, run.id)
                    receipt = repository.pi_provider_receipt(session, claim, step.id)
                    if (receipt is None or sum(item["model_step_id"] == step.id for item in facts) != 1
                            or receipt.state not in ("responded", "unknown", "not_sent")
                            or (receipt.state == "responded") != (ledger.status in ("settled", "reconciliation_required"))
                            or receipt.send_started is not (False if receipt.state == "not_sent" else True)
                            or step.input_ref != "sha256:" + repository._canonical_digest({
                                "purpose": "inference", "input_digest": receipt.input_digest,
                                "toolset_version": receipt.toolset_version})):
                        return "unresolved"
                    wire.identifier(receipt.request_id)
                    for value in (receipt.permit_digest, receipt.input_digest,
                                  receipt.request_digest, receipt.manifest_digest):
                        wire._pi_digest(value)
                    if receipt.response_digest is not None:
                        wire._pi_digest(receipt.response_digest)
                    observed_v2.add(step.id)
                if ((step.state == "succeeded" and ledger.status != "settled"
                     and not (type(row.frozen_context) is dict
                         and "pi_runtime_record" in row.frozen_context
                         and ledger.status == "reconciliation_required"))
                    or (step.state in ("pending", "running") and ledger.status != "reserved"
                        and step.id not in observed_v2)):
                    return "unresolved"
            if any(step.kind not in ("model", "tool") or step.state not in
                   ("succeeded", "pending", "running") for step in steps):
                return "unresolved"
            pi_rows = repository.prepare_pi_receipt_close(session, claim, row, steps)
            settled_gap = bool(steps) and all(step.state == "succeeded" for step in steps)
            if run.state == "completed":
                final_audit = session.scalar(select(AuditEvent).where(
                    AuditEvent.organization_id == run.organization_id,
                    AuditEvent.store_id == run.store_id, AuditEvent.target_id == run.id,
                    AuditEvent.target_type == "agent_run",
                    AuditEvent.idempotency_key == f"agent_worker:{run.id}:agent_run.worker_finished:completed",
                    AuditEvent.action == "agent_run.worker_finished"))
                if (settled_gap and steps[-1].kind == "model" and len(answer_ids) == 1
                        and all(item.status == "settled" for item in ledgers)
                        and (pi_rows is None or all(item.state == "succeeded" for item in pi_rows))
                        and row.phase == "finished" and row.dispatch_state == "settled"
                        and row.owner_machine_id is None and row.lease_until is None
                        and row.epoch == claim.epoch and final_audit is not None
                        and final_audit.details.get("machine_id") == claim.machine_id):
                    return "already_completed"
                return "unresolved"
            # No non-exact write, even if cancellation or an old owner advanced epoch.
            if (row.phase != "claimed" or row.owner_machine_id != claim.machine_id
                    or row.epoch != claim.epoch or run.state not in ("created", "running")
                    or answer_ids or abort is not None):
                return "unresolved"
            active = [step for step in steps if step.state in ("pending", "running")]
            if len(active) > 1 or (active and active[0] is not steps[-1]):
                return "unresolved"
            if row.dispatch_state == "not_dispatched":
                # Normal begin rollback leaves no Step/reservation at all.
                # Release only if an actual stored not-dispatched reservation exists.
                if steps and not (len(steps) == 1 and len(active) == 1 and steps[0].kind == "model"):
                    return "unresolved"
                outcome = "failed"
            elif row.dispatch_state == "settled":
                if not settled_gap or run.state != "running":
                    return "unresolved"
                outcome = "failed"
            elif row.dispatch_state == "dispatched":
                if len(active) != 1 or run.state != "running":
                    return "unresolved"
                outcome = "unknown"
            else:
                return "unresolved"
            if pi_rows is not None and any(item.status == "reconciliation_required" for item in ledgers):
                outcome = "unknown"
            actor = _machine_actor(ctx.principal)
            for step in active:
                transition_step(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id, step_id=step.id,
                    expected_run_version=run.version, expected_step_version=step.version,
                    change=StepTransition(to_state=(StepState.UNKNOWN if outcome == "unknown"
                        else StepState.CANCELLED if step.state == "pending" else StepState.FAILED),
                        error_code=code), now=ctx.now)
                run = get_run(session, run.organization_id, run.store_id, run.id)
                if step.kind == "model":
                    ledger = by_step[step.id]
                    if row.dispatch_state == "not_dispatched":
                        cost_ledger.release_cost(session, actor, run, step,
                            reason="sdk_pre_dispatch_failed", now=ctx.now, commit=False)
                    elif ledger.status not in ("settled", "reconciliation_required"):
                        cost_ledger.mark_cost_unknown(session, actor, run, step,
                            reason="sdk_dispatch_unknown", now=ctx.now, commit=False)
            if pi_rows is not None:
                repository.close_pi_receipts(session, claim, pi_rows, code=code, now=ctx.now)
            transition_run(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, expected_version=run.version,
                to_state=RunState.FAILED, now=ctx.now)
            repository._cas_execution(session, row, values={
                "phase": "finished" if outcome == "failed" else "unknown",
                "dispatch_state": "settled" if outcome == "failed" else "unknown",
                "effect_status": "none" if outcome == "failed" else "external_unknown",
                "owner_machine_id": None, "lease_until": None, "epoch": claim.epoch + 1,
            }, expected_epoch=claim.epoch)
            record_event(session, actor_id=ctx.principal.subject_id,
                organization_id=run.organization_id, store_id=run.store_id,
                action="agent_run.worker_failed" if outcome == "failed" else "agent_run.worker_unknown",
                target_type="agent_run", target_id=run.id, idempotency_key=audit_key,
                details={"machine_id": claim.machine_id, "claim_epoch": claim.epoch,
                    "runtime_binding": repository.sdk_runtime_binding(), "outcome": outcome, "code": code})
            session.commit()
            return outcome
    except Exception as error:
        # Keep the actual object only in Host memory. The bridge exposes sanitized
        # metadata, never str(error), SQL parameters, tokens, or answer content.
        if errors is not None:
            errors.append(error)
        # This includes a successful commit with a lost reply: no invented rollback.
        return "unresolved"


def _close_unknown(session_factory, machine_token: str, claim: AgentClaim,
                   *, code: str, expected_runtime: tuple = repository.LEGACY_RUNTIME) -> str:
    """Close an actually-dispatched attempt as unknown; never replay it."""
    return _recovery_close(session_factory, machine_token, claim,
                           code=code, definite=False, expected_runtime=expected_runtime)


async def _close_pre_dispatch(session_factory, machine_token: str, claim: AgentClaim,
                              code: str, *, settled_ok: bool = False) -> str:
    """Nothing provably left the process: close without inventing an effect."""
    return _recovery_close(session_factory, machine_token, claim,
                           code=code, definite=True, settled_ok=settled_ok)


async def _execute_loop(*, session_factory, machine_token: str, claim: AgentClaim,
                        provider, lease_seconds: int) -> str:
    tool_result: dict | None = None
    extra_evidence: list[dict] = []
    while True:
        # ---- pre-call transaction: validate, allocate step, reserve, dispatch ----
        try:
            with session_factory() as session:
                ctx = guard_execution(session, machine_token, claim,
                                      additional_evidence=extra_evidence or None)
                run = ctx.run
                steps = list_steps(session, run.organization_id, run.store_id, run.id)
                if run.state == RunState.CREATED.value:
                    run = transition_run(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id,
                        expected_version=run.version, to_state=RunState.RUNNING)
                    steps = []
                if run.state != RunState.RUNNING.value:
                    _fail("agent_worker_run_not_running")
                budget = _remaining_budget(run, steps)
                budget["input"] = input_capacity(run.model, output_tokens=budget["output"],
                                                  remaining_input=budget["input"])
                origin_text = _origin_text(session, ctx.execution, run)
                tools, approved_skill = _tool_views(session, ctx, origin_text)
                context = _history_context(session, ctx.execution, run, ctx.execution.origin_message_id)
                prompt_text = fit_prompt(
                    lambda history: service._prompt(origin_text, tool_result=tool_result,
                        context=history, tools=tools, approved_skill=approved_skill),
                    context, budget["input"])
                step = append_step(session, StepCreate(
                    id=str(uuid4()), organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id,
                    sequence_no=len(steps) + 1, kind=StepKind.MODEL,
                    input_ref="sha256:" + sha256(prompt_text.encode("utf-8")).hexdigest()),
                    expected_run_version=run.version, now=ctx.now)
                run = get_run(session, run.organization_id, run.store_id, run.id)
                step = transition_step(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id, step_id=step.id,
                    expected_run_version=run.version, expected_step_version=step.version,
                    change=StepTransition(to_state=StepState.RUNNING), now=ctx.now)
                cost_ledger.reserve_cost(session, ctx.person, run, step,
                    max_input_tokens=budget["input"], max_output_tokens=budget["output"],
                    now=ctx.now, commit=False)
                record_worker_dispatch(session, claim, step_id=step.id)
                run = get_run(session, run.organization_id, run.store_id, run.id)
                run_version, step_version = run.version, step.version
                step_id = step.id
                request = ProviderRequest(
                    organization_id=run.organization_id, store_id=run.store_id,
                    requester_id=run.requester_id, task_id=run.id,
                    purpose="shop_assistant", input_text=prompt_text,
                    budget=CallBudget(budget["input"], budget["output"]),
                    deadline_at=run.deadline_at)
                session.commit()
        except (AgentExecutorError, ModelContextError) as exc:
            return await _close_after_failure(session_factory, machine_token, claim, exc.code)
        except AgentPersistenceError as exc:
            return await _close_after_failure(session_factory, machine_token, claim, exc.code)
        except (HTTPException, ScopeSecurityFenceError):
            return await _close_after_failure(session_factory, machine_token, claim,
                                              "agent_worker_guard_denied")
        except cost_ledger.CostLedgerError as exc:
            return await _close_pre_dispatch(session_factory, machine_token, claim, str(exc))

        # ---- network happens outside every transaction ----
        try:
            response = await _call_provider(provider=provider, request=request,
                                            session_factory=session_factory,
                                            machine_token=machine_token, claim=claim,
                                            lease_seconds=lease_seconds)
        except asyncio.CancelledError:
            _close_unknown(session_factory, machine_token, claim, code="agent_worker_cancelled")
            raise
        except AgentExecutorError as exc:
            if exc.code in ("agent_worker_lease_lost", "agent_worker_provider_timeout"):
                return _close_unknown(session_factory, machine_token, claim,
                                      code=exc.code)
            _close_unknown(session_factory, machine_token, claim, code=exc.code)
            return "unknown"
        except ProviderError as exc:
            usage = (type(exc.input_tokens) is int and exc.input_tokens >= 0
                     and type(exc.output_tokens) is int and exc.output_tokens >= 0)
            if usage:
                outcome = _close_with_usage(session_factory, machine_token, claim,
                                           step_id=step_id,
                                           run_version=run_version,
                                           step_version=step_version,
                                           input_tokens=exc.input_tokens,
                                           output_tokens=exc.output_tokens,
                                           code=exc.code)
                if outcome is not None:
                    return outcome
            _close_unknown(session_factory, machine_token, claim, code=exc.code)
            return "unknown"
        except Exception:
            _close_unknown(session_factory, machine_token, claim, code="agent_worker_provider_error")
            return "unknown"

        # ---- post-call transaction: settle, decide, persist ----
        try:
            with session_factory() as session:
                ctx = guard_execution(session, machine_token, claim, step_id=step_id,
                                      expected_run_version=run_version,
                                      expected_step_version=step_version,
                                      additional_evidence=extra_evidence or None)
                run, step = ctx.run, ctx.step
                ledger = cost_ledger.settle_cost(session, ctx.person, run, step,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens, now=ctx.now, commit=False)
                usage_unknown = (ledger is None or ledger.status in (
                    "reconciliation_required", "unknown"))
                over_budget = (response.input_tokens > budget["input"]
                               or response.output_tokens > budget["output"])
                if usage_unknown or over_budget:
                    code = ("agent_worker_usage_unknown" if usage_unknown
                            else "agent_worker_usage_exceeded")
                    transition_step(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id, step_id=step.id,
                        expected_run_version=run.version,
                        expected_step_version=step.version,
                        change=StepTransition(to_state=StepState.FAILED,
                            input_tokens=response.input_tokens,
                            output_tokens=response.output_tokens,
                            error_code=code), now=ctx.now)
                    run = get_run(session, run.organization_id, run.store_id, run.id)
                    transition_run(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id,
                        expected_version=run.version, to_state=RunState.FAILED,
                        now=ctx.now)
                    _audit(session, ctx, "agent_run.worker_failed", code)
                    settle_worker_control(session, claim,
                        outcome="unknown" if usage_unknown else "finished")
                    session.commit()
                    return "unknown" if usage_unknown else "failed"
                try:
                    decision = service._decode_decision(response.suggestion)
                except service.AgentRuntimeError:
                    decision = None
                if decision is None:
                    transition_step(session, organization_id=ctx.run.organization_id,
                        store_id=ctx.run.store_id, run_id=ctx.run.id, step_id=step.id,
                        expected_run_version=run.version, expected_step_version=step.version,
                        change=StepTransition(to_state=StepState.FAILED,
                            input_tokens=response.input_tokens,
                            output_tokens=response.output_tokens,
                            error_code="agent_decision_invalid"),
                        now=ctx.now)
                    run = get_run(session, ctx.run.organization_id, ctx.run.store_id, ctx.run.id)
                    run = transition_run(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id,
                        expected_version=run.version, to_state=RunState.FAILED,
                        now=ctx.now)
                    _audit(session, ctx, "agent_run.worker_failed", "agent_decision_invalid")
                    settle_worker_control(session, claim, outcome="finished")
                    session.commit()
                    return "failed"
                transition_step(session, organization_id=ctx.run.organization_id,
                    store_id=ctx.run.store_id, run_id=ctx.run.id, step_id=step.id,
                    expected_run_version=run.version, expected_step_version=step.version,
                    change=StepTransition(to_state=StepState.SUCCEEDED,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        output_ref="sha256:" + _digest(response.suggestion)),
                    now=ctx.now)
                run = get_run(session, ctx.run.organization_id, ctx.run.store_id, ctx.run.id)
                if decision["action"] == "final":
                    evidence = answer_receipts.merge_evidence(
                        ctx.execution.frozen_context.get("evidence") or [], extra_evidence)
                    valid, _ = answer_receipts.validate_evidence(session, ctx.person, evidence)
                    if not valid:
                        run = transition_run(session, organization_id=run.organization_id,
                            store_id=run.store_id, run_id=run.id,
                            expected_version=run.version, to_state=RunState.FAILED,
                            now=ctx.now)
                        _audit(session, ctx, "agent_run.worker_failed",
                               "agent_worker_source_changed")
                        settle_worker_control(session, claim, outcome="finished")
                        session.commit()
                        return "failed"
                    message = conversation_service.append_message(session, ctx.person,
                        conversation_id=ctx.execution.frozen_context["conversation_id"],
                        role="assistant", content=decision["answer"],
                        source_ref=f"run:{run.id}", now=ctx.now)
                    run = transition_run(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id,
                        expected_version=run.version, to_state=RunState.COMPLETED)
                    record_answer(session, run=run, message=message, evidence=evidence)
                    settle_worker_attempt(session, claim, step_id=step.id, outcome="finished")
                    _audit(session, ctx, "agent_run.worker_finished", "completed")
                    session.commit()
                    return "finished"
                try:
                    definition = _tool_definition(ctx, decision)
                    _tool_views(session, ctx, "")
                    _check_tool_scope(session, ctx, decision, definition)
                    _remaining_budget(run, list_steps(
                        session, run.organization_id, run.store_id, run.id),
                        phase="tool")
                except (AgentExecutorError, HTTPException, ValidationError) as exc:
                    code = getattr(exc, "code", "tool_scope_denied")
                    run = transition_run(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id,
                        expected_version=run.version, to_state=RunState.FAILED,
                        now=ctx.now)
                    _audit(session, ctx, "agent_run.worker_failed", code)
                    settle_worker_control(session, claim, outcome="finished")
                    session.commit()
                    return "failed"
                if definition.mode == "write" and definition.confirmation != "none":
                    _stage_waiting(session, ctx, claim, step, decision, definition)
                    session.commit()
                    return "waiting"
                # the settled model attempt may license exactly one more step
                settle_worker_attempt(session, claim, step_id=step.id,
                                      outcome="continue")
                # read tool step + dispatch intent are durable before invocation
                tool_step, run_version = _open_tool_step(session, ctx, claim, run,
                                                         decision, definition)
                session.commit()
                tool_step_id, tool_step_version = tool_step.id, tool_step.version
        except AgentExecutorError as exc:
            return await _close_after_failure(session_factory, machine_token, claim, exc.code)
        except AgentPersistenceError as exc:
            return await _close_after_failure(session_factory, machine_token, claim, exc.code)
        except (HTTPException, ScopeSecurityFenceError):
            return await _close_after_failure(session_factory, machine_token, claim,
                                              "agent_worker_guard_denied")
        except (answer_receipts.EvidenceError, service.AgentRuntimeError) as exc:
            code = getattr(exc, "code", "agent_worker_source_changed")
            return await _close_after_failure(session_factory, machine_token, claim, code)
        except cost_ledger.CostLedgerError:
            return await _close_after_failure(session_factory, machine_token, claim,
                                              "agent_worker_cost_conflict")
        except Exception:
            return await _close_after_failure(session_factory, machine_token, claim,
                                              "agent_worker_internal_error")

        # ---- tool transaction: invoke the fixed local tool, settle the attempt ----
        try:
            with session_factory() as session:
                ctx = guard_execution(session, machine_token, claim,
                                      step_id=tool_step_id,
                                      expected_run_version=run_version,
                                      expected_step_version=tool_step_version,
                                      additional_evidence=extra_evidence or None)
                _tool_views(session, ctx, "")
                _check_tool_scope(session, ctx, decision, definition)
                started = time.monotonic()
                try:
                    result = invoke_tool(session, requester=ctx.person, agent=ctx.agent,
                        tool_key=decision["tool_key"], version=decision["version"],
                        arguments=decision["arguments"],
                        idempotency_key=(f"{ctx.run.id}:{ctx.step.sequence_no}"
                                         if definition.idempotency == "required" else None))
                except ToolRegistryError as exc:
                    _close_known_failure(session, ctx, claim, exc.code, step=ctx.step)
                    session.commit()
                    return "failed"
                if time.monotonic() - started > definition.timeout_seconds:
                    _fail("agent_tool_timeout")
                now = repository._db_clock(session)
                row = repository.execution_for(session, ctx.run.organization_id,
                                               ctx.run.store_id, ctx.run.id,
                                               for_update=True)
                if (row is None or row.lease_until is None or row.lease_until <= now
                        or row.cancel_requested_at is not None
                        or ctx.run.deadline_at is None or ctx.run.deadline_at <= now):
                    _fail("agent_worker_lease_lost")
                refs = answer_receipts.capture_tool_evidence(session, ctx.person,
                    tool_key=definition.key, permission=definition.requester_permission,
                    result=result)
                extra_evidence = answer_receipts.merge_evidence(extra_evidence, refs)
                transition_step(session, organization_id=ctx.run.organization_id,
                    store_id=ctx.run.store_id, run_id=ctx.run.id, step_id=ctx.step.id,
                    expected_run_version=ctx.run.version,
                    expected_step_version=ctx.step.version,
                    change=StepTransition(to_state=StepState.SUCCEEDED,
                        output_ref="sha256:" + _digest(result)),
                    now=ctx.now)
                _append_frozen_evidence(session, ctx, refs)
                settle_worker_attempt(session, claim, step_id=ctx.step.id, outcome="continue")
                _audit(session, ctx, "agent_tool.worker_invoked", definition.key)
                session.commit()
        except (AgentExecutorError, AgentPersistenceError) as exc:
            code = getattr(exc, "code", "agent_worker_failed")
            return await _close_after_failure(session_factory, machine_token, claim, code)
        except (HTTPException, ScopeSecurityFenceError):
            return await _close_after_failure(session_factory, machine_token, claim,
                                              "agent_worker_guard_denied")
        except answer_receipts.EvidenceError as exc:
            return await _close_after_failure(session_factory, machine_token, claim, str(exc))
        tool_result = {"key": decision["tool_key"], "version": decision["version"],
                       "data": result}


def _pi_binding_present(ctx: ExecutionContext) -> bool:
    return (isinstance(ctx.execution.frozen_context, dict)
            and "pi_readonly_binding" in ctx.execution.frozen_context)


def pi_readonly_decision(session: Session, ctx: ExecutionContext, *,
                         name: object, version: object, native_call_id: object,
                         arguments: object) -> dict:
    """Host translation under an already acquired live execution guard.

    This does not acquire a claim, attach an old Run, dispatch a tool or launch
    an SDK. The future runtime must supply the actual native call ID; it may
    not substitute a sequence number. All dispatch still uses the normal
    claim/Run/Step CAS and the gate below.
    """
    if not _pi_binding_present(ctx):
        _fail("agent_pi_profile_invalid")
    _tool_views(session, ctx, "")
    from backend.app.modules.agents import capability_discovery as discovery
    if name not in discovery.active_names(session, ctx):
        _fail("agent_pi_tool_not_loaded")
    if (type(native_call_id) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", native_call_id) is None):
        _fail("agent_pi_call_invalid")
    try:
        key, exact_version = capability_registry.pi_pair(
            ctx.execution.frozen_context["pi_readonly_binding"], name, version)
        definition = resolve_tool(key, exact_version, store_id=ctx.run.store_id)
        definition.input_model.model_validate(arguments)
        # Preserve original schema-validated values, never coerce/trim them.
        copied = json.loads(json.dumps(arguments, allow_nan=False))
    except ToolRegistryError as exc:
        _fail(exc.code)
    except (ValidationError, ValueError, TypeError, RecursionError):
        _fail("tool_input_invalid")
    return {"tool_key": key, "version": exact_version, "arguments": copied,
            "native_call_id": native_call_id, "toolset_version": ctx.run.toolset_version}


def _pi_call_identity(session: Session, ctx: ExecutionContext, decision: dict) -> tuple[str, str]:
    """Recheck at every call boundary, including after Step intent commit."""
    _tool_views(session, ctx, "")
    if (type(decision) is not dict
            or set(decision) != {"tool_key", "version", "arguments",
                                 "native_call_id", "toolset_version"}
            or decision["toolset_version"] != ctx.run.toolset_version
            or type(decision["native_call_id"]) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", decision["native_call_id"]) is None):
        _fail("agent_pi_call_invalid")
    entries = ctx.execution.frozen_context["pi_readonly_binding"]["tools"]
    pair = decision["tool_key"], decision["version"]
    if pair not in [(e["tool_key"], e["version"]) for e in entries]:
        _fail("agent_pi_tool_not_allowed")
    try:
        call_key = "pi:" + _digest([ctx.run.id, decision["native_call_id"]])
        call_ref = "sha256:" + _digest({
            "toolset_version": ctx.run.toolset_version, "call_key": call_key,
            "tool_key": pair[0], "version": pair[1],
            "input_digest": _digest(decision["arguments"])})
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail("agent_pi_call_invalid")
    return call_key, call_ref


def _tool_definition(ctx: ExecutionContext, decision: dict):
    if legacy_code_tool(decision["tool_key"], decision["version"],
                        store_id=ctx.run.store_id):
        _fail("tool_legacy_code_execution_disabled")
    try:
        return resolve_tool(decision["tool_key"], decision["version"],
                            store_id=ctx.run.store_id)
    except ToolRegistryError:
        _fail("tool_not_registered")


def _check_tool_scope(session: Session, ctx: ExecutionContext, decision: dict,
                      definition) -> None:
    """The tool's current grants and input model are re-checked under the guard."""
    if _pi_binding_present(ctx):
        call_key, call_ref = _pi_call_identity(session, ctx, decision)
        if (definition is not resolve_tool(decision["tool_key"], decision["version"],
                                          store_id=ctx.run.store_id)
                or definition is not capability_registry._pi_definition(
                    definition.key, definition.version, profile=ctx.execution.frozen_context["pi_readonly_binding"]["profile"])):
            _fail("agent_pi_tool_not_allowed")
        if ctx.step is not None and ctx.step.kind == StepKind.TOOL.value:
            call = session.scalar(select(AgentToolCall).where(
                AgentToolCall.organization_id == ctx.run.organization_id,
                AgentToolCall.store_id == ctx.run.store_id,
                AgentToolCall.run_id == ctx.run.id, AgentToolCall.step_id == ctx.step.id))
            if (ctx.step.state != StepState.RUNNING.value or ctx.step.input_ref != call_ref
                    or call is None or call.idempotency_key != call_key
                    or (call.tool_key, call.tool_version) != (definition.key, definition.version)
                    or call.input_digest != _digest(decision["arguments"])):
                _fail("agent_pi_call_invalid")
    try:
        from backend.app.modules.identity.service import authorize_scope
        authorize_scope(session, ctx.person, organization_id=ctx.run.organization_id,
                        store_id=ctx.run.store_id,
                        permission=definition.requester_permission, now=ctx.now)
        authorize_scope(session, ctx.agent, organization_id=ctx.run.organization_id,
                        store_id=ctx.run.store_id,
                        permission=definition.agent_permission, now=ctx.now)
        definition.input_model.model_validate(decision["arguments"])
    except (HTTPException, ValidationError):
        _fail("tool_scope_denied")
    frozen = ctx.execution.frozen_context
    pi_record = (frozen.get('pi_runtime_record')
                 if _pi_binding_present(ctx)
                 and repository.runtime_identity(frozen) == repository.SDK_RUNTIME else None)
    if not service.supported_prompt(ctx.run, pi_runtime_record=pi_record):
        _fail("agent_prompt_version_unsupported")
    pin = skill_service.runtime_pin(session, ctx.run)
    if pin is not None:
        frozen = {(entry.get("key"), entry.get("version"))
                  for entry in pin.allowed_tools if isinstance(entry, dict)}
        if (definition.key, definition.version) not in frozen:
            _fail("agent_worker_toolset_changed")


def _open_tool_step(session: Session, ctx: ExecutionContext, claim: AgentClaim,
                    run, decision: dict, definition, *,
                    expected_runtime: tuple = repository.LEGACY_RUNTIME) -> AgentStep:
    repository.require_worker_claim(session, claim, expected_runtime=expected_runtime)
    steps = list_steps(session, run.organization_id, run.store_id, run.id)
    call_key = f"{run.id}:{len(steps) + 1}"
    call_ref = None
    if _pi_binding_present(ctx):
        if run.id != ctx.run.id or expected_runtime != repository.SDK_RUNTIME:
            _fail("agent_pi_profile_invalid")
        call_key, call_ref = _pi_call_identity(session, ctx, decision)
        _check_tool_scope(session, ctx, decision, definition)
        if session.scalar(select(AgentToolCall.id).where(
                AgentToolCall.organization_id == run.organization_id,
                AgentToolCall.store_id == run.store_id, AgentToolCall.run_id == run.id,
                AgentToolCall.idempotency_key == call_key)) is not None:
            _fail("agent_pi_call_replay")
        # Pre-read gives a stable public rejection. Concurrent callers still
        # rely on append_step's real Run-version CAS and the existing scoped
        # AgentToolCall unique constraint, never on this read as a lock.
    call = ToolCallCreate(id=str(uuid4()), tool_key=decision["tool_key"],
        tool_version=decision["version"], idempotency_key=call_key,
        input_digest=_digest(decision["arguments"]))
    step = append_step(session, StepCreate(id=str(uuid4()),
        organization_id=run.organization_id, store_id=run.store_id,
        run_id=run.id, sequence_no=len(steps) + 1, kind=StepKind.TOOL, input_ref=call_ref,
        tool_call=call), expected_run_version=run.version, now=ctx.now)
    run = get_run(session, run.organization_id, run.store_id, run.id)
    step = transition_step(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, step_id=step.id,
        expected_run_version=run.version, expected_step_version=step.version,
        change=StepTransition(to_state=StepState.RUNNING), now=ctx.now)
    record_worker_dispatch(session, claim, step_id=step.id, expected_runtime=expected_runtime)
    run = get_run(session, run.organization_id, run.store_id, run.id)
    return step, run.version


def _stage_waiting(session: Session, ctx: ExecutionContext, claim: AgentClaim,
                   model_step: AgentStep, decision: dict, definition) -> None:
    """Park one exact write for a real human decision; never run it here."""
    run = ctx.run
    steps = list_steps(session, run.organization_id, run.store_id, run.id)
    call_key = f"{run.id}:{len(steps) + 1}"
    call = ToolCallCreate(id=str(uuid4()), tool_key=decision["tool_key"],
        tool_version=decision["version"], idempotency_key=call_key,
        input_digest=_digest(decision["arguments"]))
    step = append_step(session, StepCreate(id=str(uuid4()),
        organization_id=run.organization_id, store_id=run.store_id,
        run_id=run.id, sequence_no=len(steps) + 1, kind=StepKind.TOOL,
        tool_call=call), expected_run_version=run.version, now=ctx.now)
    expires_at = min(ctx.now + timedelta(seconds=service.CONFIRMATION_SECONDS),
                     run.deadline_at)
    create_confirmation(session, AgentConfirmation(
        run_id=run.id, organization_id=run.organization_id, store_id=run.store_id,
        step_id=step.id, requester_id=run.requester_id,
        tool_key=decision["tool_key"], tool_version=decision["version"],
        arguments=decision["arguments"], input_digest=_digest(decision["arguments"]),
        state="pending", version=1, expires_at=expires_at,
        created_at=ctx.now, updated_at=ctx.now))
    run = get_run(session, run.organization_id, run.store_id, run.id)
    transition_run(session, organization_id=run.organization_id,
        store_id=run.store_id, run_id=run.id, expected_version=run.version,
        to_state=RunState.WAITING_FOR_HUMAN, now=ctx.now)
    settle_worker_attempt(session, claim, step_id=model_step.id, outcome="waiting")
    _audit(session, ctx, "agent_confirmation.requested", definition.key)


async def _close_after_failure(session_factory, machine_token: str, claim: AgentClaim,
                               code: str) -> str:
    """Fail closed on the durable dispatch fact, not on an error-code list.

    A never-dispatched attempt can be released; anything that may have reached
    the provider keeps its reservation as unknown (no fake refund).
    """
    dispatch_state = _dispatch_state_of(session_factory, claim)
    if dispatch_state in ("not_dispatched", "settled"):
        # A settled loop with no new in-flight attempt is a definitive local
        # failure: the already-settled cost stays, nothing is released/unknown.
        return await _close_pre_dispatch(session_factory, machine_token, claim, code,
                                         settled_ok=dispatch_state == "settled")
    _close_unknown(session_factory, machine_token, claim, code=code)
    return "unknown"


async def tick_real_agent_worker(*, session_factory, machine_token: str,
                                 provider_factory=None, lease_seconds: int = 30) -> str:
    """Run at most one live claim through a real model loop.

    Returns ``disabled``/``idle``/``finished``/``waiting``/``cancelled``/``unknown``.
    """
    if not _enabled():
        return "disabled"
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
        raise AgentExecutorError("agent_lease_invalid")
    if not isinstance(machine_token, str):
        raise AgentExecutorError("agent_worker_token_invalid")
    factory = provider_factory or service._provider_factory
    try:
        claim = _claim(session_factory=session_factory, machine_token=machine_token,
                       lease_seconds=lease_seconds)
    except (HTTPException, ScopeSecurityFenceError, AgentPersistenceError) as exc:
        return "denied" if not isinstance(exc, AgentPersistenceError) else "idle"
    if claim is None:
        return "idle"
    with session_factory() as session:
        row = repository.execution_for(session, claim.organization_id, claim.store_id,
                                       claim.run_id)
        if row is None:
            return "idle"
        if row.cancel_requested_at is not None:
            # HTTP cancellation may already have fenced/closed this claim
            # between claim commit and this read. Never re-close a stale claim.
            return "unknown" if row.cancel_resolution == "unknown" else "cancelled"
        grant = repository._grant_pending(row)
        run = get_run(session, row.organization_id, row.store_id, row.run_id)
    if run is None:
        return "unknown"
    if not service.supported_prompt(run):
        return await _close_pre_dispatch(
            session_factory, machine_token, claim,
            "agent_prompt_version_unsupported")
    if grant is not None:
        return await _execute_approved(session_factory=session_factory,
                                       machine_token=machine_token, claim=claim,
                                       grant=grant)
    try:
        provider = factory()
    except Exception:
        return await _close_pre_dispatch(
            session_factory, machine_token, claim,
            "agent_provider_configuration_invalid")
    config = getattr(provider, "config", None)
    actual_model = getattr(config, "model", None) or getattr(provider, "model", None)
    # The frozen identity is the whole contract: a missing/unknown model can
    # never be treated as matching, and the registered implementation only
    # speaks the chat-completions/current-prompt/shop-assistant contract.
    if (not isinstance(actual_model, str) or not actual_model
            or actual_model != run.model
            or (run.provider_key, run.prompt_version, run.agent_key,
                run.agent_version) != _FROZEN_RUN_IDENTITY):
        return await _close_pre_dispatch(
            session_factory, machine_token, claim,
            "agent_provider_configuration_invalid")
    try:
        return await _execute_loop(session_factory=session_factory,
                                   machine_token=machine_token, claim=claim,
                                   provider=provider, lease_seconds=lease_seconds)
    except AgentExecutorError:
        return "unknown"
    except AgentPersistenceError:
        return "unknown"


def _check_tool_call(cell: AgentToolCall | None, run, step, tool: tuple[str, str],
                     digest: str, *, step_id: str) -> None:
    """The durable ToolCall must be exactly this run/step/grant, tamper-evident."""
    if (cell is None or cell.run_id != run.id or cell.step_id != step_id
            or (cell.tool_key, cell.tool_version) != tool
            or cell.input_digest != digest
            or cell.idempotency_key != f"{run.id}:{step.sequence_no}"):
        _fail("agent_approval_fenced")


def _fail_executing_confirmation(session_factory, machine_token: str,
                                 claim: AgentClaim, code: str) -> None:
    """Leave no confirmation stuck in ``executing`` after a failed approval.

    The human approval reason is never replaced by an internal error code.
    When the attempt lease is already gone the execution guard cannot pass;
    a bounded recovery close may then settle the SAME attempt only, and a
    newer owner/epoch is never overwritten by this stale claim.
    """
    def settle(session: Session, run) -> None:
        confirmation = get_confirmation(session, run.organization_id,
                                        run.store_id, run.id)
        if confirmation is not None and confirmation.state == "executing":
            repository.set_confirmation_state(
                session, confirmation, "unknown",
                reason=(confirmation.decision_reason or code))
            session.commit()

    try:
        with session_factory() as session:
            ctx = guard_execution(session, machine_token, claim)
            settle(session, ctx.run)
        return
    except Exception:
        pass
    try:
        with session_factory() as session:
            ctx = guard_recovery(session, machine_token,
                                 organization_id=claim.organization_id,
                                 store_id=claim.store_id, run_id=claim.run_id)
            row = ctx.execution
            if (ctx.principal.machine_id == claim.machine_id
                    and row.owner_machine_id == claim.machine_id
                    and row.epoch == claim.epoch):
                settle(session, ctx.run)
    except Exception:
        return


async def _execute_approved(*, session_factory, machine_token: str, claim: AgentClaim,
                            grant: dict) -> str:
    """Consume ONE frozen approval and run only its fixed write tool."""
    step_id = grant.get("step_id")
    confirmation_id = grant.get("confirmation_id")
    tool = (grant.get("tool_key"), grant.get("tool_version"))
    digest = grant.get("input_digest")
    if (not isinstance(step_id, str) or not isinstance(confirmation_id, str)
            or not isinstance(digest, str)
            or not all(isinstance(part, str) for part in tool)):
        return "unknown"

    def validate_intent(session: Session, ctx: ExecutionContext, step) -> tuple:
        run = ctx.run
        confirmation = get_confirmation(session, run.organization_id,
                                        run.store_id, run.id)
        if (confirmation is None or confirmation.step_id != step_id
                or (confirmation.tool_key, confirmation.tool_version) != tool
                or confirmation.input_digest != digest):
            _fail("agent_approval_fenced")
        try:
            definition = resolve_tool(tool[0], tool[1], store_id=run.store_id)
        except ToolRegistryError:
            _fail("agent_approval_fenced")
        if definition.mode != "write" or definition.confirmation == "none":
            _fail("agent_approval_fenced")
        try:
            definition.input_model.model_validate(confirmation.arguments)
        except ValidationError:
            _fail("tool_scope_denied")
        # Current pin/schema and the live person/agent grants are re-checked
        # inside the same guarded lock domain, never inherited from approval.
        # _tool_views re-derives the visible descriptions from the FROZEN pin
        # (never a fresh release selection); a changed contract fails closed.
        _tool_views(session, ctx, _origin_text(session, ctx.execution, ctx.run))
        _check_tool_scope(session, ctx, {"arguments": confirmation.arguments},
                          definition)
        cell = session.scalar(select(AgentToolCall).where(
            AgentToolCall.organization_id == run.organization_id,
            AgentToolCall.store_id == run.store_id,
            AgentToolCall.run_id == run.id,
            AgentToolCall.step_id == step.id))
        _check_tool_call(cell, run, step, tool, digest, step_id=step.id)
        return confirmation, definition

    try:
        with session_factory() as session:
            ctx = guard_execution(session, machine_token, claim, step_id=step_id)
            run, step = ctx.run, ctx.step
            if step.state != StepState.PENDING.value:
                _fail("agent_approval_fenced")
            validate_intent(session, ctx, step)
            step = transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version, expected_step_version=step.version,
                change=StepTransition(to_state=StepState.RUNNING), now=ctx.now)
            record_worker_dispatch(session, claim, step_id=step.id)
            run = get_run(session, run.organization_id, run.store_id, run.id)
            run_version, step_version = run.version, step.version
            session.commit()
    except (AgentExecutorError, AgentPersistenceError, HTTPException,
            ScopeSecurityFenceError) as exc:
        code = getattr(exc, "code", "agent_worker_guard_denied")
        _fail_executing_confirmation(session_factory, machine_token, claim, code)
        _close_unknown(session_factory, machine_token, claim, code=code)
        return "unknown"

    try:
        with session_factory() as session:
            ctx = guard_execution(session, machine_token, claim, step_id=step_id,
                                  expected_run_version=run_version,
                                  expected_step_version=step_version)
            run, step = ctx.run, ctx.step
            if step.state != StepState.RUNNING.value:
                _fail("agent_approval_fenced")
            confirmation, definition = validate_intent(session, ctx, step)
            frozen = consume_approval_grant(session, claim, step_id=step_id,
                                            confirmation_id=confirmation_id)
            if (frozen.get("input_digest") != digest
                    or (frozen.get("tool_key"), frozen.get("tool_version")) != tool):
                _fail("agent_approval_fenced")
            human_reason = confirmation.decision_reason
            started = time.monotonic()
            result = invoke_tool(session, requester=ctx.person, agent=ctx.agent,
                tool_key=definition.key, version=definition.version,
                arguments=confirmation.arguments,
                idempotency_key=((f"{run.id}:{step.sequence_no}")
                                 if definition.idempotency == "required" else None))
            elapsed = time.monotonic() - started
            if elapsed > definition.timeout_seconds:
                _fail("agent_tool_timeout")
            task_id = result.get("task_id") if isinstance(result, dict) else None
            if not isinstance(task_id, str) or not task_id:
                _fail("agent_task_receipt_missing")
            # Same lock domain, fresh DB clock: a lease/deadline that expired
            # while the tool ran rolls the whole domain transaction back.
            now = repository._db_clock(session)
            row = repository.execution_for(session, run.organization_id,
                                           run.store_id, run.id, for_update=True)
            if (row is None or row.lease_until is None or row.lease_until <= now
                    or row.cancel_requested_at is not None
                    or run.deadline_at is None or run.deadline_at <= now):
                _fail("agent_worker_lease_lost")
            lease_deadline = row.lease_until
            repository.set_confirmation_state(session, confirmation, "approved",
                reason=(human_reason or "worker_executed"), result_task_id=task_id)
            transition_step(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, step_id=step.id,
                expected_run_version=run.version, expected_step_version=step.version,
                change=StepTransition(to_state=StepState.SUCCEEDED,
                    output_ref="sha256:" + _digest(result)), now=ctx.now)
            run = get_run(session, run.organization_id, run.store_id, run.id)
            run = transition_run(session, organization_id=run.organization_id,
                store_id=run.store_id, run_id=run.id, expected_version=run.version,
                to_state=RunState.COMPLETED)
            settle_worker_control(session, claim, outcome="finished",
                                  effect_status="local_task_committed")
            record_event(session, actor_id=ctx.person.subject_id,
                organization_id=run.organization_id, store_id=run.store_id,
                action="agent_run.worker_write_finished", target_type="agent_run",
                target_id=run.id,
                idempotency_key=f"agent_worker_write:{run.id}",
                details={"machine_id": ctx.principal.machine_id, "task_id": task_id,
                         "executed_by": "worker",
                         "human_decision_reason": human_reason})
            # Domain checks do not know this owner's one-shot grant/lease.
            # Enlist the owner's pure final check at the SAME final DB instant.
            def final(db, instant):
                machine_service.authorize_machine_action(db, ctx.principal,
                    organization_id=claim.organization_id, store_id=claim.store_id,
                    action=EXECUTE_ACTION, now=instant)
                for principal, permission in ((ctx.person, "agents.run"),
                        (ctx.person, definition.requester_permission), (ctx.agent, definition.agent_permission)):
                    authorize_scope(db, principal, organization_id=claim.organization_id,
                                    store_id=claim.store_id, permission=permission, now=instant)
                current = repository.execution_for(db, claim.organization_id, claim.store_id, claim.run_id)
                current_run = get_run(db, claim.organization_id, claim.store_id, claim.run_id)
                current_confirmation = get_confirmation(db, claim.organization_id, claim.store_id, claim.run_id)
                current_step = db.scalar(select(AgentStep).where(AgentStep.organization_id == claim.organization_id,
                    AgentStep.store_id == claim.store_id, AgentStep.run_id == claim.run_id,
                    AgentStep.id == step_id).execution_options(populate_existing=True))
                expires = repository._grant_time(frozen, "expires_at")
                if (current is None or current.epoch != claim.epoch or current.phase != "finished"
                        or current.cancel_requested_at is not None or current.approval_grant != frozen
                        or not repository._grant_bound(frozen, current)
                        or lease_deadline <= instant or run.deadline_at <= instant):
                    _fail("agent_worker_lease_lost")
                if (current_run is None or current_run.state != RunState.COMPLETED.value or current_step is None
                        or current_step.state != StepState.SUCCEEDED.value or current_confirmation is None
                        or current_confirmation.state != "approved" or current_confirmation.result_task_id != task_id):
                    _fail("agent_approval_fenced")
                if (expires is None or expires <= instant or confirmation.expires_at <= instant
                        or confirmation.input_digest != digest
                        or _digest(confirmation.arguments) != digest):
                    _fail("agent_approval_expired")
            commit_guard.enlist(session, action="worker.approved_effect", check=final)
            session.commit()
            return "finished"
    except (AgentExecutorError, AgentPersistenceError, HTTPException,
            ScopeSecurityFenceError, cost_ledger.CostLedgerError) as exc:
        code = getattr(exc, "code", "agent_worker_tool_failed")
        _fail_executing_confirmation(session_factory, machine_token, claim, code)
        _close_unknown(session_factory, machine_token, claim, code=code)
        return "unknown"
    except Exception:
        _fail_executing_confirmation(session_factory, machine_token, claim,
                                     "agent_worker_tool_failed")
        _close_unknown(session_factory, machine_token, claim,
                       code="agent_worker_tool_failed")
        return "unknown"


def _reconcile_orphaned_costs(session: Session, ctx, row, run) -> str:
    """Close reserved cost facts of an already-unknown/terminal attempt.

    The control row may have been closed by a reaper or HTTP cancel before the
    worker learned the outcome; its reservation must still become *unknown*
    (never released, never overwriting a settled charge or a task receipt).
    No execution versions or terminal receipts are rewritten here.
    """
    if row.phase not in ("unknown", "finished", "cancelled") and run.state not in (
            "completed", "failed", "cancelled"):
        return "verified"
    steps = list_steps(session, run.organization_id, run.store_id, run.id)
    actor = _machine_actor(ctx.principal)
    reconciled = 0
    try:
        closed_confirmation = repository._close_executing_confirmation(
            session, run.id,
            dispatched=row.dispatch_state in ("dispatched", "unknown"),
            now=ctx.now)
        for step in steps:
            if step.kind != "model":
                continue
            ledger = session.scalar(select(CostLedger).where(
                CostLedger.organization_id == run.organization_id,
                CostLedger.store_id == run.store_id,
                CostLedger.run_id == run.id,
                CostLedger.step_id == step.id))
            if ledger is None or ledger.status != "reserved":
                continue
            cost_ledger.mark_cost_unknown(session, actor, run, step,
                reason="agent_worker_unknown_recovered", now=ctx.now, commit=False)
            reconciled += 1
    except (cost_ledger.CostLedgerError, AgentPersistenceError, HTTPException):
        session.rollback()
        return "preserved"
    if reconciled or closed_confirmation:
        session.commit()
        return "unknown_preserved"
    return "already_closed"


def recover_attempt(*, session_factory, machine_token: str, organization_id: str,
                    store_id: str, run_id: str) -> str:
    """Machine-only reconciliation of an old attempt; never a new execution grant.

    Uses the shared-fence recovery guard (no exclusive reaper, no person/agent
    identity). Only an expired/own-claim attempt or an already-unknown/terminal
    row is closed; a live foreign claim is left untouched.
    """
    with session_factory() as session:
        try:
            ctx = guard_recovery(session, machine_token,
                                 organization_id=organization_id,
                                 store_id=store_id, run_id=run_id)
        except (HTTPException, ScopeSecurityFenceError, AgentPersistenceError):
            return "denied"
        row, run = ctx.execution, ctx.run
        now = ctx.now
        if row.phase == "unknown":
            return _reconcile_orphaned_costs(session, ctx, row, run)
        if row.phase == "finished" or run.state in (
                "completed", "failed", "cancelled"):
            # Terminal control rows may still hold a pre-call reservation if the
            # closing process died between the provider call and the settle.
            outcome = _reconcile_orphaned_costs(session, ctx, row, run)
            return "verified" if outcome == "already_closed" else outcome
        if row.phase == "claimed" and (
                row.lease_until is not None and row.lease_until > now):
            # A live claim belongs to the executor that owns it, even when this
            # machine happens to be the same machine id.
            return "live_claim"
        if row.dispatch_state in ("dispatched", "unknown"):
            steps = list_steps(session, run.organization_id, run.store_id, run.id)
            actor = _machine_actor(ctx.principal)
            for step in steps:
                if step.state == StepState.RUNNING.value:
                    transition_step(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id, step_id=step.id,
                        expected_run_version=run.version,
                        expected_step_version=step.version,
                        change=StepTransition(to_state=StepState.UNKNOWN,
                            error_code="agent_worker_unknown"))
                    run = get_run(session, run.organization_id, run.store_id, run.id)
                if step.kind == "model" and step.state == StepState.UNKNOWN.value:
                    cost_ledger.mark_cost_unknown(session, actor, run, step,
                        reason="agent_worker_unknown", now=now, commit=False)
            if run.state == RunState.RUNNING.value:
                transition_run(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id,
                    expected_version=run.version, to_state=RunState.FAILED)
            repository._cas_execution(session, row, values={
                "phase": "unknown", "owner_machine_id": None, "lease_until": None,
                "dispatch_state": "unknown", "effect_status": "external_unknown",
            }, expected_epoch=row.epoch)
            repository._close_executing_confirmation(
                session, run.id, dispatched=True, now=now)
            record_event(session, actor_id=ctx.principal.subject_id,
                organization_id=run.organization_id, store_id=run.store_id,
                action="agent_run.worker_reconciled", target_type="agent_run",
                target_id=run.id,
                idempotency_key=f"agent_worker_reconciled:{run.id}",
                details={"machine_id": ctx.principal.machine_id})
            session.commit()
            return "unknown_preserved"
        if row.dispatch_state == "settled" and (
                row.lease_until is not None and row.lease_until <= now):
            if run.state == RunState.RUNNING.value:
                transition_run(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id,
                    expected_version=run.version, to_state=RunState.FAILED)
            repository._cas_execution(session, row, values={
                "phase": "finished", "owner_machine_id": None, "lease_until": None,
                "dispatch_state": "settled", "effect_status": "none",
            }, expected_epoch=row.epoch)
            repository._close_executing_confirmation(
                session, run.id, dispatched=False, now=now)
            session.commit()
            return "interrupted"
        return "verified"



def record_pi_known_model_after_denial(session_factory, machine_token: str,
                                       claim: AgentClaim, *, pending: dict,
                                       usage: dict, output_ref: str) -> None:
    """Accounting-only recovery for one known physical response after denial.

    The caller proved the original failed guard transaction rolled back and
    never attempted commit. No employee/agent or new execution grant is made.
    A changed owner/epoch/Step/version or ledger is unresolved, not retried.
    This does not close or resume: the caller performs its single normal A
    disposition after this transaction has returned. Any commit/close exception
    is propagated without a second accounting attempt.
    """
    if (type(claim) is not AgentClaim or claim.runtime_identity != repository.SDK_RUNTIME
            or type(usage) is not dict or set(usage) != {"input", "output"}
            or any(type(v) is not int or v < 0 for v in usage.values())
            or not isinstance(output_ref, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", output_ref) is None):
        _fail("agent_pi_result_invalid")
    with session_factory() as session:
        ctx = guard_recovery(session, machine_token, organization_id=claim.organization_id,
                             store_id=claim.store_id, run_id=claim.run_id,
                             expected_runtime=repository.SDK_RUNTIME)
        row, run = ctx.execution, ctx.run
        repository.validate_pi_runtime_record(row.frozen_context.get("pi_runtime_record"))
        binding = row.frozen_context.get("pi_readonly_binding")
        if (type(binding) is not dict or binding.get("profile") != repository.pi_record_tool_profile(row.frozen_context.get("pi_runtime_record"))
                or row.frozen_context.get("toolset_version") != run.toolset_version
                or "sha256:" + str(repository._canonical_digest(binding)) != run.toolset_version
                or (binding.get("run_id"), binding.get("organization_id"), binding.get("store_id"),
                    binding.get("requester_id"))
                != (run.id, run.organization_id, run.store_id, run.requester_id)):
            _fail("agent_pi_profile_invalid")
        if (ctx.principal.machine_id != claim.machine_id or row.owner_machine_id != claim.machine_id
                or row.epoch != claim.epoch or row.phase != "claimed"
                or row.dispatch_state != "dispatched" or row.cancel_requested_at is not None
                or run.state != "running" or run.version != pending["run_version"]):
            _fail("agent_execution_fenced")
        steps = list_steps(session, run.organization_id, run.store_id, run.id)
        step = steps[-1] if steps else None
        if (step is None or step.id != pending["step_id"] or step.kind != "model"
                or step.state != "running" or step.version != pending["step_version"]):
            _fail("agent_pi_result_invalid")
        ledgers = list(session.scalars(select(CostLedger).where(
            CostLedger.organization_id == run.organization_id,
            CostLedger.store_id == run.store_id, CostLedger.run_id == run.id,
            CostLedger.step_id == step.id)))
        if (len(ledgers) != 1 or ledgers[0].status != "reserved"
                or ledgers[0].call_key != f"{run.id}:{step.sequence_no}"
                or (ledgers[0].requester_id, ledgers[0].provider_key, ledgers[0].model)
                != (run.requester_id, run.provider_key, run.model)):
            _fail("agent_pi_result_invalid")
        ledger = cost_ledger.settle_cost(session, _machine_actor(ctx.principal), run, step,
            input_tokens=usage["input"], output_tokens=usage["output"], now=ctx.now, commit=False)
        if ledger is None or ledger.status not in ("settled", "reconciliation_required"):
            _fail("agent_pi_usage_unsettled")
        transition_step(session, organization_id=run.organization_id, store_id=run.store_id,
            run_id=run.id, step_id=step.id, expected_run_version=run.version,
            expected_step_version=step.version, change=StepTransition(to_state=StepState.SUCCEEDED,
                input_tokens=usage["input"], output_tokens=usage["output"], output_ref=output_ref), now=ctx.now)
        repository._cas_execution(session, row, values={"dispatch_state": "settled"}, expected_epoch=claim.epoch)
        session.commit()
