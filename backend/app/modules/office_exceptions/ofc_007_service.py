"""Human exception ownership, with real source proofs and durable receipts.

Caller owns the transaction and MUST roll it back on any exception. No network,
Task assignment, source-domain mutation, approval or retry is performed here.
"""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4
import base64
import json
import re

from fastapi import HTTPException
from pydantic import ValidationError, TypeAdapter
from sqlalchemy import and_, or_, select, text, true

from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.models import Subject
from backend.app.modules.office_exceptions import ofc_007_sources as sources
from backend.app.modules.office_exceptions.ofc_007_models import OfficeExceptionCase, OfficeExceptionReceipt, SOURCE_POINTERS
from backend.app.modules.office_exceptions.ofc_007_schemas import (
    SourceLocator, ExceptionSource, CaseCreateInput, CaseCommandInput,
    CaseSummary, CaseView, CaseCommandReceipt, ReceiptReferences, CaseReceiptPage,
    CaseListPage, CaseCommandStatus, Identifier,
)

_CAPABILITIES = {"claim": "claim", "takeover": "takeover", "propose_transfer": "transfer",
                 "accept_transfer": "claim", "cancel_transfer": "transfer", "resolve": "resolve", "reopen": "manage"}


class OfficeExceptionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise OfficeExceptionError(code)


def _clock(db):
    sql = "SELECT clock_timestamp()" if db.get_bind().dialect.name == "postgresql" else "SELECT strftime('%Y-%m-%d %H:%M:%f','now')"
    result = db.execute(text(sql)).scalar_one()
    value = datetime.fromisoformat(result) if isinstance(result, str) else result
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _allow(db, actor, capability, now):
    if (actor.account_id is None or actor.session_id is None
            or type(actor.account_version) is not int or actor.account_version <= 0):
        _fail("exception_human_required")
    authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission="office.exceptions." + capability, now=now)
    if db.scalar(select(Subject.subject_type).where(Subject.organization_id == actor.organization_id,
                                                  Subject.id == actor.subject_id)) != "person":
        _fail("exception_human_required")


def _guard(db, actor, capability):
    lock_employee_authority(db, actor)
    now = _clock(db)
    _allow(db, actor, capability, now)
    _allow(db, actor, "read", now)
    return now


def _typed(model, body):
    if not isinstance(body, model):
        _fail("exception_input_invalid")
    try:
        return model.model_validate(body.model_dump(warnings=False))
    except (ValidationError, ValueError, TypeError):
        raise OfficeExceptionError("exception_input_invalid") from None


def _key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or value != value.strip():
        _fail("exception_key_invalid")
    return value


def _case(db, actor, ident, *, lock=True):
    query = select(OfficeExceptionCase).where(*sources.scope(OfficeExceptionCase, actor),
        OfficeExceptionCase.id == ident).execution_options(populate_existing=True)
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        _fail("exception_not_found")
    return row


def _locator(row):
    return dict(source_type=row.source_type, source_id=row.source_id, kind=row.kind)


def _available(db, actor, subject, now):
    return subject is not None and has_usable_account(db, organization_id=actor.organization_id,
        store_id=actor.store_id, subject_id=subject, now=now)


def _source(db, actor, locator):
    return sources.read_source(db, actor, locator, _clock(db))


def _source_snapshot(db, actor, locator):
    """Read a display snapshot without adding adapter parent/child row locks.

    Upstream read services may still lock domain rows. This is a best-effort
    display, not an atomic source snapshot. The employee authority fence
    still serializes with revocation.
    Commands reread and lock real sources before checking digest/version.
    """
    return sources.read_source(db, actor, locator, _clock(db), lock=False)


def read_source_context(db, actor, body: SourceLocator):
    body = _typed(SourceLocator, body)
    _guard(db, actor, "read")
    result = _source_snapshot(db, actor, body.model_dump())
    _allow(db, actor, "read", _clock(db))
    return result


def _summary(db, actor, row, now):
    return CaseSummary.model_validate(dict(**_locator(row), id=row.id, state=row.state, version=row.version,
        initial_source_digest=row.initial_source_digest, source_version=row.source_version,
        owner_id=row.owner_id, pending_owner_id=row.pending_owner_id,
        owner_available=_available(db, actor, row.owner_id, now),
        pending_owner_available=None if row.pending_owner_id is None else _available(db, actor, row.pending_owner_id, now),
        created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
        resolved_at=row.resolved_at, resolution_digest=row.resolution_digest)).model_dump()


