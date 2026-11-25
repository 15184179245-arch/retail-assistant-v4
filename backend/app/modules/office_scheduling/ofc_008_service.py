"""Human-controlled schedule management. Caller owns commit/rollback.

No timer, task effect, default grant or network write is started by this module.
"""
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from backend.app.modules.identity.command_guard import check_command_authority, lock_scope_security_fence
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.office_scheduling import ofc_008_authority as a
from backend.app.modules.office_scheduling.ofc_008_models import OfficeSchedule, OfficeScheduleGrant
from backend.app.modules.office_scheduling.ofc_008_schemas import (
    ScheduleTemplateInput, ScheduleCreateInput, ScheduleReplaceInput, ScheduleApproveInput,
    SchedulePauseInput, ScheduleSummary, ScheduleGrantView, ScheduleReceiptView,
    ScheduleView, ScheduleCommandStatus,
)

ScheduleError = a.ScheduleError


def _summary(row):
    return ScheduleSummary.model_validate({k: getattr(row, k) for k in ScheduleSummary.model_fields}).model_dump()


def _receipt(row):
    return ScheduleReceiptView.model_validate({k: getattr(row, k) for k in ScheduleReceiptView.model_fields}).model_dump()


def _view(db, actor, row, *, command=None, replayed=False):
    now = a.clock(db)
    a.allow_person(db, actor, "read", now)
    grant = a.current_grant(db, actor, row)
    authorization, blocking = None, None
    if grant is not None:
        used, reserved = a.usage(db, actor, grant)
        state = "valid"
        try:
            a.validate_grant(db, actor, row, grant, now)
        except a.ScheduleError as exc:
            blocking = exc.code.removeprefix("schedule_").upper()
            state = {"schedule_not_yet_valid": "not_yet_valid",
                     "schedule_authorization_expired": "expired"}.get(exc.code, "needs_reapproval")
        if state == "valid" and used + reserved >= grant.max_occurrences:
            state, blocking = "exhausted", "QUOTA_EXHAUSTED"
        authorization = ScheduleGrantView.model_validate(dict(
            **{k: getattr(grant, k) for k in ("id", "schedule_id", "schedule_version", "template_digest",
                "template_snapshot", "reviewer_id", "machine_id", "approved_by", "authorization_epoch",
                "valid_from", "expires_at", "max_occurrences")}, approved_at=grant.created_at,
            used_occurrences=used, reserved_occurrences=reserved,
            remaining_occurrences=max(0, grant.max_occurrences-used-reserved), authorization_state=state)).model_dump()
    elif row.state == "active":
        blocking = "APPROVAL_MISSING"
    result = ScheduleView.model_validate(dict(**_summary(row), description=row.description, rule=row.rule,
        authorization=authorization, blocking_code=blocking, command_receipt=_receipt(command) if command else None,
        replayed=replayed)).model_dump()
    a.allow_person(db, actor, "read", a.clock(db))
    return result


def read_schedule(db, actor, ident):
    a.guard_person(db, actor, "read")
    return _view(db, actor, a.schedule(db, actor, ident, lock=False))


def _template_input(body):
    return ScheduleTemplateInput.model_validate({k: getattr(body, k) for k in ScheduleTemplateInput.model_fields})


def _final(db, actor, row, receipt, capability, *, epoch, replayed=False):
    result = _view(db, actor, row, command=receipt, replayed=replayed)
    now = a.clock(db)
    check_command_authority(db, actor, permission="office.schedules." + capability, epoch=epoch, now=now)
    if capability == "approve":
        authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                        permission="tasks.suggest", now=now)
        if not replayed:
            # A new approval must remain valid at the final boundary. A future
            # start is permitted; expiry during the transaction is never hidden.
            grant = a.current_grant(db, actor, row)
            if now >= grant.expires_at:
                a.fail("authorization_expired")
            a.reviewer(db, actor, row.reviewer_id, now)
            a.selected_machine(db, actor, grant.machine_id, now)
    return result


