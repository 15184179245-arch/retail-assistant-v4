from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, ensure_subject_active
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority
from backend.app.application import commit_guard
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.repository import grants_for, load_scope
from backend.app.modules.identity.service import authorize_scope, permission_allowed
from backend.app.modules.tasks.models import Task, TaskEvent, TaskIdempotency
from backend.app.modules.notifications.service import notify_task_transition
from backend.app.modules.tasks.repository import (
    compare_and_set_task_state, get_idempotency, get_task, list_events, list_tasks, list_tasks_visible,
)


def _fail(status: int, detail: str) -> None:
    raise HTTPException(status_code=status, detail=detail)


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        _fail(400, "task_time_timezone_required")
    return current.astimezone(timezone.utc)


def _key(value: str) -> str:
    if not value or not value.strip() or len(value) > 200:
        _fail(400, "idempotency_key_invalid")
    return value.strip()


def _digest(operation: str, values: dict) -> str:
    packed = json.dumps({"operation": operation, **values}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _authorize(session: Session, actor: AuthenticatedSubject, permission: str | None, current: datetime) -> None:
    ensure_subject_active(actor, current)
    authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission=permission, now=current)


def _begin_command(session, actor, permission):
    with session.no_autoflush:
        epoch = lock_command_authority(session, actor)
        _authorize(session, actor, permission, commit_guard._db_clock(session))
    return epoch


def _enlist_command(session, actor, permission, epoch, *, extra=None, new_effect=False):
    def final(db, current):
        check_command_authority(db, actor, permission=permission, epoch=epoch, now=current)
        if new_effect and extra is not None:
            extra(db, current)
    commit_guard.enlist(session, action=permission, check=final)


def _require_person_actor(session: Session, actor: AuthenticatedSubject) -> None:
    subject_type = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id))
    if subject_type != "person":
        _fail(403, "task_human_actor_required")


def _has_permission(session: Session, *, organization_id: str, store_id: str,
                    subject_id: str, permission: str, current: datetime) -> bool:
    organization, store, subject, roles = load_scope(session, organization_id, store_id, subject_id)
    if not (organization and organization.active and store and store.active
            and subject and subject.active and store.organization_id == organization_id
            and subject.organization_id == organization_id):
        return False
    grants = grants_for(session, organization_id, store_id, subject_id, roles, permission)
    return permission_allowed(grants, subject_id, now=current)


def _valid_assignee(session: Session, actor: AuthenticatedSubject, subject_id: str,
                    current: datetime) -> None:
    if not has_usable_account(session, organization_id=actor.organization_id,
                              store_id=actor.store_id, subject_id=subject_id, now=current):
        _fail(400, "task_assignee_invalid")
    if not _has_permission(session, organization_id=actor.organization_id,
                           store_id=actor.store_id, subject_id=subject_id,
                           permission="tasks.start", current=current):
        _fail(400, "task_assignee_invalid")


def _replay(session: Session, actor: AuthenticatedSubject, key: str, operation: str, digest: str) -> Task | None:
    existing = get_idempotency(session, actor.organization_id, actor.store_id, key)
    if existing is None:
        return None
    if existing.actor_id != actor.subject_id or existing.operation != operation or existing.payload_digest != digest:
        _fail(409, "idempotency_conflict")
    task = get_task(session, actor.organization_id, actor.store_id, existing.task_id)
    if task is None:
        _fail(409, "task_conflict")
    return task


def _event(session: Session, *, actor: AuthenticatedSubject, task: Task, previous: str | None,
           reason: str, occurred_at: datetime) -> None:
    event = TaskEvent(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        task_id=task.id, actor_id=actor.subject_id, from_state=previous, to_state=task.state,
        decision_state=task.decision_state,
        responsible_id=task.assignee_id, due_at=task.due_at, reason=reason,
        occurred_at=occurred_at,
    )
    session.add(event)
    notify_task_transition(session, task=task, event_id=event.id, now=occurred_at)


