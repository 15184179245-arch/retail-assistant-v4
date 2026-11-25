"""Schedule authority primitives. No Task service, synthetic employee or commit.

The caller retains the scope fence and Store lock through its outer transaction.
Historical grants record finite approval; every new effect needs current policy.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text, update

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.models import AuditEvent
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.models import EmployeeAccount, LoginSession
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.command_guard import (
    lock_command_authority, lock_employee_authority, lock_scope_security_fence,
)
from backend.app.modules.identity.models import Store, Subject
from backend.app.modules.identity.repository import grants_for, load_scope
from backend.app.modules.identity.service import authorize_scope, permission_allowed
from backend.app.modules.identity.machine_service import (
    MachineIdentity, MachineCredential, authenticate_machine_token, authorize_machine_action,
)
from backend.app.modules.office_scheduling.ofc_008_models import (
    OfficeSchedule, OfficeScheduleGrant, OfficeScheduleOccurrence, OfficeScheduleReceipt,
)
from backend.app.modules.office_scheduling.ofc_008_schemas import ScheduleTemplateInput, ScheduleReferences

ACTION = "worker.office.schedule.materialize"
EVENT_PREFIX = "office.schedule.materialize.v1."


class ScheduleError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise ScheduleError("schedule_" + code)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def clock(db):
    sql = "SELECT clock_timestamp()" if db.get_bind().dialect.name == "postgresql" else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    value = db.execute(text(sql)).scalar_one()
    return utc(datetime.fromisoformat(value) if isinstance(value, str) else value)


def scope(model, actor):
    return (model.organization_id == actor.organization_id, model.store_id == actor.store_id)


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def typed(model, body):
    if not isinstance(body, model):
        fail("input_invalid")
    try:
        return model.model_validate(body.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError):
        fail("input_invalid")


def template(row):
    fields = ScheduleTemplateInput.model_fields
    data = {key: getattr(row, key) for key in fields}
    return ScheduleTemplateInput.model_validate(data).model_dump(mode="json")


def key(value):
    if type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip():
        fail("key_invalid")
    return value


def allow_person(db, actor, capability, now):
    if (not getattr(actor, "account_id", None) or not getattr(actor, "session_id", None)
            or type(getattr(actor, "account_version", None)) is not int or actor.account_version <= 0):
        fail("human_required")
    authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission="office.schedules." + capability, now=now)
    if db.scalar(select(Subject.subject_type).where(Subject.id == actor.subject_id,
            Subject.organization_id == actor.organization_id)) != "person":
        fail("human_required")


def guard_person(db, actor, capability):
    # Reject a machine or locator before calling the employee-specific guard.
    if not getattr(actor, "account_id", None) or not getattr(actor, "session_id", None):
        fail("human_required")
    if type(actor) is not AuthenticatedSubject:
        raise HTTPException(403, "human_session_required")
    epoch = None
    if capability == "read":
        lock_employee_authority(db, actor)
    else:
        epoch = lock_command_authority(db, actor)
    now = clock(db)
    allow_person(db, actor, "read", now)
    allow_person(db, actor, capability, now)
    return epoch


def person_permission(db, principal, subject_id, permission, now):
    organization, store, subject, roles = load_scope(db, principal.organization_id, principal.store_id, subject_id)
    if (not organization or not organization.active or not store or not store.active
            or store.organization_id != principal.organization_id or not subject or not subject.active
            or subject.organization_id != principal.organization_id or subject.subject_type != "person"):
        return False
    return permission_allowed(grants_for(db, principal.organization_id, principal.store_id,
        subject_id, roles, permission), subject_id, now=now)


def reviewer(db, principal, subject_id, now):
    account = db.scalar(select(EmployeeAccount).where(*scope(EmployeeAccount, principal),
        EmployeeAccount.subject_id == subject_id).with_for_update().execution_options(populate_existing=True))
    if (account is None or not has_usable_account(db, organization_id=principal.organization_id,
            store_id=principal.store_id, subject_id=subject_id, now=now)
            or not all(person_permission(db, principal, subject_id, p, now)
                       for p in ("tasks.decide", "notifications.read"))
            or not any(person_permission(db, principal, subject_id, p, now)
                       for p in ("tasks.read", "tasks.read_responsible"))):
        fail("reviewer_unavailable")
    return account


def selected_machine(db, principal, machine_id, now):
    machine = db.scalar(select(MachineIdentity).where(*scope(MachineIdentity, principal),
        MachineIdentity.id == machine_id).execution_options(populate_existing=True))
    subject = db.get(Subject, machine.subject_id) if machine else None
    credential = None if machine is None else db.scalar(select(MachineCredential.id).where(
        *scope(MachineCredential, principal), MachineCredential.machine_id == machine.id,
        MachineCredential.version == machine.version, MachineCredential.revoked_at.is_(None),
        MachineCredential.issued_at <= now, MachineCredential.expires_at > now))
    if (machine is None or not machine.active or machine.purpose != "worker" or not subject
            or not subject.active or subject.subject_type != "service"
            or subject.organization_id != principal.organization_id or credential is None):
        fail("machine_unavailable")
    return machine


def schedule(db, principal, ident, *, lock=True):
    query = select(OfficeSchedule).where(*scope(OfficeSchedule, principal), OfficeSchedule.id == ident)
    row = db.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    if row is None:
        fail("not_found")
    return row


def current_grant(db, principal, row):
    return db.scalar(select(OfficeScheduleGrant).where(*scope(OfficeScheduleGrant, principal),
        OfficeScheduleGrant.schedule_id == row.id).order_by(OfficeScheduleGrant.schedule_version.desc()).limit(1))


def usage(db, principal, grant):
    used = db.scalar(select(func.count()).select_from(OfficeScheduleReceipt).where(
        *scope(OfficeScheduleReceipt, principal), OfficeScheduleReceipt.grant_id == grant.id,
        OfficeScheduleReceipt.operation == "materialize"))
    reserved = db.scalar(select(func.count()).select_from(OfficeScheduleOccurrence).where(
        *scope(OfficeScheduleOccurrence, principal), OfficeScheduleOccurrence.grant_id == grant.id,
        OfficeScheduleOccurrence.state == "queued"))
    return used, reserved


def request_digest(actor, operation, ident, body):
    return digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        operation=operation, schedule_id=ident, input=body.model_dump(mode="json")))


def replay(db, actor, request_key, request_hash):
    old = db.scalar(select(OfficeScheduleReceipt).where(*scope(OfficeScheduleReceipt, actor),
        OfficeScheduleReceipt.request_key == request_key))
    if old is not None and (old.actor_id != actor.subject_id or old.request_digest != request_hash):
        fail("idempotency_conflict")
    return old


def audit_key(principal, request_key):
    return "ofc8:" + digest(dict(scope=[principal.organization_id, principal.store_id], key=request_key))


def record(db, actor, row, *, operation, request_key, request_hash, now, references,
           grant=None, occurrence=None, task_id=None):
    references = ScheduleReferences.model_validate(references).model_dump(mode="json", exclude_none=True)
    version = occurrence.schedule_version if occurrence else row.version
    receipt = OfficeScheduleReceipt(id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, schedule_id=row.id, schedule_version=version, operation=operation,
        request_key=request_key, request_digest=request_hash, actor_id=actor.subject_id,
        references=references, occurrence_id=occurrence.id if occurrence else None,
        grant_id=grant.id if grant else None, task_id=task_id, created_at=now)
    db.add(receipt)
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action="office.schedule." + operation, target_type="office_schedule",
        target_id=row.id, idempotency_key=audit_key(actor, request_key), occurred_at=now,
        details=dict(receipt_id=receipt.id, grant_id=receipt.grant_id, schedule_version=version,
                     input_digest=request_hash, template_digest=references.get("template_digest")))
    db.flush()
    return receipt


def machine_guard(db, token):
    principal = authenticate_machine_token(db, token, now=clock(db))
    lock_scope_security_fence(db, principal.organization_id, principal.store_id)
    changed = db.execute(update(Store).where(
        Store.organization_id == principal.organization_id, Store.id == principal.store_id,
        Store.active.is_(True)).values(active=Store.active).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        fail("machine_unavailable")
    db.expire_all()
    now = clock(db)
    principal = authenticate_machine_token(db, token, now=now)
    authorize_machine_action(db, principal, organization_id=principal.organization_id,
        store_id=principal.store_id, action=ACTION, now=now)
    return principal, now


def validate_grant(db, principal, row, grant, now):
    """Current approval policy independent of quota and per-slot window."""
    if (row.state != "active" or grant is None or grant.machine_id != getattr(principal, "machine_id", grant.machine_id)
            or (grant.organization_id, grant.store_id, grant.schedule_id) != (
                principal.organization_id, principal.store_id, row.id)
            or grant.schedule_version != row.version or grant.template_digest != row.template_digest):
        fail("needs_reapproval")
    epoch = lock_scope_security_fence(db, principal.organization_id, principal.store_id)
    if epoch != grant.authorization_epoch:
        fail("needs_reapproval")
    account = db.scalar(select(EmployeeAccount).where(EmployeeAccount.id == grant.account_id)
        .with_for_update().execution_options(populate_existing=True))
    old_session = db.scalar(select(LoginSession).where(LoginSession.id == grant.session_id)
        .with_for_update().execution_options(populate_existing=True))
    if (account is None or (account.organization_id, account.store_id, account.subject_id,
            account.version) != (principal.organization_id, principal.store_id, grant.approved_by, grant.account_version)
            or not has_usable_account(db, organization_id=principal.organization_id, store_id=principal.store_id,
                                     subject_id=grant.approved_by, now=now)
            or old_session is None or old_session.account_id != account.id
            or old_session.account_version != grant.account_version or old_session.revoked_at is not None
            or not utc(old_session.created_at) <= grant.created_at < utc(old_session.expires_at)):
        fail("needs_reapproval")
    for permission in ("office.schedules.read", "office.schedules.approve", "tasks.suggest"):
        if not person_permission(db, principal, grant.approved_by, permission, now):
            fail("needs_reapproval")
    reviewer(db, principal, grant.reviewer_id, now)
    selected_machine(db, principal, grant.machine_id, now)
    try:
        snapshot = ScheduleTemplateInput.model_validate(grant.template_snapshot).model_dump(mode="json")
        if digest(snapshot) != grant.template_digest or snapshot != template(row):
            fail("needs_reapproval")
    except (ValidationError, TypeError, ValueError):
        fail("needs_reapproval")
    receipt = db.scalar(select(OfficeScheduleReceipt).where(*scope(OfficeScheduleReceipt, principal),
        OfficeScheduleReceipt.request_key == grant.request_key))
    audit = db.scalar(select(AuditEvent).where(*scope(AuditEvent, principal),
        AuditEvent.idempotency_key == audit_key(principal, grant.request_key)))
    if (receipt is None or receipt.operation != "approve" or receipt.schedule_id != row.id
            or receipt.schedule_version != grant.schedule_version or receipt.grant_id != grant.id
            or receipt.actor_id != grant.approved_by or receipt.request_digest != grant.input_digest
            or receipt.references.get("template_digest") != grant.template_digest
            or receipt.created_at != grant.created_at or audit is None or audit.actor_id != grant.approved_by
            or audit.action != "office.schedule.approve" or audit.target_type != "office_schedule"
            or audit.target_id != row.id or utc(audit.occurred_at) != grant.created_at
            or audit.details != dict(receipt_id=receipt.id, grant_id=grant.id,
                schedule_version=grant.schedule_version, input_digest=grant.input_digest,
                template_digest=grant.template_digest)):
        fail("approval_proof_invalid")
    if now < grant.valid_from:
        fail("not_yet_valid")
    if now >= grant.expires_at:
        fail("authorization_expired")
    return snapshot


def occurrence_context(db, principal, occurrence_id):
    occurrence = db.scalar(select(OfficeScheduleOccurrence).where(*scope(OfficeScheduleOccurrence, principal),
        OfficeScheduleOccurrence.id == occurrence_id).with_for_update().execution_options(populate_existing=True))
    if occurrence is None:
        fail("not_found")
    grant = db.scalar(select(OfficeScheduleGrant).where(*scope(OfficeScheduleGrant, principal),
        OfficeScheduleGrant.id == occurrence.grant_id))
    if grant is None or grant.machine_id != principal.machine_id:
        fail("not_found")
    row = schedule(db, principal, occurrence.schedule_id)
    if (grant.schedule_id, grant.schedule_version) != (row.id, occurrence.schedule_version):
        fail("approval_proof_invalid")
    return row, grant, occurrence


def bound_outbox(db, principal, occurrence, grant):
    from backend.app.workers.outbox import OutboxEvent
    event = db.scalar(select(OutboxEvent).where(*scope(OutboxEvent, principal),
        OutboxEvent.idempotency_key == "office-schedule:" + occurrence.id))
    expected = dict(occurrence_id=occurrence.id, schedule_version=occurrence.schedule_version,
                    template_digest=grant.template_digest)
    if event is None or event.event_type != EVENT_PREFIX + principal.machine_id or event.payload != expected:
        fail("outbox_binding_invalid")
    return event


def validate_slot(db, principal, row, grant, occurrence, now, *, consumed=False):
    from backend.app.modules.office_scheduling.ofc_008_calendar import slot_for_date
    from backend.app.modules.office_scheduling.ofc_008_schemas import CalendarRuleInput
    snapshot = validate_grant(db, principal, row, grant, now)
    slot = slot_for_date(CalendarRuleInput.model_validate(snapshot["rule"]).to_calendar_rule(),
                         occurrence.local_date, now=now)
    if (occurrence.local_date < row.start_date or slot.decision != "due"
            or slot.scheduled_at < grant.valid_from or slot.scheduled_at != occurrence.scheduled_at
            or slot.window_end != occurrence.window_end):
        fail("execution_window_invalid")
    used, reserved = usage(db, principal, grant)
    if used > grant.max_occurrences or (not consumed and used >= grant.max_occurrences) or used + reserved > grant.max_occurrences:
        fail("quota_exhausted")
    return snapshot


def final_receipt(db, principal, occurrence):
    return db.scalar(select(OfficeScheduleReceipt).where(*scope(OfficeScheduleReceipt, principal),
        OfficeScheduleReceipt.occurrence_id == occurrence.id))


def control_close(db, principal, row, grant, occurrence, code, now):
    old = final_receipt(db, principal, occurrence)
    if old:
        return old
    occurrence.state = "skipped" if code in ("AUTHORIZATION_EXPIRED", "EXECUTION_WINDOW_INVALID", "QUOTA_EXHAUSTED") else "needs_reapproval"
    occurrence.decision_code = code
    occurrence.version += 1
    occurrence.updated_at = now
    db.flush()
    return record(db, principal, row, operation="skip" if occurrence.state == "skipped" else "invalidate",
        request_key="ofc8slot:" + occurrence.id,
        request_hash=digest(dict(occurrence_id=occurrence.id, template_digest=grant.template_digest)),
        now=now, grant=grant, occurrence=occurrence,
        references=dict(template_digest=grant.template_digest, local_date=occurrence.local_date,
                        decision_code=code, result_schedule_version=row.version))