def _command(row):
    return CaseCommandReceipt.model_validate({column.name: getattr(row, column.name)
        for column in row.__table__.columns if column.name not in ("organization_id", "store_id", "request_key")}).model_dump()


def _view(db, actor, row, *, command=None, replayed=False, lock=True):
    _allow(db, actor, "read", _clock(db))
    current, blocking = None, None
    try:
        current = (_source if lock else _source_snapshot)(db, actor, _locator(row))
    except sources.ExceptionSourceError as exc:
        # This code is raised only AFTER real source visibility was checked.
        # A 403, hidden Task, wrong scope or missing pointer never becomes a
        # readable historical case merely because it is in this queue.
        if exc.code != "exception_source_unavailable":
            raise
        blocking = "SOURCE_UNAVAILABLE"
    now = _clock(db)
    _allow(db, actor, "read", now)
    return CaseView.model_validate(dict(**_summary(db, actor, row, now), source=current,
        blocking_code=blocking, source_changed=current is not None and current["source_digest"] != row.initial_source_digest,
        command_receipt=None if command is None else _command(command), replayed=replayed)).model_dump()


def read_case(db, actor, ident):
    _guard(db, actor, "read")
    return _view(db, actor, _case(db, actor, ident, lock=False), lock=False)


def _request_digest(actor, operation, ident, body):
    return sources.digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        operation=operation, case_id=ident, input=body.model_dump(mode="json")))


def _replay(db, actor, key, request_digest):
    row = db.scalar(select(OfficeExceptionReceipt).where(*sources.scope(OfficeExceptionReceipt, actor),
        OfficeExceptionReceipt.request_key == key))
    if row is not None and (row.request_digest != request_digest or row.actor_id != actor.subject_id):
        _fail("exception_idempotency_conflict")
    return row


def _record(db, actor, row, body, key, request_digest, source, now, *, operation, from_owner=None, to_owner=None):
    resolving = operation == "resolve"
    references = ReceiptReferences.model_validate(dict(**_locator(row), source_version=source["source_version"],
        source_digest=source["source_digest"], source_state=source["source_state"],
        source_verification=source["source_verification"],
        resolution_kind=source["resolution_kind"] if resolving else None)).model_dump(mode="json")
    receipt = OfficeExceptionReceipt(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        case_id=row.id, operation=operation, case_version=row.version, request_key=key, request_digest=request_digest,
        source_digest=source["source_digest"], reason_digest=sha256(body.reason.encode()).hexdigest(),
        actor_id=actor.subject_id, from_owner_id=from_owner, to_owner_id=to_owner,
        resolution_digest=row.resolution_digest if resolving else None, references=references, created_at=now)
    db.add(receipt)
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id, store_id=actor.store_id,
        action="office.exception." + operation, target_type="office_exception", target_id=row.id,
        idempotency_key="ofc7:" + sources.digest(dict(scope=[actor.organization_id, actor.store_id], key=key)),
        details={"receipt_id": receipt.id, "case_version": row.version, "source_digest": source["source_digest"],
                 "from_owner_id": from_owner, "to_owner_id": to_owner, "state": row.state,
                 "reason_digest": receipt.reason_digest, "resolution_digest": receipt.resolution_digest}, occurred_at=now)
    db.flush()
    return receipt