def _audit(session: Session, *, actor: AuthenticatedSubject, task: Task, operation: str,
           key: str, previous: str | None, reason: str, occurred_at: datetime) -> None:
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action=f"task.{operation}", target_type="task", target_id=task.id,
        idempotency_key=f"task:{task.id}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}", occurred_at=occurred_at,
        details={"from_state": previous, "to_state": task.state, "responsible_id": task.assignee_id,
                 "decision_state": task.decision_state,
                 "due_at": task.due_at.isoformat() if task.due_at else None, "reason": reason})


def _store_idempotency(session: Session, *, actor: AuthenticatedSubject, task: Task,
                       key: str, operation: str, digest: str) -> None:
    session.add(TaskIdempotency(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        idempotency_key=key, operation=operation, payload_digest=digest,
        task_id=task.id, actor_id=actor.subject_id,
    ))
    session.flush()


def _ensure_real_sqlite_transaction(session: Session) -> None:
    connection = session.connection()
    if connection.dialect.name == "sqlite":
        raw = connection.connection.driver_connection
        if not raw.in_transaction:
            connection.exec_driver_sql("BEGIN")


def _run_idempotent(session: Session, *, actor: AuthenticatedSubject, key: str,
                    operation: str, digest: str, apply, enlist=None):
    replayed = _replay(session, actor, key, operation, digest)
    if replayed is not None:
        if enlist is not None:
            enlist(False)
        return replayed
    _ensure_real_sqlite_transaction(session)
    try:
        with session.begin_nested():
            task, previous, reason, current = apply()
            _event(session, actor=actor, task=task, previous=previous, reason=reason, occurred_at=current)
            _audit(session, actor=actor, task=task, operation=operation, key=key,
                   previous=previous, reason=reason, occurred_at=current)
            _store_idempotency(session, actor=actor, task=task, key=key, operation=operation, digest=digest)
            if enlist is not None:
                enlist(True)
        return task
    except IntegrityError:
        replayed = _replay(session, actor, key, operation, digest)
        if replayed is not None:
            if enlist is not None:
                enlist(False)
            return replayed
        _fail(409, "task_conflict")


def create_task(session: Session, actor: AuthenticatedSubject, *, title: str, description: str,
                proposal_ref: str | None, reason: str, idempotency_key: str,
                reviewer_id: str | None = None, agent_only: bool = False,
                now: datetime | None = None) -> Task:
    current = _now(now)
    epoch = _begin_command(session, actor, "tasks.suggest")
    checked_at = commit_guard._db_clock(session)
    key = _key(idempotency_key)
    subject_type = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id))
    source_by_subject = {"person": "human", "agent": "agent"}
    source = source_by_subject.get(subject_type)
    if source is None:
        _fail(403, "task_subject_type_unsupported")
    if agent_only and source != "agent":
        _fail(403, "task_agent_actor_required")
    if source == "agent" and reviewer_id is None:
        _fail(400, "task_reviewer_invalid")
    if reviewer_id is not None:
        if source != "agent" or not has_usable_account(session, organization_id=actor.organization_id,
            store_id=actor.store_id, subject_id=reviewer_id, now=checked_at) or not _has_permission(
                session, organization_id=actor.organization_id, store_id=actor.store_id,
                subject_id=reviewer_id, permission="tasks.decide", current=checked_at):
            _fail(400, "task_reviewer_invalid")
    if not title.strip() or not description.strip() or not reason.strip():
        _fail(400, "task_invalid")
    values = {"actor_id": actor.subject_id, "title": title, "description": description,
              "source": source, "proposal_ref": proposal_ref, "reviewer_id": reviewer_id, "reason": reason}
    digest = _digest("create", values)

    def apply():
        task = Task(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
                    created_by=actor.subject_id, title=title, description=description, source=source,
                    proposal_ref=proposal_ref, reviewer_id=reviewer_id,
                    state="suggested", decision_state="pending")
        session.add(task)
        session.flush()
        return task, None, reason, current

    def extra(db, instant):
        if reviewer_id is not None and (not has_usable_account(db, organization_id=actor.organization_id,
                store_id=actor.store_id, subject_id=reviewer_id, now=instant) or not _has_permission(
                db, organization_id=actor.organization_id, store_id=actor.store_id,
                subject_id=reviewer_id, permission="tasks.decide", current=instant)):
            _fail(400, "task_reviewer_invalid")
    return _run_idempotent(session, actor=actor, key=key, operation="create", digest=digest, apply=apply,
        enlist=lambda new: _enlist_command(session, actor, "tasks.suggest", epoch, extra=extra, new_effect=new))