def create_schedule(db, actor, body, key):
    body = a.typed(ScheduleCreateInput, body)
    key = a.key(key)
    epoch = a.guard_person(db, actor, "manage")
    request_hash = a.request_digest(actor, "create", None, body)
    old = a.replay(db, actor, key, request_hash)
    if old is not None:
        return _final(db, actor, a.schedule(db, actor, old.schedule_id), old, "manage", epoch=epoch, replayed=True)
    now = a.clock(db)
    a.reviewer(db, actor, body.reviewer_id, now)
    snapshot = _template_input(body).model_dump(mode="json")
    row = OfficeSchedule(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        **snapshot, template_digest=a.digest(snapshot), state="draft", version=1,
        create_key=key, created_by=actor.subject_id, created_at=now, updated_at=now, last_checked_at=None)
    # SQLAlchemy Date expects a date, while the canonical snapshot is JSON.
    row.start_date = body.start_date
    db.add(row)
    db.flush()
    receipt = a.record(db, actor, row, operation="create", request_key=key, request_hash=request_hash,
        now=now, references=dict(template_digest=row.template_digest, reason_digest=sha256(body.reason.encode()).hexdigest()))
    return _final(db, actor, row, receipt, "manage", epoch=epoch)


def _command(db, actor, ident, body, key, operation):
    cls, capability = {"replace": (ScheduleReplaceInput, "manage"),
                       "approve": (ScheduleApproveInput, "approve"),
                       "pause": (SchedulePauseInput, "manage")}[operation]
    body, key = a.typed(cls, body), a.key(key)
    epoch = a.guard_person(db, actor, capability)
    if operation == "approve":
        authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                        permission="tasks.suggest", now=a.clock(db))
    request_hash = a.request_digest(actor, operation, ident, body)
    old = a.replay(db, actor, key, request_hash)
    row = a.schedule(db, actor, ident)
    if old is not None:
        return _final(db, actor, row, old, capability, epoch=epoch, replayed=True)
    if row.version != body.expected_version:
        a.fail("version_conflict")
    if row.template_digest != body.expected_template_digest:
        a.fail("template_changed")
    now, grant = a.clock(db), None
    if operation == "replace":
        a.reviewer(db, actor, body.reviewer_id, now)
        snapshot = _template_input(body).model_dump(mode="json")
        for name in ScheduleTemplateInput.model_fields:
            setattr(row, name, body.start_date if name == "start_date" else snapshot[name])
        row.template_digest = a.digest(snapshot)
        row.state = "draft"
    elif operation == "pause":
        row.state = "paused"
    else:
        a.reviewer(db, actor, row.reviewer_id, now)
        a.selected_machine(db, actor, body.machine_id, now)
        effective_start = max(body.valid_from, now)
        if body.expires_at <= effective_start:
            a.fail("authorization_interval_invalid")
        try:
            a.utc(effective_start)
            a.utc(body.expires_at)
        except OverflowError:
            a.fail("input_invalid")
        row.state = "active"
    row.version += 1
    row.updated_at = now
    db.flush()
    if operation == "approve":
        grant = OfficeScheduleGrant(id=str(uuid4()), organization_id=actor.organization_id,
            store_id=actor.store_id, schedule_id=row.id, schedule_version=row.version,
            template_digest=row.template_digest, template_snapshot=a.template(row), reviewer_id=row.reviewer_id,
            machine_id=body.machine_id, approved_by=actor.subject_id, account_id=actor.account_id,
            account_version=actor.account_version, session_id=actor.session_id,
            authorization_epoch=lock_scope_security_fence(db, actor.organization_id, actor.store_id),
            valid_from=effective_start, expires_at=body.expires_at,
            max_occurrences=body.max_occurrences, request_key=key, input_digest=request_hash, created_at=now)
        db.add(grant)
        db.flush()
    receipt = a.record(db, actor, row, operation=operation, request_key=key, request_hash=request_hash,
        now=now, grant=grant, references=dict(template_digest=row.template_digest,
                                            reason_digest=sha256(body.reason.encode()).hexdigest()))
    return _final(db, actor, row, receipt, capability, epoch=epoch)


def replace_schedule(db, actor, ident, body, key):
    return _command(db, actor, ident, body, key, "replace")


def approve_schedule(db, actor, ident, body, key):
    return _command(db, actor, ident, body, key, "approve")


