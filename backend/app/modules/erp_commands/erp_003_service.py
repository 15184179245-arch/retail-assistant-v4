"""Synthetic ERP transaction boundary. Never authorizes a formal ERP write.

Session-level functions leave commit/rollback to the caller. Network entry
points own short transactions and close them before entering the adapter.
Persisted dispatch intent is irreversible: recovery can only read back.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.db.session import get_session
from backend.app.modules.audit.service import record_event
from backend.app.modules.erp_commands import erp_002_service as gateway
from backend.app.modules.erp_commands.erp_002_models import ERPCommandRequest, ERPCommandIntent
from backend.app.modules.erp_commands.erp_003_models import (
    ERPSandboxBatch, ERPSandboxLine, ERPSandboxObservation,
)
from backend.app.modules.erp_commands.erp_003_schemas import SandboxQueueInput, SandboxExecutionReceipt, SandboxExecutionEnvelope
from backend.app.modules.erp_commands.erp_003_wire import (
    SandboxDispatchRequest, SandboxReadbackReceipt, canonical_digest,
    queue_input_digest, line_input_digest,
)
from backend.app.modules.erp_commands.erp_003_adapter import SandboxAdapterError
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.identity.models import ScopeSecurityFence
from backend.app.modules.identity.service import authorize_scope
from backend.app.workers.outbox import enqueue

EVENT_TYPE = "erp.sandbox.execute.v1"
router = APIRouter(prefix="/erp/sandbox-executions", tags=["Synthetic ERP sandbox"])


class SandboxCommandError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise SandboxCommandError(code)


def _clock(db: Session) -> datetime:
    sql = "SELECT clock_timestamp()" if db.get_bind().dialect.name == "postgresql" else "SELECT CURRENT_TIMESTAMP"
    value = db.execute(text(sql)).scalar_one()
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _allow(db, actor, permission, now):
    if not actor.account_id or not actor.session_id or not actor.account_version:
        _fail("erp_sandbox_human_required")
    try:
        authorize_scope(db, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)
    except HTTPException:
        raise SandboxCommandError("erp_sandbox_permission_denied") from None


def _guard(db, actor, permission):
    try:
        lock_employee_authority(db, actor)
    except HTTPException:
        raise SandboxCommandError("erp_sandbox_permission_denied") from None
    now = _clock(db)
    _allow(db, actor, permission, now)
    return now


def _epoch(db, actor):
    return db.scalar(select(ScopeSecurityFence.version).where(
        ScopeSecurityFence.organization_id == actor.organization_id,
        ScopeSecurityFence.store_id == actor.store_id))


def _scope(model, actor):
    return model.organization_id == actor.organization_id, model.store_id == actor.store_id


def _batch(db, actor, ident):
    row = db.scalar(select(ERPSandboxBatch).where(*_scope(ERPSandboxBatch, actor),
        ERPSandboxBatch.id == ident).execution_options(populate_existing=True).with_for_update())
    if row is None:
        _fail("erp_sandbox_not_found")
    return row


def _lines(db, actor, batch):
    return list(db.scalars(select(ERPSandboxLine).where(*_scope(ERPSandboxLine, actor),
        ERPSandboxLine.batch_id == batch.id).order_by(ERPSandboxLine.line_no)
        .execution_options(populate_existing=True).with_for_update()))


def _request(db, actor, ident):
    row = db.scalar(select(ERPCommandRequest).where(*_scope(ERPCommandRequest, actor),
        ERPCommandRequest.id == ident).execution_options(populate_existing=True).with_for_update())
    if row is None:
        _fail("erp_sandbox_command_not_found")
    return row


def _intent_request(db, actor, line):
    intent = db.scalar(select(ERPCommandIntent).where(*_scope(ERPCommandIntent, actor),
        ERPCommandIntent.id == line.intent_id).execution_options(populate_existing=True).with_for_update())
    if intent is None:
        _fail("erp_sandbox_binding_changed")
    return intent, _request(db, actor, intent.request_id)


def _source_read(db, actor, rows, now):
    _allow(db, actor, "erp.command.read", now)
    for line in rows:
        _, request = _intent_request(db, actor, line)
        if request.action not in gateway._ACTIONS:
            _fail("erp_sandbox_binding_changed")
        _allow(db, actor, gateway._ACTIONS[request.action][2], now)


def _staged(db, actor, request, now):
    """Recompute the original approval binding; approval is never a permission."""
    if request.action == "goods_receipt_document_proposal":
        _fail("erp_sandbox_receipt_entry_required")
    if request.action == "catalog_maintenance_proposal":
        _fail("erp_sandbox_catalog_entry_required")
    if request.action not in gateway._ACTIONS:
        _fail("erp_sandbox_binding_changed")
    _allow(db, actor, gateway._ACTIONS[request.action][2], now)
    approval = gateway._approval(db, actor, request)
    intent = gateway._intent(db, actor, request)
    if (approval is None or intent is None or approval.decision != "approved"
            or intent.approval_id != approval.id or intent.executor_id != actor.subject_id
            or len({request.requested_by, approval.approved_by, intent.executor_id}) != 3
            or intent.request_digest != request.request_digest
            or approval.request_digest != request.request_digest
            or intent.target_digest != request.target_digest
            or gateway._outcome(db, actor, intent) is not None):
        _fail("erp_sandbox_approval_invalid")
    if now >= min(request.expires_at, approval.expires_at):
        _fail("erp_sandbox_approval_expired")
    params = gateway._params(request.action, request.parameters)
    digest = canonical_digest({
        "scope": [actor.organization_id, actor.store_id], "requested_by": request.requested_by,
        "action": request.action,
        "target": [request.target_kind, request.target_id, request.expected_version, request.target_digest],
        "source_sha256": request.evidence_digest, "parameters": params,
        "expires_at": request.expires_at.isoformat(),
    })
    if digest != request.request_digest or canonical_digest(params) != request.parameters_digest:
        _fail("erp_sandbox_binding_changed")
    version, target_digest, kind = gateway._target(db, actor, request.action, request.target_id)
    if (version != request.expected_version or target_digest != request.target_digest
            or kind != request.target_kind):
        _fail("erp_sandbox_target_changed")
    return intent, approval


def _wire_line(request, intent, *, ident, number):
    value = dict(line_id=ident, line_no=number, intent_id=intent.id, command_id=request.id,
        request_digest=request.request_digest, action=request.action, target_kind=request.target_kind,
        target_id=request.target_id, target_version=request.expected_version, target_digest=request.target_digest,
        source_sha256=request.evidence_digest, parameters=request.parameters,
        parameters_digest=request.parameters_digest)
    value["input_digest"] = line_input_digest(value)
    return value


def _receipt(db, actor, batch, rows=None, *, replayed=False):
    rows = _lines(db, actor, batch) if rows is None else rows
    now = _clock(db)
    _source_read(db, actor, rows, now)
    values = []
    for line in rows:
        intent, _ = _intent_request(db, actor, line)
        values.append(dict(id=line.id, command_id=intent.request_id, intent_id=line.intent_id,
            line_no=line.line_no, input_digest=line.input_digest, state=line.state,
            external_ref=line.external_ref, receipt_digest=line.receipt_digest,
            error_code=line.error_code, version=line.version))
    return SandboxExecutionReceipt.model_validate(dict(
        id=batch.id, state=batch.state, input_digest=batch.input_digest, version=batch.version,
        lines=values, created_at=batch.created_at, updated_at=batch.updated_at,
        dispatched_at=batch.dispatched_at, replayed=replayed)).model_dump()


def _audit(db, actor, batch, operation, key, details, now):
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action="erp.sandbox." + operation,
        target_type="erp_sandbox_batch", target_id=batch.id,
        idempotency_key="erp-sandbox:" + canonical_digest({"batch": batch.id, "operation": operation, "key": key}),
        details=details, occurred_at=now)


def queue_execution(db: Session, actor: AuthenticatedSubject, body: SandboxQueueInput, key: str):
    gateway._text(key, "idempotency_key")
    body = SandboxQueueInput.model_validate(body.model_dump())
    now = _guard(db, actor, "erp.sandbox.dispatch")
    _allow(db, actor, "erp.command.stage", now)
    digest = queue_input_digest(actor.organization_id, actor.store_id,
        body.adapter_id, [c.model_dump() for c in body.commands])
    old = db.scalar(select(ERPSandboxBatch).where(*_scope(ERPSandboxBatch, actor), ERPSandboxBatch.queue_key == key))
    if old is not None:
        if old.input_digest != digest or old.executor_id != actor.subject_id:
            _fail("erp_sandbox_idempotency_conflict")
        return _receipt(db, actor, old, replayed=True)
    checked = {}
    expiry = actor.auth_expires_at
    for ref in sorted(body.commands, key=lambda c: c.command_id):
        request = _request(db, actor, ref.command_id)
        if ref.request_digest != request.request_digest:
            _fail("erp_sandbox_binding_changed")
        intent, approval = _staged(db, actor, request, now)
        if db.scalar(select(ERPSandboxLine.id).where(*_scope(ERPSandboxLine, actor),
                ERPSandboxLine.intent_id == intent.id)) is not None:
            _fail("erp_sandbox_intent_bound")
        expiry = min(expiry, request.expires_at, approval.expires_at)
        checked[request.id] = (request, intent)
    batch = ERPSandboxBatch(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        queue_key=key, input_digest=digest, adapter_id=body.adapter_id, contract_version=1, mode="synthetic",
        executor_id=actor.subject_id, account_id=actor.account_id, session_id=actor.session_id,
        account_version=actor.account_version, authorization_epoch=_epoch(db, actor),
        authorization_expires_at=expiry, state="prepared", version=1, created_at=now, updated_at=now)
    db.add(batch)
    db.flush()
    rows = []
    for number, ref in enumerate(body.commands, 1):
        request, intent = checked[ref.command_id]
        value = _wire_line(request, intent, ident=str(uuid4()), number=number)
        row = ERPSandboxLine(id=value["line_id"], organization_id=actor.organization_id, store_id=actor.store_id,
            batch_id=batch.id, intent_id=intent.id, line_no=number, request_digest=request.request_digest,
            target_digest=request.target_digest, parameters_digest=request.parameters_digest,
            input_digest=value["input_digest"], approved_parameters=request.parameters, state="pending", version=1)
        db.add(row)
        rows.append(row)
    db.flush()
    _snapshot(db, actor, batch, rows)
    enqueue(db, organization_id=actor.organization_id, store_id=actor.store_id,
        event_type=EVENT_TYPE, idempotency_key="erp-sandbox:" + batch.id,
        payload=event_payload(batch), now=now)
    _audit(db, actor, batch, "queued", key, {"input_digest": digest, "line_count": len(rows)}, now)
    return _receipt(db, actor, batch, rows)


def event_payload(batch):
    return dict(batch_id=batch.id, input_digest=batch.input_digest, contract_id="retail.erp-sandbox",
                adapter_id="synthetic.erp.v1", protocol_version=1, mode="synthetic")


def read_execution(db, actor, ident):
    _guard(db, actor, "erp.command.read")
    return _receipt(db, actor, _batch(db, actor, ident))


def _original_actor(db, actor, batch, now):
    if (batch.executor_id != actor.subject_id or batch.account_id != actor.account_id
            or batch.session_id != actor.session_id or batch.account_version != actor.account_version
            or batch.authorization_epoch != _epoch(db, actor)
            or now >= batch.authorization_expires_at):
        _fail("erp_sandbox_authorization_changed")


def _snapshot(db, actor, batch, rows):
    values = []
    for line in rows:
        intent, request = _intent_request(db, actor, line)
        value = _wire_line(request, intent, ident=line.id, number=line.line_no)
        if (value["input_digest"] != line.input_digest
                or request.request_digest != line.request_digest
                or request.target_digest != line.target_digest
                or request.parameters_digest != line.parameters_digest
                or request.parameters != line.approved_parameters):
            _fail("erp_sandbox_binding_changed")
        values.append(value)
    try:
        return SandboxDispatchRequest.model_validate(dict(organization_id=actor.organization_id,
            store_id=actor.store_id, batch_id=batch.id, input_digest=batch.input_digest, lines=values))
    except ValidationError:
        raise SandboxCommandError("erp_sandbox_binding_changed") from None


def prepare_dispatch(db, actor, ident):
    now = _guard(db, actor, "erp.sandbox.dispatch")
    _allow(db, actor, "erp.command.stage", now)
    batch = _batch(db, actor, ident)
    rows = _lines(db, actor, batch)
    _source_read(db, actor, rows, now)
    if batch.state != "prepared":
        return None
    _original_actor(db, actor, batch, now)
    for line in sorted(rows, key=lambda line: line.intent_id):
        _, request = _intent_request(db, actor, line)
        _staged(db, actor, request, now)
    snapshot = _snapshot(db, actor, batch, rows)
    batch.state = "dispatched"
    batch.dispatched_at = now
    batch.updated_at = now
    batch.version += 1
    _observe(db, actor, batch, "send_intent", "send-intent", {"input_digest": batch.input_digest}, now)
    _audit(db, actor, batch, "dispatch_intent", "once", {"input_digest": batch.input_digest}, now)
    db.flush()
    return snapshot


def _observe(db, actor, batch, kind, key, evidence, now, *, error=None):
    old = db.scalar(select(ERPSandboxObservation).where(*_scope(ERPSandboxObservation, actor),
        ERPSandboxObservation.batch_id == batch.id, ERPSandboxObservation.observation_key == key))
    if old is None:
        db.add(ERPSandboxObservation(id=str(uuid4()), organization_id=actor.organization_id,
            store_id=actor.store_id, batch_id=batch.id, observation_key=key, kind=kind,
            evidence_digest=canonical_digest(evidence), error_code=error,
            normalized_receipt=evidence, observed_at=now))


def _aggregate(batch, rows, now):
    states = {line.state for line in rows}
    batch.state = next(iter(states)) if len(states) == 1 else "partial"
    batch.updated_at = now
    batch.version += 1


def apply_readback(db, actor, snapshot, receipt):
    now = _guard(db, actor, "erp.sandbox.reconcile")
    batch = _batch(db, actor, snapshot.batch_id)
    rows = _lines(db, actor, batch)
    _source_read(db, actor, rows, now)
    if batch.dispatched_at is None:
        _fail("erp_sandbox_not_dispatched")
    expected = _snapshot(db, actor, batch, rows)
    if expected.model_dump() != snapshot.model_dump():
        _fail("erp_sandbox_binding_changed")
    if receipt is None:
        values = None
        evidence = {"batch_id": batch.id, "input_digest": batch.input_digest, "result": "not_found_unknown"}
    else:
        receipt = SandboxReadbackReceipt.model_validate(receipt.model_dump())
        if (receipt.organization_id != actor.organization_id or receipt.store_id != actor.store_id
                or receipt.batch_id != batch.id or receipt.input_digest != batch.input_digest):
            _fail("erp_sandbox_receipt_invalid")
        values = {line.line_id: line for line in receipt.lines}
        if len(values) != len(rows) or set(values) != {line.id for line in rows}:
            _fail("erp_sandbox_receipt_invalid")
        for line in rows:
            observed = values[line.id]
            intent, _ = _intent_request(db, actor, line)
            if (observed.line_no != line.line_no or observed.intent_id != line.intent_id
                    or observed.command_id != intent.request_id or observed.input_digest != line.input_digest):
                _fail("erp_sandbox_receipt_invalid")
        # Authentication nonce/signature vary for each GET; persist only business facts.
        evidence = receipt.model_dump(exclude={"nonce", "signature"})
    evidence_key = "receipt:" + canonical_digest(evidence)
    existing = db.scalar(select(ERPSandboxObservation.id).where(*_scope(ERPSandboxObservation, actor),
        ERPSandboxObservation.batch_id == batch.id, ERPSandboxObservation.observation_key == evidence_key))
    if existing is not None:
        return _receipt(db, actor, batch, rows, replayed=True)
    for line in rows:
        observed = values[line.id] if values else None
        state = {"accepted": "sandbox_verified", "rejected": "failed", "unknown": "unknown"}[observed.status] if observed else "unknown"
        if line.state in ("sandbox_verified", "failed"):
            if state == "unknown":
                continue
            if (line.state != state or line.external_ref != observed.external_ref
                    or line.error_code != observed.error_code):
                _fail("erp_sandbox_receipt_conflict")
            continue
        line.state = state
        line.external_ref = observed.external_ref if observed else None
        line.error_code = observed.error_code if observed else "SANDBOX_NOT_FOUND"
        line.receipt_digest = canonical_digest(observed.model_dump()) if observed and state != "unknown" else None
        line.version += 1
    _aggregate(batch, rows, now)
    _observe(db, actor, batch, "readback_receipt", evidence_key, evidence, now)
    _audit(db, actor, batch, "readback", evidence_key,
           {"evidence_digest": canonical_digest(evidence), "mode": "synthetic"}, now)
    db.flush()
    return _receipt(db, actor, batch, rows)


def _record_network_error(db, actor, ident, *, dispatch):
    permission = "erp.sandbox.dispatch" if dispatch else "erp.sandbox.reconcile"
    now = _guard(db, actor, permission)
    batch = _batch(db, actor, ident)
    rows = _lines(db, actor, batch)
    _source_read(db, actor, rows, now)
    if dispatch:
        _original_actor(db, actor, batch, now)
    if batch.dispatched_at is None:
        _fail("erp_sandbox_not_dispatched")
    kind = "send_error" if dispatch else "readback_error"
    old = db.scalar(select(ERPSandboxObservation.id).where(*_scope(ERPSandboxObservation, actor),
        ERPSandboxObservation.batch_id == batch.id, ERPSandboxObservation.observation_key == kind))
    if old is not None:
        return _receipt(db, actor, batch, rows, replayed=True)
    for line in rows:
        if line.state == "pending":
            line.state = "unknown"
            line.error_code = "SANDBOX_TRANSPORT_UNKNOWN"
            line.version += 1
    _aggregate(batch, rows, now)
    evidence = {"batch_id": ident, "result": "transport_unknown"}
    _observe(db, actor, batch, kind, kind, evidence, now, error="SANDBOX_TRANSPORT_UNKNOWN")
    _audit(db, actor, batch, kind, kind, evidence, now)
    db.flush()
    return _receipt(db, actor, batch, rows)


def dispatch_execution(factory, actor, ident, adapter=None, *, enabled=False):
    """No open transaction spans any HTTP. Once prepared, never POST again."""
    if not enabled or adapter is None:
        _fail("erp_sandbox_disabled")
    with factory.begin() as db:
        snapshot = prepare_dispatch(db, actor, ident)
    if snapshot is None:
        with factory.begin() as db:
            return read_execution(db, actor, ident)
    try:
        adapter.dispatch(snapshot)
    except SandboxAdapterError:
        with factory.begin() as db:
            return _record_network_error(db, actor, ident, dispatch=True)
    # ACK means only received. It is never a business-success observation.
    with factory.begin() as db:
        now = _guard(db, actor, "erp.sandbox.dispatch")
        batch = _batch(db, actor, ident)
        _original_actor(db, actor, batch, now)
        return _receipt(db, actor, batch)


def reconcile_execution(factory, actor, ident, adapter=None):
    if adapter is None:
        _fail("erp_sandbox_unconfigured")
    with factory.begin() as db:
        now = _guard(db, actor, "erp.sandbox.reconcile")
        batch = _batch(db, actor, ident)
        rows = _lines(db, actor, batch)
        _source_read(db, actor, rows, now)
        if batch.dispatched_at is None:
            _fail("erp_sandbox_not_dispatched")
        snapshot = _snapshot(db, actor, batch, rows)
    try:
        receipt = adapter.readback(snapshot)
    except SandboxAdapterError:
        with factory.begin() as db:
            return _record_network_error(db, actor, ident, dispatch=False)
    with factory.begin() as db:
        return apply_readback(db, actor, snapshot, receipt)


def _http_error(exc):
    if isinstance(exc, gateway.ERPCommandError):
        return gateway._http(exc)
    status = 409
    if exc.code in ("erp_sandbox_permission_denied", "erp_sandbox_human_required"):
        status = 403
    elif exc.code in ("erp_sandbox_not_found", "erp_sandbox_command_not_found"):
        status = 404
    elif exc.code in ("erp_sandbox_disabled", "erp_sandbox_unconfigured"):
        status = 503
    return HTTPException(status, exc.code)


def _envelope(value):
    return SandboxExecutionEnvelope(success=True, data=SandboxExecutionReceipt.model_validate(value),
        code="OK", error=None, user_message="隔离测试记录已更新，请逐行核对；此记录不是正式ERP单据。", hint=None)


def _required_key(value: Annotated[str, Header(alias="Idempotency-Key")]):
    return require_idempotency_key(value)


@router.post("", response_model=SandboxExecutionEnvelope)
def queue_api(body: SandboxQueueInput,
              actor: AuthenticatedSubject = Depends(require_authenticated_subject),
              db: Session = Depends(get_session), key: str = Depends(_required_key)):
    try:
        result = queue_execution(db, actor, body, key)
        db.commit()
        return _envelope(result)
    except (SandboxCommandError, gateway.ERPCommandError) as exc:
        db.rollback()
        raise _http_error(exc) from None


@router.get("/{batch_id}", response_model=SandboxExecutionEnvelope)
def read_api(batch_id: str, actor: AuthenticatedSubject = Depends(require_authenticated_subject),
             db: Session = Depends(get_session)):
    try:
        return _envelope(read_execution(db, actor, batch_id))
    except (SandboxCommandError, gateway.ERPCommandError) as exc:
        db.rollback()
        raise _http_error(exc) from None


@router.post("/{batch_id}/dispatch", response_model=SandboxExecutionEnvelope)
def dispatch_api(batch_id: str, request: Request,
                 actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                 db: Session = Depends(get_session), key: str = Depends(_required_key)):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        result = dispatch_execution(factory, actor, batch_id,
            getattr(request.app.state, "erp_sandbox_adapter", None), enabled=os.getenv("ERP_SANDBOX_ENABLED") == "1")
        return _envelope(result)
    except (SandboxCommandError, gateway.ERPCommandError) as exc:
        raise _http_error(exc) from None


@router.post("/{batch_id}/reconcile", response_model=SandboxExecutionEnvelope)
def reconcile_api(batch_id: str, request: Request,
                  actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                  db: Session = Depends(get_session), key: str = Depends(_required_key)):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    try:
        return _envelope(reconcile_execution(factory, actor, batch_id,
            getattr(request.app.state, "erp_sandbox_adapter", None)))
    except (SandboxCommandError, gateway.ERPCommandError) as exc:
        raise _http_error(exc) from None
