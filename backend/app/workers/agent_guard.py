"""Shared execution/recovery guards for the real Worker (AGT-026 B2c).

These helpers ONLY take locks and re-verify durable authorization inside the
caller's transaction. They never commit or roll back, never mint or consume a
human approval grant, never call a Provider or a tool, never claim work, and
never change authorization epochs or control state. Every real Provider call
belongs after the caller commits/leaves this transaction.

The employee lock order from ``backend.app.modules.identity.command_guard`` is
preserved: ``ScopeSecurityFence -> Store -> EmployeeAccount -> LoginSession ->
AgentExecution -> AgentRun -> AgentStep``. The shared fence is held for the
caller's whole transaction; it is never upgraded to exclusive here. SQLite has
no shared row locks, so this test path proves call order and fail-closed
revalidation only, not PostgreSQL concurrency or deadlock freedom.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.assistant.principal import WorkPrincipal
from backend.app.modules.agents import (
    answer_receipts, repository, service as agent_service, skill_service,
)
from backend.app.modules.agents.models import (
    AgentConversation, AgentMessage, AgentRun, AgentStep,
)
from backend.app.modules.agents.repository import (
    AgentClaim, AgentPersistenceError, execution_for, require_worker_claim,
)
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.identity.command_guard import (
    ScopeSecurityFenceError, lock_scope_security_fence,
)
from backend.app.modules.identity.machine_service import (
    MachinePrincipal, authenticate_machine_token, authorize_machine_action,
)
from backend.app.modules.identity.models import Store, Subject
from backend.app.modules.identity.service import authorize_scope

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z", re.ASCII)
_EXECUTE_ACTION = "worker.agent.execute"
_RECOVER_ACTION = "worker.agent.recover"
_TERMINAL_STEP_STATES = ("unknown", "cancelled", "failed")


@dataclass(frozen=True)
class ExecutionContext:
    """Real locked identities for one claimed attempt; never carries the token."""

    principal: MachinePrincipal
    execution: object
    run: AgentRun
    person: AuthenticatedSubject | WorkPrincipal
    agent: AuthenticatedSubject
    step: AgentStep | None
    now: datetime


@dataclass(frozen=True)
class RecoveryContext:
    """Locked row snapshot for reconciliation; deliberately has NO person/agent."""

    principal: MachinePrincipal
    execution: object
    run: AgentRun
    now: datetime


def _fail(code: str) -> None:
    raise AgentPersistenceError(code)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _strict_positive(value: object) -> bool:
    # ``bool`` is an ``int`` subclass; expected versions must be real ints.
    return type(value) is int and value > 0


def _machine(session: Session, token: str,
             now: datetime | None = None) -> MachinePrincipal:
    if not isinstance(token, str) or not token:
        _fail("agent_guard_invalid_claim")
    try:
        # ``now`` is always the DB clock when supplied; the host clock is never
        # allowed to decide machine expiry after a lock wait.
        return authenticate_machine_token(session, token, now=now)
    except HTTPException as error:
        raise AgentPersistenceError("agent_guard_machine_denied") from error


def _require_scope(principal: MachinePrincipal, organization_id: str,
                   store_id: str, machine_id: str | None = None) -> None:
    if (principal.purpose != "worker"
            or (principal.organization_id, principal.store_id)
            != (organization_id, store_id)):
        _fail("agent_guard_scope_denied")
    if machine_id is not None and principal.machine_id != machine_id:
        _fail("agent_guard_scope_denied")


def _shared_fence(session: Session, organization_id: str, store_id: str) -> int:
    """Read the current scope epoch under the SAME shared fence; never upgrade."""
    try:
        epoch = lock_scope_security_fence(session, organization_id, store_id,
                                          exclusive=False)
    except ScopeSecurityFenceError as error:
        raise AgentPersistenceError("agent_execution_authorization_unknown") from error
    if type(epoch) is not int or epoch <= 0:
        # NULL/bool/zero epochs are damaged authorization, never epoch 1.
        _fail("agent_execution_authorization_unknown")
    return epoch


def _lock_store(session: Session, organization_id: str, store_id: str) -> None:
    with session.no_autoflush:
        changed = session.execute(update(Store).where(
            Store.organization_id == organization_id,
            Store.id == store_id,
            Store.active.is_(True),
        ).values(active=Store.active).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        _fail("agent_guard_store_unavailable")


def _lock_run(session: Session, organization_id: str, store_id: str,
              run_id: str) -> AgentRun:
    run = session.scalar(repository._scoped_run_query(
        organization_id, store_id, run_id).with_for_update())
    if run is None:
        _fail("agent_guard_execution_not_found")
    return run


def _lock_step(session: Session, organization_id: str, store_id: str,
               run_id: str, step_id: str) -> AgentStep:
    step = session.scalar(select(AgentStep).where(
        AgentStep.organization_id == organization_id,
        AgentStep.store_id == store_id,
        AgentStep.run_id == run_id,
        AgentStep.id == step_id,
    ).with_for_update().execution_options(populate_existing=True))
    if step is None or step.state in _TERMINAL_STEP_STATES:
        _fail("agent_guard_step_invalid")
    return step


def _real_person(session: Session, actor: AuthenticatedSubject,
                 execution, run: AgentRun,
                 now: datetime) -> AuthenticatedSubject:
    """Revalidate the locked employee identity; the actor is DB-derived only.

    The account/session rows are already locked by ``lock_employee_authority``,
    so the shared repository re-read cannot invert the lock order; it adds the
    lockout/version/session-expiry checks the command guard does not own.
    """
    if type(actor) is WorkPrincipal:
        from backend.app.modules.assistant import principal
        try:
            if principal.from_execution(session, execution, run) != actor:
                _fail('agent_guard_person_denied')
            principal.check(session, actor, now=now)
        except HTTPException as error:
            raise AgentPersistenceError('agent_guard_person_denied') from error
        return actor
    try:
        validate_actor_session(session, actor, now=now)
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission="agents.run", now=now)
    except HTTPException as error:
        raise AgentPersistenceError("agent_guard_person_denied") from error
    if not repository._live_person(session, execution, now,
                                   subject_id=run.requester_id):
        _fail("agent_guard_person_denied")
    kind = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id,
        Subject.id == actor.subject_id,
    ).execution_options(populate_existing=True))
    if kind != "person":
        _fail("agent_guard_person_denied")
    return actor


def _frozen_context(session: Session, execution, run: AgentRun,
                    actor: AuthenticatedSubject) -> list[dict]:
    """Validate the exact frozen prompt/evidence; never add a new message.

    The submitter hashes the trimmed user text (``RunCreate.input_ref``) and
    stores the trimmed text as the origin message, so the digest of that
    message must equal both frozen values. Missing/empty evidence fails shut;
    there is no default that could hide a damaged context.
    """
    frozen = execution.frozen_context
    # NOTE: ``toolset_version`` is only compared with the Run's own frozen pin.
    # This guard does NOT prove the current tool descriptions/skill release still
    # hash to that pin; the executor must recompute the toolset digest and skill
    # pin from the live descriptions before it acts on the context.
    if not isinstance(frozen, dict):
        _fail("agent_guard_context_invalid")
    conversation_id = frozen.get("conversation_id")
    evidence = frozen.get("evidence")
    context_ids = frozen.get("context_message_ids")
    toolset_version = frozen.get("toolset_version")
    summary_version = frozen.get("summary_version")
    if (not isinstance(conversation_id, str) or not conversation_id
            or not isinstance(evidence, list) or not evidence
            or not isinstance(context_ids, list)
            or not isinstance(toolset_version, str)
            or toolset_version != run.toolset_version
            or type(summary_version) is not int or summary_version < 0
            or not isinstance(execution.origin_message_id, str)
            or not isinstance(execution.input_digest, str)):
        _fail("agent_guard_context_invalid")
    conversation = session.scalar(select(AgentConversation).where(
        AgentConversation.organization_id == execution.organization_id,
        AgentConversation.store_id == execution.store_id,
        AgentConversation.id == conversation_id,
    ).execution_options(populate_existing=True))
    if (conversation is None or conversation.status != "active"
            or conversation.subject_id != run.requester_id):
        _fail("agent_guard_context_invalid")
    origin = session.scalar(select(AgentMessage).where(
        AgentMessage.organization_id == execution.organization_id,
        AgentMessage.store_id == execution.store_id,
        AgentMessage.id == execution.origin_message_id,
    ).execution_options(populate_existing=True))
    if (origin is None or origin.conversation_id != conversation.id
            or origin.role != "user" or origin.state != "active"):
        _fail("agent_guard_context_invalid")
    digest = sha256(origin.content.encode("utf-8")).hexdigest()
    if (not run.input_ref.startswith("sha256:")
            or run.input_ref != "sha256:" + digest
            or execution.input_digest != digest):
        _fail("agent_guard_context_invalid")
    # Summary versions are frozen evidence too: a >0 pin must still equal the
    # real conversation summary, never silently swap to a newer one.
    if summary_version > 0 and conversation.summary_version != summary_version:
        _fail("agent_guard_context_invalid")
    references = [ref for ref in evidence
                  if isinstance(ref, dict) and ref.get("kind") == "message"]
    if not any(ref.get("id") == origin.id
               and ref.get("version") == origin.version
               and ref.get("conversation_id") == conversation.id
               for ref in references):
        _fail("agent_guard_context_invalid")
    for message_id in context_ids:
        if not isinstance(message_id, str) or not message_id:
            _fail("agent_guard_context_invalid")
        message = session.scalar(select(AgentMessage).where(
            AgentMessage.organization_id == execution.organization_id,
            AgentMessage.store_id == execution.store_id,
            AgentMessage.conversation_id == conversation.id,
            AgentMessage.id == message_id,
        ).execution_options(populate_existing=True))
        if message is None or message.state != "active":
            _fail("agent_guard_context_invalid")
        # Every frozen history message needs its own versioned evidence ref, so
        # a newly built prompt can never read an unversioned old history row.
        if not any(ref.get("id") == message.id
                   and ref.get("version") == message.version
                   and ref.get("conversation_id") == conversation.id
                   for ref in references):
            _fail("agent_guard_context_invalid")
    return evidence


def _merged_evidence(session: Session, actor: AuthenticatedSubject,
                     evidence: list[dict],
                     additional_evidence: object) -> list[dict]:
    extra = [] if additional_evidence is None else additional_evidence
    if not isinstance(extra, list) or any(not isinstance(item, dict) for item in extra):
        _fail("agent_guard_evidence_invalid")
    try:
        merged = answer_receipts.merge_evidence(evidence, extra)
        valid, _ = answer_receipts.validate_evidence(session, actor, merged)
    except (answer_receipts.EvidenceError, HTTPException) as error:
        raise AgentPersistenceError("agent_guard_evidence_invalid") from error
    if not valid:
        _fail("agent_guard_evidence_invalid")
    return merged


def guard_execution(session: Session, machine_token: str, claim: AgentClaim, *,
                    step_id: str | None = None,
                    expected_run_version: int | None = None,
                    expected_step_version: int | None = None,
                    additional_evidence: list[dict] | None = None,
                    expected_runtime: tuple = repository.LEGACY_RUNTIME,
                    ) -> ExecutionContext:
    """Lock and revalidate one claimed attempt for execute-only work.

    Pre-reads only locate the machine scope, bound employee and old epoch; all
    durable decisions are made after the locks with the database clock. The
    caller owns the transaction and MUST keep the shared fence until commit.
    """
    repository.validate_runtime_identity(expected_runtime)
    repository._reject_runtime_mutations(session)
    if isinstance(claim, AgentClaim) and claim.runtime_identity != expected_runtime:
        _fail("agent_runtime_mismatch")
    if not isinstance(claim, AgentClaim) or not _valid_id(claim.run_id):
        _fail("agent_guard_invalid_claim")
    organization_id, store_id, run_id = (
        claim.organization_id, claim.store_id, claim.run_id)
    if not (_valid_id(organization_id) and _valid_id(store_id)
            and _valid_id(claim.machine_id) and _strict_positive(claim.epoch)):
        _fail("agent_guard_invalid_claim")
    if step_id is not None and not _valid_id(step_id):
        _fail("agent_guard_step_invalid")
    for value in (expected_run_version, expected_step_version):
        if value is not None and not _strict_positive(value):
            _fail("agent_guard_version_mismatch")
    if expected_step_version is not None and step_id is None:
        _fail("agent_guard_step_invalid")

    # Pre-read: NO decision is taken from these rows.
    principal = _machine(session, machine_token)
    _require_scope(principal, organization_id, store_id, claim.machine_id)
    pre = execution_for(session, organization_id, store_id, run_id)
    if pre is None:
        _fail("agent_guard_execution_not_found")
    pre_run = repository._load_run(session, organization_id, store_id, run_id)
    if pre_run is None:
        _fail("agent_guard_execution_not_found")
    if not isinstance(pre.frozen_context, dict):
        _fail("agent_guard_context_invalid")
    from backend.app.modules.assistant import principal as work_principal
    try:
        delegated = work_principal.from_execution(session, pre, pre_run)
    except HTTPException as error:
        raise AgentPersistenceError('agent_guard_person_denied') from error
    actor = delegated or AuthenticatedSubject(
        pre_run.requester_id, organization_id, store_id,
        pre.authorization_expires_at,
        account_id=pre.account_id, session_id=pre.session_id,
        account_version=pre.account_version)

    # Order (shared fence, Store, Account, Session) comes from the existing
    # command guard; it also drops snapshots taken before the fence.
    from backend.app.modules.identity.command_guard import lock_employee_authority
    try:
        if delegated is not None:
            work_principal.check(session, delegated, lock=True)
        else:
            lock_employee_authority(session, actor)
    except HTTPException as error:
        raise AgentPersistenceError("agent_guard_person_denied") from error

    execution = execution_for(session, organization_id, store_id, run_id,
                              for_update=True)
    if execution is None:
        _fail("agent_guard_execution_not_found")
    if not isinstance(execution.frozen_context, dict):
        _fail("agent_guard_context_invalid")
    execution = require_worker_claim(session, claim, expected_runtime=expected_runtime)
    run = _lock_run(session, organization_id, store_id, run_id)
    step = (_lock_step(session, organization_id, store_id, run_id, step_id)
            if step_id is not None else None)

    # The locked row is authoritative. If the pre-read binding is not exactly
    # the locked one, reject instead of rebuilding a person from the new
    # snapshot (which would bypass the account/session lock we actually hold).
    if (execution.account_id != actor.account_id
            or execution.session_id != actor.session_id
            or execution.account_version != actor.account_version
            or execution.authorization_expires_at != actor.auth_expires_at
            or run.requester_id != actor.subject_id):
        _fail("agent_guard_person_denied")
    if type(execution.version) is not int or execution.version <= 0:
        _fail("agent_execution_fenced")

    # All lock domains are held: only now read the DB clock and the epoch.
    now = repository._db_clock(session)
    epoch = _shared_fence(session, organization_id, store_id)
    if delegated is not None:
        if (work_principal.from_execution(session, execution, run) != delegated
                or execution.approval_grant is not None):
            _fail('agent_guard_person_denied')
        work_principal.check(session, delegated, now=now)
        epoch = delegated.work_epoch

    # Machine expiry must be judged by the same DB clock AFTER lock waits.
    principal = _machine(session, machine_token, now)
    _require_scope(principal, organization_id, store_id, claim.machine_id)
    try:
        authorize_machine_action(session, principal,
            organization_id=organization_id, store_id=store_id,
            action=_EXECUTE_ACTION, now=now)
    except HTTPException as error:
        raise AgentPersistenceError("agent_guard_machine_denied") from error

    if execution.authorization_epoch is None or execution.authorization_epoch != epoch:
        _fail("agent_execution_authorization_unknown")
    if (execution.epoch != claim.epoch
            or execution.mode != "worker_v2"
            or execution.phase != "claimed"
            or execution.owner_machine_id != principal.machine_id
            or execution.lease_until is None or execution.lease_until <= now
            or execution.authorization_expires_at is None
            or execution.authorization_expires_at <= now
            or execution.cancel_requested_at is not None):
        _fail("agent_execution_fenced")
    if run.deadline_at is None or run.deadline_at <= now:
        _fail("agent_execution_fenced")
    if execution.dispatch_state == "unknown":
        # An unknown dispatch is never replayed by this guard; the executor must
        # close it through its own reconciliation path.
        _fail("agent_execution_fenced")
    if run.state not in ("running", "created"):
        _fail("agent_execution_fenced")
    if run.state == "created" and (execution.dispatch_state != "not_dispatched"
                                   or execution.approval_grant is not None):
        _fail("agent_execution_fenced")
    # A live RUNNING claim may loop over ordinary model/read-tool steps without
    # a human grant; approved write tools still require the executor to consume
    # the frozen grant through ``consume_approval_grant`` itself. This guard
    # never signs, consumes or renews a grant.
    if expected_run_version is not None and run.version != expected_run_version:
        _fail("agent_guard_version_mismatch")
    if expected_step_version is not None and step.version != expected_step_version:
        _fail("agent_guard_version_mismatch")

    person = _real_person(session, actor, execution, run, now)
    try:
        agent = agent_service._agent_actor(session, person,
                                           execution.authorization_expires_at)
    except (agent_service.AgentRuntimeError, HTTPException) as error:
        raise AgentPersistenceError("agent_guard_agent_denied") from error
    try:
        skill_service.verify_runtime_binding(session, person, agent, run)
    except (skill_service.SkillReleaseError, HTTPException) as error:
        raise AgentPersistenceError("agent_guard_skill_denied") from error

    evidence = _frozen_context(session, execution, run, person)
    _merged_evidence(session, person, evidence, additional_evidence)
    return ExecutionContext(principal=principal, execution=execution, run=run,
                            person=person, agent=agent, step=step, now=now)


def guard_recovery(session: Session, machine_token: str, *,
                   organization_id: str, store_id: str, run_id: str,
                   expected_runtime: tuple = repository.LEGACY_RUNTIME,
                   ) -> RecoveryContext:
    """Lock and revalidate one attempt snapshot for reconciliation only.

    The person may be logged out and the epoch/lease may be stale: recovery
    still returns no person or Agent identity and grants no execution. Another
    current worker of the same scope may inspect the old owner; revoked,
    cross-store and non-worker machines are rejected.
    """
    repository.validate_runtime_identity(expected_runtime)
    repository._reject_runtime_mutations(session)
    if not (_valid_id(organization_id) and _valid_id(store_id)
            and _valid_id(run_id)):
        _fail("agent_guard_recovery_invalid")
    principal = _machine(session, machine_token)
    _require_scope(principal, organization_id, store_id)

    # Shared fence first; recovery NEVER advances it or the attempt epoch.
    _shared_fence(session, organization_id, store_id)
    _lock_store(session, organization_id, store_id)
    execution = execution_for(session, organization_id, store_id, run_id,
                              for_update=True)
    if execution is None or execution.mode != "worker_v2":
        _fail("agent_guard_execution_not_found")
    if repository.runtime_identity(execution.frozen_context) != expected_runtime:
        _fail("agent_runtime_mismatch")
    run = _lock_run(session, organization_id, store_id, run_id)
    now = repository._db_clock(session)

    principal = _machine(session, machine_token, now)
    _require_scope(principal, organization_id, store_id)
    try:
        authorize_machine_action(session, principal,
            organization_id=organization_id, store_id=store_id,
            action=_RECOVER_ACTION, now=now)
    except HTTPException as error:
        raise AgentPersistenceError("agent_guard_machine_denied") from error
    return RecoveryContext(principal=principal, execution=execution,
                           run=run, now=now)