def create_case(db, actor, body: CaseCreateInput, key):
    body = _typed(CaseCreateInput, body)
    key = _key(key)
    _guard(db, actor, "manage")
    request_digest = _request_digest(actor, "create", None, body)
    old = _replay(db, actor, key, request_digest)
    if old is not None:
        result = _view(db, actor, _case(db, actor, old.case_id), command=old, replayed=True)
        _allow(db, actor, "manage", _clock(db))
        return result
    locator = dict(source_type=body.source_type, source_id=body.source_id, kind=body.kind)
    current = _source(db, actor, locator)
    if current["source_digest"] != body.expected_source_digest or current["source_version"] != body.expected_source_version:
        _fail("exception_source_changed")
    if not current["anomaly_present"]:
        _fail("exception_source_has_no_anomaly")
    if db.scalar(select(OfficeExceptionCase.id).where(*sources.scope(OfficeExceptionCase, actor),
            OfficeExceptionCase.source_type == body.source_type, OfficeExceptionCase.source_id == body.source_id,
            OfficeExceptionCase.kind == body.kind)) is not None:
        _fail("exception_source_already_bound")
    now = _clock(db)
    _allow(db, actor, "manage", now)
    pointers = {column: body.source_id if source == body.source_type else None for source, column, _ in SOURCE_POINTERS}
    row = OfficeExceptionCase(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        **locator, **pointers, initial_source_digest=current["source_digest"], source_version=current["source_version"],
        state="open", version=1, created_by=actor.subject_id, create_key=key, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    receipt = _record(db, actor, row, body, key, request_digest, current, now, operation="create")
    result = _view(db, actor, row, command=receipt)
    if result["source"] is None or result["source"]["source_digest"] != current["source_digest"]:
        _fail("exception_source_changed")
    _allow(db, actor, "manage", _clock(db))
    return result


def command_case(db, actor, ident, body: CaseCommandInput, key):
    body = _typed(CaseCommandInput, body)
    key = _key(key)
    capability = _CAPABILITIES[body.operation]
    _guard(db, actor, capability)
    request_digest = _request_digest(actor, body.operation, ident, body)
    old = _replay(db, actor, key, request_digest)
    if old is not None:
        result = _view(db, actor, _case(db, actor, ident), command=old, replayed=True)
        _allow(db, actor, capability, _clock(db))
        return result
    row = _case(db, actor, ident)
    current = _source(db, actor, _locator(row))
    if row.version != body.expected_version:
        _fail("exception_version_conflict")
    if current["source_digest"] != body.expected_source_digest:
        _fail("exception_source_changed")
    now = _clock(db)
    _allow(db, actor, capability, now)
    state, owner, pending = row.state, row.owner_id, row.pending_owner_id
    resolved_at, resolution_digest = row.resolved_at, row.resolution_digest
    before_owner = owner
    operation = body.operation
    if operation == "claim":
        if state != "open":
            _fail("exception_state_conflict")
        state, owner = "owned", actor.subject_id
    elif operation == "takeover":
        if state not in ("owned", "transfer_pending") or owner == actor.subject_id:
            _fail("exception_state_conflict")
        state, owner, pending = "owned", actor.subject_id, None
    elif operation == "propose_transfer":
        if state != "owned":
            _fail("exception_state_conflict")
        if owner != actor.subject_id:
            _fail("exception_owner_required")
        if body.to_owner_id == owner or not _available(db, actor, body.to_owner_id, now):
            _fail("exception_target_unavailable")
        state, pending = "transfer_pending", body.to_owner_id
    elif operation == "accept_transfer":
        if state != "transfer_pending":
            _fail("exception_state_conflict")
        if pending != actor.subject_id:
            _fail("exception_target_required")
        state, owner, pending = "owned", actor.subject_id, None
    elif operation == "cancel_transfer":
        if state != "transfer_pending":
            _fail("exception_state_conflict")
        if owner != actor.subject_id:
            _fail("exception_owner_required")
        state, pending = "owned", None
    elif operation == "resolve":
        if state != "owned":
            _fail("exception_state_conflict")
        if owner != actor.subject_id:
            _fail("exception_owner_required")
        if not current["can_resolve"] or current["resolution_digest"] != body.expected_resolution_digest:
            _fail("exception_resolution_unverified")
        state, resolved_at, resolution_digest = "resolved", now, current["resolution_digest"]
    elif operation == "reopen":
        if state != "resolved" or not current["anomaly_present"]:
            _fail("exception_state_conflict")
        state, owner, pending, resolved_at, resolution_digest = "open", None, None, None, None
    # Complete the constrained shape before a source service can expire ORM
    # objects. _record flushes this shape and its audit/receipt atomically.
    with db.no_autoflush:
        row.version = body.expected_version + 1
        row.state, row.owner_id, row.pending_owner_id = state, owner, pending
        row.resolved_at, row.resolution_digest, row.updated_at = resolved_at, resolution_digest, now
    receipt = _record(db, actor, row, body, key, request_digest, current, now, operation=operation,
        from_owner=before_owner, to_owner=pending if operation == "propose_transfer" else owner)
    # Current operation rights, not just read visibility, must survive the full
    # source read/proof path; an expired grant rolls the outer transaction back.
    result = _view(db, actor, row, command=receipt)
    if result["source"] is None or result["source"]["source_digest"] != current["source_digest"]:
        _fail("exception_source_changed")
    _allow(db, actor, capability, _clock(db))
    return result


def read_receipts(db, actor, ident, *, after_version=0, limit=100):
    if type(after_version) is not int or after_version < 0 or type(limit) is not int or not 1 <= limit <= 100:
        _fail("exception_input_invalid")
    _guard(db, actor, "read")
    row = _case(db, actor, ident, lock=False)
    try:
        _source_snapshot(db, actor, _locator(row))
    except sources.ExceptionSourceError as exc:
        if exc.code != "exception_source_unavailable":
            raise
    receipts = list(db.scalars(select(OfficeExceptionReceipt).where(*sources.scope(OfficeExceptionReceipt, actor),
        OfficeExceptionReceipt.case_id == ident, OfficeExceptionReceipt.case_version > after_version)
        .order_by(OfficeExceptionReceipt.case_version).limit(limit + 1)))
    page = receipts[:limit]
    _allow(db, actor, "read", _clock(db))
    return CaseReceiptPage(case_id=row.id, case_version=row.version, receipts=[_command(r) for r in page],
        next_after_version=page[-1].case_version if len(receipts) > limit else None).model_dump()


def command_status(db, actor, key, request_digest):
    key = _key(key)
    if not isinstance(request_digest, str) or re.fullmatch(r"[0-9a-f]{64}", request_digest) is None:
        _fail("exception_input_invalid")
    _guard(db, actor, "read")
    receipt = db.scalar(select(OfficeExceptionReceipt).where(*sources.scope(OfficeExceptionReceipt, actor),
        OfficeExceptionReceipt.request_key == key))
    if receipt is None:
        return CaseCommandStatus(status="not_found", case=None).model_dump()
    if receipt.actor_id != actor.subject_id or receipt.request_digest != request_digest:
        _fail("exception_not_found")
    current = _view(db, actor, _case(db, actor, receipt.case_id, lock=False), command=receipt, replayed=True, lock=False)
    return CaseCommandStatus(status="found", case=current).model_dump()


def _source_permission(db, actor, permission, now):
    authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                    permission=permission, now=now)