def pause_schedule(db, actor, ident, body, key):
    return _command(db, actor, ident, body, key, "pause")


def command_status(db, actor, key, request_digest):
    import re
    from backend.app.modules.office_scheduling.ofc_008_models import OfficeScheduleReceipt
    key = a.key(key)
    if type(request_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", request_digest) is None:
        a.fail("input_invalid")
    a.guard_person(db, actor, "read")
    receipt = db.scalar(select(OfficeScheduleReceipt).where(*a.scope(OfficeScheduleReceipt, actor),
        OfficeScheduleReceipt.request_key == key))
    if receipt is None:
        return ScheduleCommandStatus(status="not_found", schedule=None).model_dump()
    if receipt.actor_id != actor.subject_id or receipt.request_digest != request_digest:
        a.fail("not_found")
    result = _view(db, actor, a.schedule(db, actor, receipt.schedule_id, lock=False), command=receipt, replayed=True)
    return ScheduleCommandStatus(status="found", schedule=result).model_dump()


def _page(db, actor, model, *, ident=None, limit=50, cursor=None, state=None, kind=None):
    import base64
    import json
    from datetime import datetime
    from sqlalchemy import or_, and_
    from pydantic import TypeAdapter, ValidationError
    from backend.app.modules.office_scheduling.ofc_008_schemas import (
        Identifier, SchedulePage, ScheduleOccurrencePage, ScheduleReceiptPage, ScheduleOccurrenceView,
    )
    if type(limit) is not int or not 1 <= limit <= 100:
        a.fail("input_invalid")
    a.guard_person(db, actor, "read")
    if ident is not None:
        a.schedule(db, actor, ident, lock=False)
    if state not in (None, "draft", "active", "paused", "needs_reapproval") or kind not in (None, "task_template", "review_summary"):
        a.fail("input_invalid")
    query = select(model).where(*a.scope(model, actor))
    if ident is not None:
        query = query.where(model.schedule_id == ident)
    if state is not None:
        query = query.where(model.state == state)
    if kind is not None:
        query = query.where(model.kind == kind)
    fingerprint = a.digest(dict(scope=[actor.organization_id, actor.store_id], actor=actor.subject_id,
        model=model.__tablename__, schedule_id=ident, state=state, kind=kind))
    if cursor is not None:
        try:
            import re
            if type(cursor) is not str or not 1 <= len(cursor) <= 256 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if type(data) is not dict or set(data) != {"t", "i", "q"} or data["q"] != fingerprint:
                raise ValueError()
            at = datetime.fromisoformat(data["t"])
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError()
            item_id = TypeAdapter(Identifier).validate_python(data["i"])
        except (ValueError, TypeError, KeyError, UnicodeError, ValidationError):
            a.fail("cursor_invalid")
        query = query.where(or_(model.created_at > at, and_(model.created_at == at, model.id > item_id)))
    rows = list(db.scalars(query.order_by(model.created_at, model.id).limit(limit+1)))
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(json.dumps(dict(t=page[-1].created_at.isoformat(),
            i=page[-1].id, q=fingerprint), separators=(",", ":")).encode()).decode().rstrip("=")
    a.allow_person(db, actor, "read", a.clock(db))
    if model is OfficeSchedule:
        return SchedulePage(items=[_summary(r) for r in page], next_cursor=next_cursor).model_dump()
    if model.__tablename__ == "office_schedule_receipts":
        return ScheduleReceiptPage(schedule_id=ident, items=[_receipt(r) for r in page], next_cursor=next_cursor).model_dump()
    return ScheduleOccurrencePage(schedule_id=ident, items=[ScheduleOccurrenceView.model_validate(
        {k: getattr(r, k) for k in ScheduleOccurrenceView.model_fields}) for r in page], next_cursor=next_cursor).model_dump()


def list_schedules(db, actor, *, limit=50, cursor=None, state=None, kind=None):
    return _page(db, actor, OfficeSchedule, limit=limit, cursor=cursor, state=state, kind=kind)


def read_occurrences(db, actor, ident, *, limit=50, cursor=None):
    from backend.app.modules.office_scheduling.ofc_008_models import OfficeScheduleOccurrence
    return _page(db, actor, OfficeScheduleOccurrence, ident=ident, limit=limit, cursor=cursor)


def read_receipts(db, actor, ident, *, limit=50, cursor=None):
    from backend.app.modules.office_scheduling.ofc_008_models import OfficeScheduleReceipt
    return _page(db, actor, OfficeScheduleReceipt, ident=ident, limit=limit, cursor=cursor)


from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header, Query
from backend.app.core.errors import ApiEnvelope
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.office_scheduling.ofc_008_schemas import (
    Identifier, Digest, ScheduleEnvelope, ScheduleListEnvelope, ScheduleOccurrencePageEnvelope,
    ScheduleReceiptPageEnvelope, ScheduleCommandStatusEnvelope,
)

router = APIRouter(prefix="/office/schedules", tags=["Office schedules"],
    responses={status: {"model": ApiEnvelope} for status in (400, 401, 403, 404, 409, 422, 500)})


def error_status(code):
    if code == "schedule_not_found":
        return 404
    if code in ("schedule_human_required", "schedule_reviewer_unavailable", "schedule_machine_unavailable"):
        return 403
    if code == "schedule_key_invalid":
        return 400
    if code in ("schedule_input_invalid", "schedule_cursor_invalid"):
        return 422
    return 409


def _api(db, fn, actor, *args, envelope=ScheduleEnvelope, write=False, **kwargs):
    try:
        result = fn(db, actor, *args, **kwargs)
        response = envelope(success=True, data=result, code="OK", error=None,
            user_message="排程记录已保存；仅生成待人审的本地建议。" if write else "排程及授权核验信息已读取。", hint=None)
        if write:
            db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def _required_key(value: Annotated[str, Header(alias="Idempotency-Key")]):
    return require_idempotency_key(value)


@router.get("/commands/status", response_model=ScheduleCommandStatusEnvelope)
def status_api(key: Annotated[str, Query(min_length=1, max_length=200)], request_digest: Digest,
               actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, command_status, actor, key, request_digest, envelope=ScheduleCommandStatusEnvelope)


@router.get("", response_model=ScheduleListEnvelope)
def list_api(limit: Annotated[int, Query(ge=1, le=100)] = 50,
             cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
             state: Literal["draft", "active", "paused", "needs_reapproval"] | None = None,
             kind: Literal["task_template", "review_summary"] | None = None,
             actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, list_schedules, actor, limit=limit, cursor=cursor, state=state, kind=kind, envelope=ScheduleListEnvelope)


@router.post("", response_model=ScheduleEnvelope)
def create_api(body: ScheduleCreateInput, key: str = Depends(_required_key),
               actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, create_schedule, actor, body, key, write=True)


@router.get("/{schedule_id}", response_model=ScheduleEnvelope)
def read_api(schedule_id: Identifier, actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, read_schedule, actor, schedule_id)


@router.put("/{schedule_id}", response_model=ScheduleEnvelope)
def replace_api(schedule_id: Identifier, body: ScheduleReplaceInput, key: str = Depends(_required_key),
                actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, replace_schedule, actor, schedule_id, body, key, write=True)


@router.post("/{schedule_id}/authorizations", response_model=ScheduleEnvelope)
def approve_api(schedule_id: Identifier, body: ScheduleApproveInput, key: str = Depends(_required_key),
                actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, approve_schedule, actor, schedule_id, body, key, write=True)


@router.post("/{schedule_id}/pause", response_model=ScheduleEnvelope)
def pause_api(schedule_id: Identifier, body: SchedulePauseInput, key: str = Depends(_required_key),
              actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, pause_schedule, actor, schedule_id, body, key, write=True)


@router.get("/{schedule_id}/occurrences", response_model=ScheduleOccurrencePageEnvelope)
def occurrences_api(schedule_id: Identifier, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                    cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
                    actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, read_occurrences, actor, schedule_id, limit=limit, cursor=cursor, envelope=ScheduleOccurrencePageEnvelope)


@router.get("/{schedule_id}/receipts", response_model=ScheduleReceiptPageEnvelope)
def receipts_api(schedule_id: Identifier, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                 cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
                 actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, read_receipts, actor, schedule_id, limit=limit, cursor=cursor, envelope=ScheduleReceiptPageEnvelope)