def create_managed_task(session: Session, actor: AuthenticatedSubject, *, title: str,
                        description: str, reason: str, idempotency_key: str,
                        assignee_id: str | None = None, due_at: datetime | None = None,
                        now: datetime | None = None) -> Task:
    current = _now(now)
    epoch = _begin_command(session, actor, "tasks.manage")
    checked_at = commit_guard._db_clock(session)
    _require_person_actor(session, actor)
    key = _key(idempotency_key)
    if not title.strip() or not description.strip() or not reason.strip():
        _fail(400, "task_invalid")
    if (assignee_id is None) != (due_at is None):
        _fail(400, "task_assignment_incomplete")
    deadline = None
    if assignee_id is not None:
        _authorize(session, actor, "tasks.assign", checked_at)
        deadline = _now(due_at)
        if deadline <= checked_at:
            _fail(400, "task_due_date_invalid")
        _valid_assignee(session, actor, assignee_id, checked_at)
    digest = _digest("manage", {"actor_id": actor.subject_id, "title": title,
        "description": description, "reason": reason, "assignee_id": assignee_id,
        "due_at": deadline.isoformat() if deadline else None})

    def apply():
        task = Task(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
            created_by=actor.subject_id, title=title, description=description, source="human",
            state="assigned" if assignee_id else "accepted", decision_state="accepted",
            reviewer_id=actor.subject_id, accepted_by=actor.subject_id, accepted_at=current,
            assigned_by=actor.subject_id if assignee_id else None,
            assigned_at=current if assignee_id else None,
            assignee_id=assignee_id, due_at=deadline)
        session.add(task)
        session.flush()
        return task, None, reason, current

    def extra(db, instant):
        if assignee_id is not None:
            _authorize(db, actor, "tasks.assign", instant)
            _valid_assignee(db, actor, assignee_id, instant)
            if deadline <= instant:
                _fail(400, "task_due_date_invalid")
    return _run_idempotent(session, actor=actor, key=key, operation="create", digest=digest, apply=apply,
        enlist=lambda new: _enlist_command(session, actor, "tasks.manage", epoch, extra=extra, new_effect=new))


