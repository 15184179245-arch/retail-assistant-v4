"""Durable one-shot intake -> formal Pi execution, without HTTP lifetime coupling.

New explicitly granted work uses its exact WorkPrincipal; legacy work retains
the original employee session checks. Neither path creates a background login. A
durably launching intent is NEVER automatically submitted or spawned again.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event

from fastapi import HTTPException
from sqlalchemy import select, or_, and_

from backend.app.application import commit_guard
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import repository, runtime_http
from backend.app.modules.agents import service as agents
from backend.app.modules.agents.models import AgentRun, AgentExecution, AgentStep, CostLedger
from backend.app.modules.assistant.models import AssistantDispatch, AssistantWork, AssistantTurn, AssistantTurnDispatch
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.identity import machine_service
from backend.app.modules.identity.command_guard import lock_command_authority, lock_scope_security_fence, check_command_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.workers.outbox import OutboxEvent
from backend.app.modules.assistant.online import AssistantOnlinePolicy
from backend.app.modules.assistant.authority import WorkAuthorityPolicy
from backend.app.modules.assistant.progress_adapter import DshProgressAdapter
from backend.app.modules.assistant.turn_adapter import DshTurnsAdapter


@dataclass(frozen=True)
class AssistantWorkerConfig:
    pi: runtime_http.PiHttpConfig
    memory: bool = True
    online_policy: AssistantOnlinePolicy | None = None
    work_authority_enabled: bool = False
    progress: DshProgressAdapter | None = None
    turns: DshTurnsAdapter | None = None
    inbox_policy: WorkAuthorityPolicy | None = None
    discovery: bool = False
    source: bool = False


def _config(config):
    if type(config) is not AssistantWorkerConfig or type(config.memory) is not bool or type(config.pi) is not runtime_http.PiHttpConfig:
        raise ValueError('assistant_worker_configuration_invalid')
    if type(config.work_authority_enabled) is not bool:
        raise ValueError('assistant_worker_configuration_invalid')
    if config.progress is not None and type(config.progress) is not DshProgressAdapter:
        raise ValueError('assistant_worker_configuration_invalid')
    if config.turns is not None and (type(config.turns) is not DshTurnsAdapter or not config.work_authority_enabled):
        raise ValueError('assistant_worker_configuration_invalid')
    if config.online_policy is not None and (type(config.online_policy) is not AssistantOnlinePolicy or not config.memory):
        raise ValueError('assistant_worker_configuration_invalid')

    if (type(config.source) is not bool or config.source and (not config.discovery or not config.work_authority_enabled)
            or config.online_policy is not None and config.source != config.online_policy.source_enabled
            or type(config.discovery) is not bool or config.discovery and not config.memory
            or config.online_policy is not None and config.discovery != config.online_policy.discovery_enabled):
        raise ValueError('assistant_worker_configuration_invalid')

    if config.inbox_policy is not None:
        from backend.app.modules.assistant.authority import WorkAuthorityPolicy
        from backend.app.modules.assistant.inbox import REQUIRED
        from backend.app.modules.assistant.principal import CAPABILITIES
        policy = config.inbox_policy
        if (type(policy) is not WorkAuthorityPolicy or config.turns is None
                or type(policy.permissions) is not tuple or any(type(p) is not str for p in policy.permissions)
                or len(policy.permissions) != len(set(policy.permissions))
                or not REQUIRED <= set(policy.permissions) <= CAPABILITIES
                or type(policy.expires_at) is not datetime or policy.expires_at.tzinfo is None
                or config.online_policy is not None and policy.expires_at > config.online_policy.expires_at):
            raise ValueError('assistant_inbox_policy_invalid')


def _machine(db, config):
    pi = config.pi
    principal = machine_service.authenticate_machine_token(db, pi.machine_token)
    if principal.purpose != 'worker' or (principal.organization_id, principal.store_id) != (pi.organization_id, pi.store_id):
        raise HTTPException(403, 'assistant_worker_scope_denied')
    lock_scope_security_fence(db, pi.organization_id, pi.store_id)
    now = commit_guard._db_clock(db)
    principal = machine_service.authenticate_machine_token(db, pi.machine_token, now=now)
    for action in ('worker.agent.execute', 'worker.agent.recover'):
        machine_service.authorize_machine_action(db, principal, organization_id=pi.organization_id,
            store_id=pi.store_id, action=action, now=now)
    return principal, now


def _machine_commit(db, config):
    # Recheck at the actual commit without changing lock order.
    def check(session, now):
        principal = machine_service.authenticate_machine_token(session, config.pi.machine_token, now=now)
        for action in ('worker.agent.execute', 'worker.agent.recover'):
            machine_service.authorize_machine_action(session, principal,
                organization_id=config.pi.organization_id, store_id=config.pi.store_id, action=action, now=now)
    commit_guard.enlist(db, action='assistant.dispatch', check=check)


def _kind(work):
    return 'turn' if type(work) is AssistantTurn else 'work'


def _models(kind):
    if kind=='work':return AssistantWork,AssistantDispatch,AssistantWork.task_id
    if kind=='turn':return AssistantTurn,AssistantTurnDispatch,AssistantTurn.id
    raise ValueError('assistant_worker_kind_invalid')


def _event_id(work,dispatch):
    return dispatch.outbox_event_id if _kind(work)=='turn' else work.outbox_event_id


def _work(db, config, work_id, kind='work'):
    model,intent,identity=_models(kind)
    work = db.scalar(select(model).where(identity == work_id,
        model.organization_id == config.pi.organization_id, model.store_id == config.pi.store_id))
    if work is None:
        raise HTTPException(404, 'assistant_work_not_found')
    if kind=='turn' and (config.turns is None or work.manifest_sha256!=config.turns.config.manifest_sha256):
        raise HTTPException(409,'assistant_turn_source_conflict')
    dispatch = db.get(intent, work_id)
    if dispatch is None:
        raise HTTPException(409, 'assistant_dispatch_authority_unrecorded')
    return work, dispatch


def _actor(work, dispatch):
    # Provenance captured at authenticated intake, never an employee token.
    return AuthenticatedSubject(work.owner_id, work.organization_id, work.store_id,
        dispatch.authorization_expires_at, account_id=work.origin_account_id,
        session_id=dispatch.session_id, account_version=dispatch.account_version)


def _binding(db, work, dispatch):
    if _kind(work)=='turn':
        from backend.app.modules.assistant.turn_execution import binding
        return binding(db,work,dispatch)
    from backend.app.modules.assistant import service
    return service._view(db, _actor(work, dispatch), work, include_execution=False)


def _authorized(db, work, dispatch):
    from backend.app.modules.assistant import service
    from backend.app.modules.assistant import principal
    delegated = principal.for_work(db, work.task_id, kind=_kind(work))
    if delegated is not None:
        if delegated.run_id != dispatch.run_id:
            raise HTTPException(403, 'assistant_work_principal_denied')
        return delegated, _binding(db, work, dispatch)
    if _kind(work)=='turn':raise HTTPException(403,'assistant_turn_authority_required')
    actor = _actor(work, dispatch)
    epoch = lock_command_authority(db, actor)
    validate_actor_session(db, actor)
    for permission in ('agents.run', 'tasks.manage'):
        authorize_scope(db, actor, organization_id=work.organization_id,
            store_id=work.store_id, permission=permission)
    def check(session, now):
        for permission in ('agents.run', 'tasks.manage'):
            check_command_authority(session, actor, permission=permission, epoch=epoch, now=now)
    commit_guard.enlist(db, action='assistant.dispatch_authority', check=check)
    view = _binding(db, work, dispatch)
    if view['task_state'] not in ('accepted', 'assigned', 'in_progress'):
        raise HTTPException(409, 'assistant_work_not_active')
    return actor, view


def _bound_run(db, work, dispatch):
    run = db.scalar(select(AgentRun).where(AgentRun.id == dispatch.run_id))
    if run is None:
        return None
    execution = db.get(AgentExecution, run.id)
    from backend.app.modules.tasks.models import Task
    from hashlib import sha256
    text = _binding(db,work,dispatch)['text']
    if (execution is None or dispatch.runtime_record is None
            or (run.organization_id, run.store_id, run.requester_id) != (work.organization_id, work.store_id, work.owner_id)
            or run.input_ref != 'sha256:' + sha256(text.strip().encode()).hexdigest()
            or (execution.account_id, execution.session_id, execution.account_version) !=
               (work.origin_account_id, dispatch.session_id, dispatch.account_version)
            or type(execution.frozen_context) is not dict
            or execution.frozen_context.get('pi_runtime_record') != dispatch.runtime_record
            or repository.runtime_identity(execution.frozen_context) != repository.SDK_RUNTIME):
        raise HTTPException(409, 'assistant_run_binding_conflict')
    from backend.app.modules.assistant.principal import from_execution
    from_execution(db, execution, run)
    return run


def execution_view(db, actor, work):
    dispatch = db.get(_models(_kind(work))[1], work.task_id)
    if dispatch is None:
        return {'execution_status': 'authority_unrecorded', 'run_id': None, 'run_receipt': None}
    run = _bound_run(db, work, dispatch)
    status = {'pending': 'queued', 'launching': 'starting', 'settled': 'dispatch_unverified',
              'denied': 'blocked', 'unknown': 'needs_attention'}[dispatch.state]
    from backend.app.modules.assistant.control import PRELAUNCH_STOP
    if dispatch.state == 'denied' and dispatch.code == PRELAUNCH_STOP and run is None:
        status = 'cancelled'
    receipt = agents.read_run_receipt(db, actor, run.id) if run is not None else None
    if receipt is not None:
        status = receipt['state']
    return {'execution_status': status, 'dispatch_state': dispatch.state,
            'run_id': run.id if run is not None else None, 'run_receipt': receipt,
            'dispatch_code': dispatch.code}


def _audit(db, principal, work, dispatch, action, code):
    record_event(db, actor_id=principal.subject_id, organization_id=work.organization_id,
        store_id=work.store_id, action=action, target_type='assistant_turn' if _kind(work)=='turn' else 'task', target_id=work.task_id,
        idempotency_key=f'{action}:{work.task_id}', details={'run_id': dispatch.run_id, 'code': code})


def _deny(config, work_id, code, *, kind='work'):
    """Machine records a prelaunch failure. No employee operation or replay."""
    with config.pi.session_factory.begin() as db:
        principal, now = _machine(db, config)
        work, dispatch = _work(db, config, work_id, kind)
        intent=_models(kind)[1]
        dispatch = db.scalar(select(intent).where(intent.work_id == work_id)
                             .with_for_update().execution_options(populate_existing=True))
        if dispatch.state != 'pending':
            return 'preserved'
        _binding(db, work, dispatch)
        dispatch.state, dispatch.code, dispatch.finished_at = 'denied', code, now
        event = db.get(OutboxEvent, _event_id(work,dispatch))
        event.status, event.last_error = 'dead', code
        _audit(db, principal, work, dispatch, 'assistant.dispatch_denied', code)
        if kind=='turn':
            from backend.app.modules.assistant.turn_execution import settle
            settle(db,config,work,dispatch)
        elif config.progress is not None:
            from backend.app.modules.assistant.progress import record
            record(db, config, principal, work, dispatch, now)
        _machine_commit(db, config)
    return 'blocked'


def _finish(config, work_id, *, recovery=False, kind='work'):
    """Read back exact Host facts; never calls Pi/Provider or retries a launch."""
    with config.pi.session_factory.begin() as db:
        principal, now = _machine(db, config)
        work, dispatch = _work(db, config, work_id, kind)
        intent=_models(kind)[1]
        dispatch = db.scalar(select(intent).where(intent.work_id == work_id)
                             .with_for_update().execution_options(populate_existing=True))
        if dispatch.state != 'launching':
            return 'preserved'
        if recovery and (dispatch.reconcile_after is None or now < dispatch.reconcile_after):
            return 'waiting'
        _binding(db, work, dispatch)
        try:
            run = _bound_run(db, work, dispatch)
        except HTTPException:
            run = None
        terminal = run is not None and run.state in ('completed', 'failed', 'cancelled')
        dispatch.state = 'settled' if terminal else 'unknown'
        dispatch.code = 'PI_RUN_RECORDED' if terminal else 'PI_LAUNCH_OUTCOME_UNRESOLVED'
        dispatch.finished_at = now
        event = db.get(OutboxEvent, _event_id(work,dispatch))
        event.lease_owner = event.lease_until = None
        event.status = 'delivered' if terminal else 'dead'
        event.external_receipt = 'agent-run:' + run.id if terminal else None
        event.delivered_at = now if terminal else None
        event.last_error = None if terminal else dispatch.code
        _audit(db, principal, work, dispatch, 'assistant.dispatch_recorded', dispatch.code)
        if kind=='turn':
            from backend.app.modules.assistant.turn_execution import settle
            settle(db,config,work,dispatch)
        elif config.progress is not None:
            from backend.app.modules.assistant.progress import record
            record(db, config, principal, work, dispatch, now)
        _machine_commit(db, config)
    return 'settled' if terminal else 'unknown'


def _online_waiting(db, config, work_id):
    """Called under the scope fence; serialize live or unresolved dispatches.

    An intent can commit before its first cost row exists. Guard both facts,
    including other owners sharing the store budget. Never repair them here.
    """
    if config.online_policy is None:
        return False
    # A terminal model attempt with unknown usage is not a running worker.
    # Keep its reservation and prohibit continuation of its original work,
    # but allow independently authorized work. Missing/mismatched facts stay
    # blocking; a launching/unknown dispatch still blocks below even if its
    # model Step has already closed.
    closed_model = select(AgentStep.id).join(AgentRun, and_(
        AgentRun.id == AgentStep.run_id,
        AgentRun.organization_id == AgentStep.organization_id,
        AgentRun.store_id == AgentStep.store_id)).where(
            AgentRun.id == CostLedger.run_id,
            AgentRun.organization_id == CostLedger.organization_id,
            AgentRun.store_id == CostLedger.store_id,
            AgentRun.state.in_(('completed', 'failed', 'cancelled')),
            AgentStep.id == CostLedger.step_id,
            AgentStep.kind == 'model', AgentStep.state == 'unknown').exists()
    unsettled = db.scalar(select(CostLedger.id).where(
        CostLedger.organization_id == config.pi.organization_id,
        CostLedger.store_id == config.pi.store_id,
        CostLedger.provider_key == 'opencode-go',
        CostLedger.status.not_in(('settled', 'released')),
        or_(CostLedger.status != 'unknown', ~closed_model)).limit(1))
    intent = db.scalar(select(AssistantDispatch.work_id).join(AssistantWork,
        AssistantWork.task_id == AssistantDispatch.work_id).where(
        AssistantWork.organization_id == config.pi.organization_id,
        AssistantWork.store_id == config.pi.store_id,
        AssistantDispatch.work_id != work_id,
        AssistantDispatch.state.in_(('launching', 'unknown'))).limit(1))
    turn_intent=db.scalar(select(AssistantTurnDispatch.work_id).join(AssistantTurn,
        AssistantTurn.id==AssistantTurnDispatch.work_id).where(AssistantTurn.organization_id==config.pi.organization_id,
        AssistantTurn.store_id==config.pi.store_id,AssistantTurnDispatch.work_id!=work_id,
        AssistantTurnDispatch.state.in_(('launching','unknown'))).limit(1))
    return unsettled is not None or intent is not None or turn_intent is not None


def tick(config):
    """At most one fresh launch or one expired-intent reconciliation per tick."""
    _config(config)
    with config.pi.session_factory() as db:
        _, now = _machine(db, config)
        candidates=[]
        for kind in (('work','turn') if config.turns is not None else ('work',)):
            model,intent,identity=_models(kind)
            owner_filter=([model.owner_id==config.online_policy.requester_id] if config.online_policy is not None else [])
            result=db.execute(select(intent,model.created_at).join(model,identity==intent.work_id).where(
                model.organization_id==config.pi.organization_id,model.store_id==config.pi.store_id,*owner_filter,
                or_(intent.state=='pending',and_(intent.state=='launching',intent.reconcile_after<=now)))
                .order_by(model.created_at,identity).limit(1)).first()
            if result:candidates.append((result[1],result[0].work_id,kind,result[0].state))
        if candidates: _,work_id,kind,state=min(candidates)
    if not candidates:
        if config.inbox_policy is not None:
            from backend.app.modules.assistant.inbox_execution import tick as convert_tick
            return convert_tick(config)
        return 'idle'
    if state == 'launching':
        return _finish(config, work_id, recovery=True, kind=kind)
    try:
        with config.pi.session_factory() as db:
            _machine(db, config)
            work, dispatch = _work(db, config, work_id, kind)
            if dispatch.state != 'pending':
                return 'preserved'
            actor, view = _authorized(db, work, dispatch)
            if _online_waiting(db, config, work_id):
                return 'waiting'
            if db.get(AgentRun, dispatch.run_id) is not None:
                raise HTTPException(409, 'assistant_run_already_exists')
            payload = runtime_http.PiRequest(text=view['text'].strip(), run_id=dispatch.run_id)
        pi = (config.online_policy.for_work(config.pi, actor, payload)
              if config.online_policy is not None else config.pi)
        prepared, private = runtime_http.prepare_pi_submission(pi, actor, payload, memory=config.memory, **({'discovery':True} if config.discovery else {}), **({'source':True} if config.source else {}))
    except Exception:
        return _deny(config, work_id, 'PI_PRELAUNCH_DENIED',kind=kind)
    # The launching intent commits BEFORE any Pi Run/claim/process/provider.
    # If this commit raises, no side effect follows and the caller sees error.
    try:
        with config.pi.session_factory.begin() as db:
            principal, now = _machine(db, config)
            work, dispatch = _work(db, config, work_id, kind)
            if dispatch.state != 'pending':
                return 'preserved'
            actor, fresh = _authorized(db, work, dispatch)
            if _online_waiting(db, config, work_id):
                return 'waiting'
            if fresh['text'].strip() != payload.text or dispatch.run_id != payload.run_id or db.get(AgentRun, payload.run_id) is not None:
                raise HTTPException(409, 'assistant_dispatch_changed')
            event = db.get(OutboxEvent, _event_id(work,dispatch))
            if event.status != 'pending' or event.attempts != 0:
                raise HTTPException(409, 'assistant_dispatch_outbox_conflict')
            dispatch.state, dispatch.machine_id, dispatch.started_at = 'launching', principal.machine_id, now
            dispatch.runtime_record = prepared.runtime_record()
            dispatch.reconcile_after = now + timedelta(minutes=15)
            event.status, event.attempts = 'leased', 1
            event.lease_owner, event.lease_until = principal.machine_id, dispatch.reconcile_after
            _audit(db, principal, work, dispatch, 'assistant.dispatch_started', 'PI_LAUNCH_INTENT')
            _machine_commit(db, config)
    except HTTPException:
        # Known authority/binding denial after preparation: preserve the work
        # as blocked. _deny only touches pending rows; a committed intent can
        # never be reverted, even if a commit reply was lost.
        return _deny(config, work_id, 'PI_PRELAUNCH_DENIED',kind=kind)
    try:
        runtime_http.execute_pi_submission(pi, actor, payload, prepared, private, memory=config.memory, **({'discovery':True} if config.discovery else {}), **({'source':True} if config.source else {}))
    except Exception:
        # The authoritative Run may already exist or have sent a model call.
        # Reconciliation below cannot re-execute it, even after response loss.
        pass
    return _finish(config, work_id,kind=kind)


def run_until_stopped(config, stop: Event, *, idle_seconds=1.0):
    """Opt-in dedicated background loop; never tied to a browser connection."""
    _config(config)
    if not isinstance(stop, Event) or not 0.1 <= idle_seconds <= 60:
        raise ValueError('assistant_worker_loop_invalid')
    while not stop.is_set():
        outcome = tick(config)
        if outcome in ('idle', 'waiting', 'preserved', 'inbox_waiting'):
            stop.wait(idle_seconds)