def _can_read(db, actor, permission, now):
    try:
        _source_permission(db, actor, permission, now)
        return True
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        return False


def _task_visible(db, actor, now):
    full, own, responsible = sources.tasks._read_permissions(db, actor, now)
    if full:
        return true()
    clauses = []
    if own:
        clauses.append(or_(sources.Task.created_by == actor.subject_id, sources.Task.assignee_id == actor.subject_id))
    if responsible:
        clauses.append(sources.Task.reviewer_id == actor.subject_id)
    return or_(*clauses)


def _visible_query(db, actor, source_type, now):
    """Filter BEFORE LIMIT, including all line capabilities of ERP batches."""
    case = OfficeExceptionCase
    query = select(case).where(*sources.scope(case, actor), case.source_type == source_type)
    if source_type in ("task", "workflow"):
        task = sources.Task
        if source_type == "workflow":
            workflow = sources.OfficeWorkflow
            for permission in ("office.workflows.read", "office.intake.read", "office.extraction.read"):
                _source_permission(db, actor, permission, now)
            query = query.join(workflow, and_(case.organization_id == workflow.organization_id,
                case.store_id == workflow.store_id, case.workflow_id == workflow.id))
            if not _can_read(db, actor, "office.rules.read", now):
                query = query.where(workflow.rule_id.is_(None))
            if not _can_read(db, actor, "approvals.read", now):
                query = query.where(workflow.next_step_no <= 4)
            query = query.join(task, and_(workflow.organization_id == task.organization_id,
                workflow.store_id == task.store_id, workflow.task_id == task.id))
        else:
            query = query.join(task, and_(case.organization_id == task.organization_id,
                case.store_id == task.store_id, case.task_id == task.id))
        return query.where(_task_visible(db, actor, now))
    if source_type == "receiving_discrepancy":
        _source_permission(db, actor, "receiving.read", now)
        return query
    if source_type == "sales_handoff":
        _source_permission(db, actor, "sales.read", now)
        return query
    _source_permission(db, actor, "erp.command.read", now)
    allowed = [action for action, (_, _, permission) in sources.erp._ACTIONS.items()
               if _can_read(db, actor, permission, now)]
    if not allowed:
        _fail("exception_source_permission_denied")
    intent, request = sources.ERPCommandIntent, sources.erp.ERPCommandRequest
    if source_type == "erp_intent":
        return query.join(intent, and_(case.organization_id == intent.organization_id,
            case.store_id == intent.store_id, case.erp_intent_id == intent.id)).join(request,
            and_(intent.organization_id == request.organization_id, intent.store_id == request.store_id,
                 intent.request_id == request.id)).where(request.action.in_(allowed))
    line = sources.ERPSandboxLine
    forbidden = select(line.id).join(intent, and_(line.organization_id == intent.organization_id,
        line.store_id == intent.store_id, line.intent_id == intent.id)).join(request,
        and_(intent.organization_id == request.organization_id, intent.store_id == request.store_id,
             intent.request_id == request.id)).where(*sources.scope(line, actor),
        line.batch_id == case.erp_sandbox_batch_id, request.action.not_in(allowed)).correlate(case)
    return query.where(~forbidden.exists())