def _scheduled_person_clock(session: Session, actor: AuthenticatedSubject, task_id: str) -> datetime | None:
    """Schedule-origin transitions require a real current human login.

    Keep legacy human/Agent entry contracts intact. Never turn a machine or a
    sessionless internal person locator into permission to review scheduled work.
    Replays take the same fence before current permission/session validation.
    """
    with session.no_autoflush:
        source = session.scalar(select(Task.source).where(Task.organization_id == actor.organization_id,
            Task.store_id == actor.store_id, Task.id == task_id))
        if source != "schedule":
            return None
        if (not actor.account_id or not actor.session_id or type(actor.account_version) is not int
                or actor.account_version <= 0):
            _fail(403, "task_human_session_required")
        lock_employee_authority(session, actor)
        sql = "SELECT clock_timestamp()" if session.get_bind().dialect.name == "postgresql" else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
        result = session.execute(text(sql)).scalar_one()
        value = datetime.fromisoformat(result) if isinstance(result, str) else result
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _transition(session: Session, actor: AuthenticatedSubject, *, task_id: str, operation: str,
                permission: str, expected: str, target: str, reason: str,
                values: dict | None = None, digest_values: dict | None = None,
                idempotency_key: str, now: datetime | None = None,
                validate=None) -> Task:
    scheduled_clock = _scheduled_person_clock(session, actor, task_id)
    current = scheduled_clock or _now(now)
    epoch = _begin_command(session, actor, permission)
    # The source pre-read only located the policy. Decide from a fresh source
    # under the real authority lock, without flushing caller work first.
    scheduled_clock = _scheduled_person_clock(session, actor, task_id)
    if scheduled_clock is not None:
        current = scheduled_clock
    checked_at = commit_guard._db_clock(session)
    _require_person_actor(session, actor)
    key = _key(idempotency_key)
    if not reason.strip():
        _fail(400, "task_invalid")
    digest = _digest(operation, {"actor_id": actor.subject_id, "task_id": task_id,
                                "reason": reason, **(digest_values if digest_values is not None else (values or {}))})
    replayed = _replay(session, actor, key, operation, digest)
    if replayed is not None:
        _enlist_command(session, actor, permission, epoch)
        return replayed
    task = get_task(session, actor.organization_id, actor.store_id, task_id)
    if task is None:
        _fail(404, "task_not_found")
    if task.state != expected:
        _fail(409, "task_conflict")
    if validate:
        validate(task, checked_at)
    if scheduled_clock is not None and values:
        # Schedule-origin row timestamps and Event/Audit must share one DB
        # instant; caller/process clock values are never durable evidence.
        values = dict(values)
        field = {"accept": "accepted_at", "assign": "assigned_at", "start": "started_at",
                 "verify": "verified_at", "close": "closed_at"}.get(operation)
        if field in values:
            values[field] = current

    def apply():
        updates = {"state": target, **(values or {})}
        if not compare_and_set_task_state(session, organization_id=actor.organization_id,
            store_id=actor.store_id, task_id=task.id, expected_state=expected, values=updates):
            _fail(409, "task_conflict")
        session.refresh(task)
        return task, expected, reason, current

    def extra(db, instant):
        _require_person_actor(db, actor)
        if operation == "assign":
            _valid_assignee(db, actor, values["assignee_id"], instant)
            if values["due_at"] <= instant:
                _fail(400, "task_due_date_invalid")
    return _run_idempotent(session, actor=actor, key=key, operation=operation, digest=digest, apply=apply,
        enlist=lambda new: _enlist_command(session, actor, permission, epoch, extra=extra, new_effect=new))


def accept_task(session: Session, actor: AuthenticatedSubject, *, task_id: str, reason: str,
                idempotency_key: str, now: datetime | None = None) -> Task:
    def validate(task, current):
        if task.reviewer_id is not None or task.source in ("agent", "schedule"):
            _fail(403, "task_reviewer_required")
        if task.created_by == actor.subject_id:
            _fail(403, "task_self_accept_forbidden")
    return _transition(session, actor, task_id=task_id, operation="accept", permission="tasks.accept",
        expected="suggested", target="accepted", reason=reason,
        values={"accepted_by": actor.subject_id, "accepted_at": _now(now),
                "decision_state": "accepted"},
        digest_values={},
        idempotency_key=idempotency_key, now=now, validate=validate)


