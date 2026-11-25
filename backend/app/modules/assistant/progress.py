"""Machine-authorized, one-shot projection of exact Host execution facts.

The intake snapshot and original execution grant remain immutable. This is not
business verification, a new execution, a retry, or a person authorization.
"""
from fastapi import HTTPException
from backend.app.application import commit_guard
from sqlalchemy import select
from backend.app.modules.assistant.core_adapter import digest
from backend.app.modules.assistant.models import AssistantDispatch, AssistantProgress
from backend.app.modules.assistant.progress_adapter import DshProgressAdapter
from backend.app.modules.audit.service import record_event
from backend.app.workers.outbox import OutboxEvent


def _capture(db, work, dispatch):
    from backend.app.modules.assistant import dispatch as worker
    from backend.app.modules.assistant.control import PRELAUNCH_STOP
    view = worker._binding(db, work, dispatch)
    if dispatch.state not in ('settled', 'denied', 'unknown') or dispatch.finished_at is None:
        raise HTTPException(409, 'assistant_progress_not_settled')
    event = db.get(OutboxEvent, work.outbox_event_id)
    run = worker._bound_run(db, work, dispatch)
    state = 'needs_attention'
    if dispatch.state == 'settled':
        if (run is None or run.state not in ('completed','failed','cancelled')
                or event.status != 'delivered' or event.external_receipt != 'agent-run:' + run.id):
            raise HTTPException(409, 'assistant_progress_fact_conflict')
        state = run.state
    elif dispatch.state == 'denied':
        if run is not None or event.status != 'dead':
            raise HTTPException(409, 'assistant_progress_fact_conflict')
        state = 'cancelled' if dispatch.code == PRELAUNCH_STOP else 'blocked'
    elif event.status != 'dead':
        raise HTTPException(409, 'assistant_progress_fact_conflict')
    fact = dict(work_id=work.task_id, run_id=dispatch.run_id, state=state,
        at=dispatch.finished_at.isoformat(), admission_sha256=work.snapshot_sha256,
        request_digest=work.request_digest, dispatch_state=dispatch.state, dispatch_code=dispatch.code,
        runtime_sha256=digest(dispatch.runtime_record), run_state=run.state if run else None,
        run_version=run.version if run else None, outbox_status=event.status,
        outbox_receipt=event.external_receipt)
    return view, fact


def record(db, config, principal, work, dispatch, now):
    """Internal: caller holds machine scope fence and dispatch row lock."""
    adapter = config.progress
    if type(adapter) is not DshProgressAdapter:
        raise HTTPException(503, 'assistant_progress_unavailable')
    if (principal.organization_id, principal.store_id) != (work.organization_id, work.store_id):
        raise HTTPException(403, 'assistant_worker_scope_denied')
    view, fact = _capture(db, work, dispatch)
    state = fact['state']
    row = db.get(AssistantProgress, work.task_id)
    if row is not None:
        if (row.run_id != dispatch.run_id or row.state != state or row.fact != fact
                or row.fact_sha256 != digest(fact) or row.projection_sha256 != digest(row.projection)
                or row.manifest_sha256 != adapter.config.manifest_sha256):
            raise HTTPException(409, 'assistant_progress_binding_conflict')
        _enlist(db, work, dispatch, row)
        return row
    admission = dict(contract_version=1,operation='inspect',scope=work.snapshot['scope'],
        request_id=work.request_id,text=view['text'],title=view['title'],
        at=work.created_at.isoformat(),snapshot=work.snapshot)
    projection = adapter.project(admission=admission, fact={k:fact[k] for k in ('work_id','run_id','state','at')})
    row = AssistantProgress(work_id=work.task_id,run_id=dispatch.run_id,state=state,fact=fact,
        fact_sha256=digest(fact),projection=projection,projection_sha256=digest(projection),
        manifest_sha256=adapter.config.manifest_sha256,created_at=now)
    db.add(row)
    record_event(db, actor_id=principal.subject_id, organization_id=work.organization_id,
        store_id=work.store_id,action='assistant.progress_recorded',target_type='task',target_id=work.task_id,
        idempotency_key='assistant-progress:' + work.task_id,
        details=dict(run_id=dispatch.run_id,state=state,fact_sha256=row.fact_sha256,
            projection_sha256=row.projection_sha256,manifest_sha256=row.manifest_sha256),occurred_at=now)
    db.flush()
    _enlist(db, work, dispatch, row)
    return row


def _enlist(db, work, dispatch, row):
    expected_fact = row.fact_sha256
    expected_projection = row.projection_sha256
    expected_manifest = row.manifest_sha256
    expected_run, expected_state = row.run_id, row.state
    def final(session, now):
        session.refresh(work)
        session.refresh(dispatch)
        session.refresh(row)
        _, fact = _capture(session, work, dispatch)
        if (digest(fact) != expected_fact or row.fact_sha256 != expected_fact
                or digest(row.fact) != expected_fact or row.projection_sha256 != expected_projection
                or digest(row.projection) != expected_projection or row.manifest_sha256 != expected_manifest
                or row.run_id != expected_run or row.state != expected_state):
            raise HTTPException(409, 'assistant_progress_commit_conflict')
    commit_guard.enlist(db, action='assistant.progress', check=final)


def reconcile(config, work_id):
    """Explicit readback only. No Pi call, source adoption, authority or retry."""
    from backend.app.modules.assistant import dispatch as worker
    worker._config(config)
    with config.pi.session_factory.begin() as db:
        principal, now = worker._machine(db, config)
        work, _ = worker._work(db, config, work_id)
        dispatch = db.scalar(select(AssistantDispatch).where(AssistantDispatch.work_id == work_id)
            .with_for_update().execution_options(populate_existing=True))
        row = record(db, config, principal, work, dispatch, now)
        worker._machine_commit(db, config)
        return dict(work_id=row.work_id,run_id=row.run_id,state=row.state,
                    fact_sha256=row.fact_sha256,projection_sha256=row.projection_sha256)