def _cursor(value, fingerprint):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 240 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
            raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (not isinstance(payload, dict) or set(payload) != {"t", "i", "q"}
                or payload["q"] != fingerprint or not isinstance(payload["t"], str)):
            raise ValueError()
        ident = TypeAdapter(Identifier).validate_python(payload["i"])
        at = datetime.fromisoformat(payload["t"])
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError()
        return at.astimezone(timezone.utc), ident
    except (ValueError, TypeError, KeyError, UnicodeError, ValidationError):
        raise OfficeExceptionError("exception_cursor_invalid") from None


def list_cases(db, actor, *, source_type, limit=50, cursor=None, kind=None, state=None, owner=None):
    if type(limit) is not int or not 1 <= limit <= 100 or source_type not in {x[0] for x in SOURCE_POINTERS}:
        _fail("exception_input_invalid")
    if state not in (None, "open", "owned", "transfer_pending", "resolved") or owner not in (None, "me", "unassigned"):
        _fail("exception_input_invalid")
    if kind is not None:
        try:
            SourceLocator(source_type=source_type, source_id="filter", kind=kind)
        except ValidationError:
            raise OfficeExceptionError("exception_input_invalid") from None
    now = _guard(db, actor, "read")
    query = _visible_query(db, actor, source_type, now)
    case = OfficeExceptionCase
    if kind is not None:
        query = query.where(case.kind == kind)
    if state is not None:
        query = query.where(case.state == state)
    if owner == "me":
        query = query.where(case.owner_id == actor.subject_id)
    elif owner == "unassigned":
        query = query.where(case.owner_id.is_(None))
    fingerprint = sources.digest(dict(scope=[actor.organization_id, actor.store_id], actor_id=actor.subject_id,
        source_type=source_type, kind=kind, state=state, owner=owner))
    if cursor is not None:
        at, ident = _cursor(cursor, fingerprint)
        query = query.where(or_(case.created_at > at, and_(case.created_at == at, case.id > ident)))
    rows = list(db.scalars(query.order_by(case.created_at, case.id).limit(limit + 1)))
    page, items = rows[:limit], []
    for row in page:
        try:
            _source_snapshot(db, actor, _locator(row))
        except sources.ExceptionSourceError as exc:
            if exc.code != "exception_source_unavailable":
                raise
        items.append(_summary(db, actor, row, _clock(db)))
    _allow(db, actor, "read", _clock(db))
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            dict(t=sources._stamp(last.created_at), i=last.id, q=fingerprint),
            separators=(",", ":")).encode()).decode().rstrip("=")
    return CaseListPage(items=items, next_cursor=next_cursor).model_dump()


# The API is deliberately confined to Case tracking. No domain dispatch or
# reconciliation function is exposed through this router.
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, Header, Query
from backend.app.core.errors import ApiEnvelope
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.office_exceptions.ofc_007_schemas import (
    SourceType, CaseKind, CaseState, Digest, CaseEnvelope, CaseListEnvelope,
    ExceptionSourceEnvelope, CaseReceiptPageEnvelope, CaseCommandStatusEnvelope,
)