def decide_task(session: Session, actor: AuthenticatedSubject, *, task_id: str,
                choice: str, reason: str, idempotency_key: str,
                supplement_ref: str | None = None, now: datetime | None = None) -> Task:
    current = _scheduled_person_clock(session, actor, task_id) or _now(now)
    epoch = _begin_command(session, actor, "tasks.decide")
    scheduled_clock = _scheduled_person_clock(session, actor, task_id)
    if scheduled_clock is not None:
        current = scheduled_clock
    _require_person_actor(session, actor)
    key = _key(idempotency_key)
    if choice not in {"accept", "reject", "defer", "request_info"} or not reason.strip():
        _fail(400, "task_decision_invalid")
    if supplement_ref is not None and (not supplement_ref.strip() or len(supplement_ref) > 200):
        _fail(400, "task_decision_invalid")
    digest = _digest("decide", {"actor_id": actor.subject_id, "task_id": task_id,
        "choice": choice, "reason": reason, "supplement_ref": supplement_ref})
    replayed = _replay(session, actor, key, "decide", digest)
    if replayed is not None:
        _enlist_command(session, actor, "tasks.decide", epoch)
        return replayed
    task = get_task(session, actor.organization_id, actor.store_id, task_id)
    if task is None:
        _fail(404, "task_not_found")
    if task.source not in ("agent", "schedule") or task.reviewer_id != actor.subject_id:
        _fail(403, "task_reviewer_required")
    if task.state != "suggested" or task.decision_state not in {"pending", "deferred", "info_requested"}:
        _fail(409, "task_conflict")
    if choice == "accept" and task.decision_state == "info_requested" and supplement_ref is None:
        _fail(400, "task_supplement_required")
    if choice != "accept" and supplement_ref is not None:
        _fail(400, "task_decision_invalid")
    target_decision = {"accept": "accepted", "reject": "rejected",
                       "defer": "deferred", "request_info": "info_requested"}[choice]
    previous_decision = task.decision_state
    event_reason = reason if supplement_ref is None else f"{reason}\n[补充资料引用] {supplement_ref}"

    def apply():
        values = {"decision_state": target_decision}
        if choice == "accept":
            values.update(state="accepted", accepted_by=actor.subject_id, accepted_at=current)
        if not compare_and_set_task_state(session, organization_id=actor.organization_id,
            store_id=actor.store_id, task_id=task.id, expected_state="suggested",
            expected_decision_state=previous_decision, values=values):
            _fail(409, "task_conflict")
        session.refresh(task)
        return task, "suggested", event_reason, current

    return _run_idempotent(session, actor=actor, key=key, operation="decide",
                           digest=digest, apply=apply,
        enlist=lambda new: _enlist_command(session, actor, "tasks.decide", epoch, new_effect=new))


def assign_task(session: Session, actor: AuthenticatedSubject, *, task_id: str, assignee_id: str,
                due_at: datetime, reason: str, idempotency_key: str,
                now: datetime | None = None) -> Task:
    _scheduled_person_clock(session, actor, task_id)
    _begin_command(session, actor, "tasks.assign")
    scheduled_clock = _scheduled_person_clock(session, actor, task_id)
    current = scheduled_clock or _now(now)
    checked_at = commit_guard._db_clock(session)
    deadline = _now(due_at)
    # A known successful scheduled assignment remains replayable after its
    # deadline. New writes are checked against the DB clock in validate below.
    # Preserve the legacy human/Agent precheck contract.
    if scheduled_clock is None and deadline <= checked_at:
        _fail(400, "task_due_date_invalid")
    def validate(_task, observed_now):
        if deadline <= observed_now:
            _fail(400, "task_due_date_invalid")
        _valid_assignee(session, actor, assignee_id, observed_now)
    return _transition(session, actor, task_id=task_id, operation="assign", permission="tasks.assign",
        expected="accepted", target="assigned", reason=reason,
        values={"assigned_by": actor.subject_id, "assigned_at": current,
                "assignee_id": assignee_id, "due_at": deadline},
        digest_values={"assignee_id": assignee_id, "due_at": deadline.isoformat()},
        idempotency_key=idempotency_key, now=current, validate=validate)


def start_task(session: Session, actor: AuthenticatedSubject, *, task_id: str, reason: str,
               idempotency_key: str, now: datetime | None = None) -> Task:
    def validate(task, current):
        if task.assignee_id != actor.subject_id:
            _fail(403, "task_assignee_required")
    return _transition(session, actor, task_id=task_id, operation="start", permission="tasks.start",
        expected="assigned", target="in_progress", reason=reason,
        values={"started_by": actor.subject_id, "started_at": _now(now)},
        digest_values={},
        idempotency_key=idempotency_key, now=now, validate=validate)