router = APIRouter(prefix="/office/exceptions", tags=["Office exceptions"],
    responses={status: {"model": ApiEnvelope} for status in (400, 401, 403, 404, 409, 422, 500)})


def error_status(code):
    if code in ("exception_not_found", "exception_source_not_found"):
        return 404
    if code in ("exception_human_required", "exception_owner_required", "exception_target_required",
                "exception_target_unavailable", "exception_source_permission_denied"):
        return 403
    if code in ("exception_input_invalid", "exception_source_invalid", "exception_cursor_invalid"):
        return 422
    if code == "exception_key_invalid":
        return 400
    return 409


_DOMAIN_ERRORS = (sources.receiving.ReceivingError, sources.erp.ERPCommandError,
    sources.sandbox.SandboxCommandError, sources.sales.SalesError, sources.workflows.WorkflowError,
    sources.workflows.intake.OfficeIntakeError, sources.workflows.extraction.ExtractionError,
    sources.workflows.rules.OfficeRuleError)


def _api(db, function, actor, *args, envelope=CaseEnvelope, write=False, **kwargs):
    try:
        result = function(db, actor, *args, **kwargs)
        response = envelope(success=True, data=result, code="OK", error=None,
            user_message="异常跟踪记录已保存；原业务结果仍须按来源核对。" if write else "异常与来源核验信息已读取。", hint=None)
        if write:
            db.commit()
        return response
    except sources.ExceptionSourceError as exc:
        db.rollback()
        raise OfficeExceptionError(exc.code) from None
    except _DOMAIN_ERRORS as exc:
        db.rollback()
        if exc.code in ("erp_permission_denied", "erp_sandbox_permission_denied", "sales_permission_denied",
                        "workflow_human_required", "receiving_person_required", "erp_human_session_required"):
            code = "exception_source_permission_denied"
        elif exc.code.endswith("not_found"):
            code = "exception_source_not_found"
        else:
            code = "exception_source_unavailable"
        raise OfficeExceptionError(code) from None
    except Exception:
        db.rollback()
        raise


def _required_key(value: Annotated[str, Header(alias="Idempotency-Key")]):
    return require_idempotency_key(value)


@router.get("/source-context", response_model=ExceptionSourceEnvelope)
def source_api(source_type: SourceType, source_id: Identifier, kind: CaseKind,
               actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    try:
        locator = SourceLocator(source_type=source_type, source_id=source_id, kind=kind)
    except ValidationError:
        raise OfficeExceptionError("exception_input_invalid") from None
    return _api(db, read_source_context, actor, locator, envelope=ExceptionSourceEnvelope)


@router.get("/commands/status", response_model=CaseCommandStatusEnvelope)
def command_status_api(key: Annotated[str, Query(min_length=1, max_length=200)], request_digest: Digest,
                       actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, command_status, actor, key, request_digest, envelope=CaseCommandStatusEnvelope)


@router.get("", response_model=CaseListEnvelope)
def list_api(source_type: SourceType, limit: Annotated[int, Query(ge=1, le=100)] = 50,
             cursor: Annotated[str | None, Query(min_length=1, max_length=240)] = None,
             kind: CaseKind | None = None, state: CaseState | None = None,
             owner: Literal["me", "unassigned"] | None = None,
             actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, list_cases, actor, source_type=source_type, limit=limit, cursor=cursor,
                kind=kind, state=state, owner=owner, envelope=CaseListEnvelope)


@router.post("", response_model=CaseEnvelope)
def create_api(body: CaseCreateInput, key: str = Depends(_required_key),
               actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, create_case, actor, body, key, write=True)


@router.get("/{case_id}", response_model=CaseEnvelope)
def read_api(case_id: Identifier, actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, read_case, actor, case_id)


@router.post("/{case_id}/commands", response_model=CaseEnvelope)
def command_api(case_id: Identifier, body: CaseCommandInput, key: str = Depends(_required_key),
                actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, command_case, actor, case_id, body, key, write=True)


@router.get("/{case_id}/receipts", response_model=CaseReceiptPageEnvelope)
def history_api(case_id: Identifier, after_version: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=100)] = 100,
                actor: AuthenticatedSubject = Depends(require_authenticated_subject), db=Depends(get_session)):
    return _api(db, read_receipts, actor, case_id, after_version=after_version, limit=limit, envelope=CaseReceiptPageEnvelope)