def verify_task(session: Session, actor: AuthenticatedSubject, *, task_id: str, reason: str,
                idempotency_key: str, now: datetime | None = None) -> Task:
    def validate(task, current):
        if task.assignee_id == actor.subject_id:
            _fail(403, "task_self_verify_forbidden")
    return _transition(session, actor, task_id=task_id, operation="verify", permission="tasks.verify",
        expected="in_progress", target="verified", reason=reason,
        values={"verified_by": actor.subject_id, "verified_at": _now(now), "verification_reason": reason},
        digest_values={},
        idempotency_key=idempotency_key, now=now, validate=validate)


def close_task(session: Session, actor: AuthenticatedSubject, *, task_id: str, reason: str,
               idempotency_key: str, now: datetime | None = None) -> Task:
    return _transition(session, actor, task_id=task_id, operation="close", permission="tasks.close",
        expected="verified", target="closed", reason=reason,
        values={"closed_by": actor.subject_id, "closed_at": _now(now), "close_reason": reason},
        digest_values={},
        idempotency_key=idempotency_key, now=now)


def get_task_for_scope(session: Session, actor: AuthenticatedSubject, task_id: str,
                       *, now: datetime | None = None) -> Task:
    current = _now(now)
    full, own, responsible = _read_permissions(session, actor, current)
    task = get_task(session, actor.organization_id, actor.store_id, task_id)
    if task is None or not (full or (own and (task.created_by == actor.subject_id
        or task.assignee_id == actor.subject_id)) or (responsible and task.reviewer_id == actor.subject_id)):
        _fail(404, "task_not_found")
    return task


def list_tasks_for_scope(session: Session, actor: AuthenticatedSubject,
                         *, now: datetime | None = None) -> list[Task]:
    current = _now(now)
    full, own, responsible = _read_permissions(session, actor, current)
    if full:
        return list_tasks(session, actor.organization_id, actor.store_id)
    return list_tasks_visible(session, organization_id=actor.organization_id, store_id=actor.store_id,
                              subject_id=actor.subject_id, read_self=own,
                              read_responsible=responsible)


def task_events_for_scope(session: Session, actor: AuthenticatedSubject, task_id: str,
                          *, now: datetime | None = None) -> list[TaskEvent]:
    task = get_task_for_scope(session, actor, task_id, now=now)
    return list_events(session, actor.organization_id, actor.store_id, task.id)


def _read_permissions(session: Session, actor: AuthenticatedSubject,
                      current: datetime) -> tuple[bool, bool, bool]:
    _authorize(session, actor, None, current)
    scope = {"organization_id": actor.organization_id, "store_id": actor.store_id,
             "subject_id": actor.subject_id, "current": current}
    person = session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id, Subject.id == actor.subject_id)) == "person"
    full = person and _has_permission(session, permission="tasks.read", **scope)
    own = person and _has_permission(session, permission="tasks.read_self", **scope)
    responsible = person and _has_permission(session, permission="tasks.read_responsible", **scope)
    if not (full or own or responsible):
        _fail(403, "permission_denied")
    return full, own, responsible


def create_scheduled_suggestion(session: Session, *, machine_token: str, occurrence_id: str) -> dict:
    """Atomic local effect; no caller-supplied approval or synthetic employee.

    All Task, event, notification, slot and immutable receipt writes share a
    savepoint. Caller owns the outer transaction. Final revalidation failures
    roll back this whole effect, even when a caller catches the exception.
    """
    from backend.app.modules.office_scheduling import ofc_008_authority as a
    principal, _ = a.machine_guard(session, machine_token)
    row, grant, occurrence = a.occurrence_context(session, principal, occurrence_id)
    a.bound_outbox(session, principal, occurrence, grant)
    old = a.final_receipt(session, principal, occurrence)
    if old is not None:
        return dict(receipt_id=old.id, task_id=old.task_id, replayed=True)
    if occurrence.state != "queued":
        a.fail("occurrence_state_invalid")
    now = a.clock(session)
    try:
        snapshot = a.validate_slot(session, principal, row, grant, occurrence, now)
    except a.ScheduleError as exc:
        with session.begin_nested():
            receipt = a.control_close(session, principal, row, grant, occurrence,
                                      exc.code.removeprefix("schedule_").upper(), now)
        return dict(receipt_id=receipt.id, task_id=None, replayed=False)
    with session.begin_nested():
        description, facts = snapshot["description"], None
        if snapshot["kind"] == "review_summary":
            from sqlalchemy import func
            # A single statement supplies a bounded, consistently counted visible
            # pending-review subset. It grants no access to other people's work.
            results = session.execute(select(Task.id, Task.title, Task.source, Task.state,
                Task.decision_state, func.count().over().label("total")).where(
                    Task.organization_id == principal.organization_id, Task.store_id == principal.store_id,
                    Task.reviewer_id == grant.reviewer_id, Task.state == "suggested",
                    Task.decision_state == "pending").order_by(Task.created_at, Task.id).limit(10)).all()
            total = results[0].total if results else 0
            facts = dict(total_count=total, fact_ids=[r.id for r in results])
            lines = [description, "当前可见待核对建议摘要（非经营或财务结论）", f"生成前共{total}条，列示{len(results)}条。"]
            lines += [f"{r.id} | {r.title} | {r.source} | {r.state}/{r.decision_state}" for r in results]
            lines.append(f"未列示{total-len(results)}条。")
            description = "\n".join(lines)
        if not 1 <= len(description) <= 4000:
            a.fail("summary_too_long")
        source_digest = a.digest(dict(template_digest=grant.template_digest, description=description, facts=facts))
        key = "ofc8task:" + occurrence.id
        reason = "经有限排程授权生成本地待人审建议"
        values = dict(occurrence_id=occurrence.id, template_digest=grant.template_digest)
        def apply():
            task = Task(id=str(uuid4()), organization_id=principal.organization_id, store_id=principal.store_id,
                created_by=principal.subject_id, title=snapshot["title"], description=description, source="schedule",
                proposal_ref="office-schedule:" + occurrence.id, reviewer_id=grant.reviewer_id,
                state="suggested", decision_state="pending", created_at=now)
            session.add(task)
            session.flush()
            return task, None, reason, now
        # Operation is Task creation, as for Agent suggestions; origin remains
        # source=schedule and the separate immutable materialize receipt.
        task = _run_idempotent(session, actor=principal, key=key, operation="create",
                               digest=a.digest(values), apply=apply)
        occurrence.state, occurrence.decision_code = "materialized", "MATERIALIZED"
        occurrence.task_id, occurrence.source_digest = task.id, source_digest
        occurrence.version += 1
        occurrence.updated_at = now
        session.flush()
        refs = dict(template_digest=grant.template_digest, source_digest=source_digest,
            decision_code="MATERIALIZED", local_date=occurrence.local_date,
            window_start=occurrence.scheduled_at, window_end=occurrence.window_end)
        if facts:
            refs.update(facts)
        receipt = a.record(session, principal, row, operation="materialize", request_key="ofc8slot:" + occurrence.id,
            request_hash=a.digest(values), now=now, references=refs, grant=grant, occurrence=occurrence, task_id=task.id)
        # Do not turn our new receipt into a replay that skips final expiry.
        current = a.clock(session)
        from backend.app.modules.identity.machine_service import authenticate_machine_token, authorize_machine_action
        fresh = authenticate_machine_token(session, machine_token, now=current)
        authorize_machine_action(session, fresh, organization_id=principal.organization_id,
                                 store_id=principal.store_id, action=a.ACTION, now=current)
        a.validate_slot(session, fresh, row, grant, occurrence, current, consumed=True)
        return dict(receipt_id=receipt.id, task_id=task.id, replayed=False)
