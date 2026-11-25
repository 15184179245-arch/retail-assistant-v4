"""ERP-002 fail-closed employee-only command authorization gateway.

NO production ERP adapter, outbound network call, dynamic URL or SQL execution.
One transaction per command operation: state record + idempotency + audit.
Approvals do not grant permissions; each actor and target is revalidated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.core.idempotency import require_idempotency_key
from backend.app.core.errors import ApiEnvelope
from backend.app.db.session import get_session
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.models import EmployeeAccount
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.command_guard import lock_employee_authority
from backend.app.modules.purchasing.models import PurchaseIntent
from backend.app.modules.sales.models import SalesDraft
from backend.app.modules.receiving.models import ReceivingBatch
from backend.app.modules.erp_catalog.erp_007_models import ERPCatalogHead
from backend.app.modules.erp_commands.erp_002_models import (
    ERPCommandRequest, ERPCommandApproval, ERPCommandIntent, ERPCommandOutcome,
)
from backend.app.modules.erp_commands.erp_002_schemas import (
    ERPCommandPropose, ERPCommandDecide, ERPCommandStage, ERPCommandOut,
    ERPCommandOutcomeInput, ERPCommandEnvelope,
)

router = APIRouter(prefix="/erp/commands", tags=["ERP command approvals"])
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ACTIONS = {
    "catalog_field_proposal": (CatalogProduct, "catalog_product", "catalog.read"),
    "purchase_document_proposal": (PurchaseIntent, "purchase_intent", "purchasing.read"),
    "sales_return_proposal": (SalesDraft, "sales_draft", "sales.read"),
    "sales_document_proposal": (SalesDraft, "sales_draft", "sales.read"),
    "goods_receipt_document_proposal": (ReceivingBatch, "receiving_batch", "receiving.read"),
    "catalog_maintenance_proposal": (ERPCatalogHead, "catalog_maintenance_target", "catalog.read"),
}
# Deliberately NO base URL, POST endpoint, ERP mapping or sending callback.
_ERP_WRITE_ENABLED = False


class ERPCommandError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise ERPCommandError(code)


def _utc(t: datetime) -> datetime:
    if not isinstance(t, datetime) or t.tzinfo is None or t.utcoffset() is None:
        _fail("erp_time_invalid")
    return t.astimezone(timezone.utc)


def _canonical(value: dict) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("erp_parameters_invalid")


def _digest(value: dict) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str, max_len: int = 200):
    if not isinstance(value, str) or not 1 <= len(value) <= max_len or value.strip() != value:
        _fail("erp_" + name + "_invalid")
    return value


def _sha(value: str):
    if not isinstance(value, str) or _HEX.fullmatch(value) is None:
        _fail("erp_sha256_invalid")
    return value


def _params(action: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        _fail("erp_parameters_invalid")
    # No arbitrary ERP paths, SQL, code, permissions or resource identifiers.
    expected = {
        "catalog_field_proposal": {"field_name", "proposed_value"},
        "purchase_document_proposal": {"memo"},
        "sales_return_proposal": {"reason"},
        "sales_document_proposal": {"route", "source_digest", "handoff_id"},
        "goods_receipt_document_proposal": {"route", "source_digest"},
        "catalog_maintenance_proposal": {"operation", "action_digest", "context_digest"},
    }
    if action not in expected or set(payload) != expected[action]:
        _fail("erp_parameters_invalid")
    for key, val in payload.items():
        _text(val, key, 400)
    if action == "sales_document_proposal":
        if payload["route"] not in ("api", "pos"):
            _fail("erp_parameters_invalid")
        _sha(payload["source_digest"])
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,35}", payload["handoff_id"]) is None:
            _fail("erp_parameters_invalid")
    if action == "goods_receipt_document_proposal":
        if payload["route"] != "api":
            _fail("erp_parameters_invalid")
        _sha(payload["source_digest"])
    if action == "catalog_maintenance_proposal":
        if payload["operation"] not in ("text_update", "image_upload", "image_bind"):
            _fail("erp_parameters_invalid")
        _sha(payload["action_digest"])
        _sha(payload["context_digest"])
    if action == "catalog_field_proposal" and payload["field_name"] not in (
            "display_label", "specification_note"):
        _fail("erp_field_not_allowed")
    return json.loads(_canonical(payload))


def _employee(db: Session, actor: AuthenticatedSubject, permission: str):
    if actor.account_id is None or actor.session_id is None:
        _fail("erp_human_session_required")
    db.flush()
    db.expire_all()
    try:
        authorize_scope(db, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission)
    except HTTPException as exc:
        raise ERPCommandError("erp_permission_denied") from exc
    return actor


def _normal_sales_permissions(db, actor, action, capability):
    """Generic entry points must not bypass ERP-004's per-action policy.

    Reuse the same real-person fence, database clock and current role checks;
    approvals/intents still use the original ERP-002 ledger and separation.
    """
    if action != "sales_document_proposal":
        return
    from backend.app.modules.erp_sales import erp_004_source as source
    try:
        now = source.guard(db, actor, "erp.sales." + capability)
        source.require_person(db, actor, "erp.sales.read", now)
        source.require_person(db, actor, "erp.command.read", now)
        if capability == "propose":
            source.require_person(db, actor, "sales.handoff", now)
        if capability == "dispatch":
            source.require_person(db, actor, "erp.sales.reconcile", now)
    except (HTTPException, source.ERPSalesError):
        raise ERPCommandError("erp_permission_denied") from None


def _receiving_permissions(db, actor, action, capability):
    """The generic approval ledger cannot bypass the receiving command policy."""
    if action != "goods_receipt_document_proposal":
        return
    from backend.app.modules.erp_purchasing import erp_005_source as source
    try:
        now = source.guard(db, actor, "erp.receiving." + capability)
        if capability == "dispatch":
            source.require_person(db, actor, "erp.receiving.reconcile", now)
    except (HTTPException, source.ERPReceiptError):
        raise ERPCommandError("erp_permission_denied") from None


def _maintenance_permissions(db, actor, action, capability, parameters):
    """New maintenance is a distinct domain; legacy field proposals stay unchanged."""
    if action != "catalog_maintenance_proposal":
        return
    from backend.app.modules.erp_catalog import erp_007_source as source
    try:
        now = source.guard(db, actor, "erp.catalog." + capability)
        if capability != "read":
            operation = _params(action, parameters)["operation"]
            source.require_person(db, actor, "erp.catalog." + operation, now)
            if operation != "text_update":
                source.require_person(db, actor, "media.read", now)
    except (HTTPException, source.ERPCatalogError):
        raise ERPCommandError("erp_permission_denied") from None


def _store_lock(db: Session, actor: AuthenticatedSubject):
    try:
        lock_employee_authority(db, actor)
    except HTTPException as exc:
        raise ERPCommandError("erp_permission_denied") from exc


def _target(db: Session, actor: AuthenticatedSubject, action: str, ident: str):
    model, kind, permission = _ACTIONS[action]
    _employee(db, actor, permission)
    q = select(model).where(
        model.id == ident, model.organization_id == actor.organization_id,
        model.store_id == actor.store_id,
    ).execution_options(populate_existing=True).with_for_update()
    row = db.scalar(q)
    if row is None:
        _fail("erp_object_not_found")
    values = {"kind": kind, "id": row.id, "version": row.version,
              "organization_id": row.organization_id, "store_id": row.store_id}
    if action == "catalog_field_proposal":
        if not row.active:
            _fail("erp_object_inactive")
        values.update(source_version=row.source_version,
                      snapshot_id=row.current_snapshot_id)
    elif action == "purchase_document_proposal":
        values.update(status=row.status, evidence_id=row.evidence_id)
    elif action == "sales_document_proposal":
        # The ERP-004 source service validates all confirmations/current quotes,
        # exact two-stage rounding, full lines, scope and the single handoff.
        from backend.app.modules.erp_sales.erp_004_source import snapshot, source_digest
        current = snapshot(db, actor, ident, require_handoff=True)
        return current.draft_version, source_digest(current), kind
    elif action == "catalog_maintenance_proposal":
        from backend.app.modules.erp_catalog.erp_007_source import base_snapshot
        current = base_snapshot(db, actor, ident)
        return current.target_version, _digest(current.model_dump(mode="json")), kind
    elif action == "goods_receipt_document_proposal":
        from backend.app.modules.erp_purchasing.erp_005_source import snapshot, source_digest
        current = snapshot(db, actor, ident)
        return current.batch_version, source_digest(current), kind
    elif action == "sales_return_proposal":
        if row.voided_at is not None:
            _fail("erp_object_inactive")
        values.update(status=row.status, stage=row.stage,
                      total_amount=row.total_amount, currency=row.currency)
    return row.version, _digest(values), kind


def _request(db: Session, actor: AuthenticatedSubject, request_id: str):
    row = db.scalar(select(ERPCommandRequest).where(
        ERPCommandRequest.id == request_id,
        ERPCommandRequest.organization_id == actor.organization_id,
        ERPCommandRequest.store_id == actor.store_id,
    ).execution_options(populate_existing=True))
    if row is None:
        _fail("erp_request_not_found")
    return row


def _approval(db: Session, actor: AuthenticatedSubject, row: ERPCommandRequest):
    return db.scalar(select(ERPCommandApproval).where(
        ERPCommandApproval.organization_id == actor.organization_id,
        ERPCommandApproval.store_id == actor.store_id,
        ERPCommandApproval.request_id == row.id,
    ).execution_options(populate_existing=True))


def _intent(db: Session, actor: AuthenticatedSubject, row: ERPCommandRequest):
    return db.scalar(select(ERPCommandIntent).where(
        ERPCommandIntent.organization_id == actor.organization_id,
        ERPCommandIntent.store_id == actor.store_id,
        ERPCommandIntent.request_id == row.id,
    ).execution_options(populate_existing=True))


def _outcome(db: Session, actor: AuthenticatedSubject, intent: ERPCommandIntent):
    return db.scalar(select(ERPCommandOutcome).where(
        ERPCommandOutcome.organization_id == actor.organization_id,
        ERPCommandOutcome.store_id == actor.store_id,
        ERPCommandOutcome.intent_id == intent.id,
    ).execution_options(populate_existing=True))


def _state(db: Session, actor: AuthenticatedSubject, row: ERPCommandRequest):
    appr = _approval(db, actor, row)
    intent = _intent(db, actor, row)
    outcome = _outcome(db, actor, intent) if intent is not None else None
    if outcome is not None:
        return outcome.outcome
    if intent is not None:
        return "staged"
    if appr is not None:
        return appr.decision
    return "pending"


def _response(db: Session, actor: AuthenticatedSubject, row: ERPCommandRequest,
              replayed: bool = False) -> dict:
    # The receipt is actionable without a private DB lookup. Business details
    # are visible only with the source capability in this authenticated scope.
    _employee(db, actor, _ACTIONS[row.action][2])
    _receiving_permissions(db, actor, row.action, "read")
    _maintenance_permissions(db, actor, row.action, "read", row.parameters)
    approval = _approval(db, actor, row)
    return {"id": row.id, "state": _state(db, actor, row),
            "request_digest": row.request_digest, "target_digest": row.target_digest,
            "expires_at": row.expires_at, "replayed": replayed,
            "request": {"action": row.action, "target_id": row.target_id,
                        "target_version": row.expected_version,
                        "requested_by": row.requested_by,
                        "source_sha256": row.evidence_digest,
                        "business_parameters": row.parameters},
            "approval": None if approval is None else {
                "id": approval.id, "approved_by": approval.approved_by,
                "decision": approval.decision, "reason": approval.reason,
                "request_digest": approval.request_digest,
                "expires_at": approval.expires_at}}


def _audit(db, actor, operation, request_id, key, details):
    record_event(db, actor_id=actor.subject_id,
                 organization_id=actor.organization_id, store_id=actor.store_id,
                 action="erp.command." + operation, target_type="erp_command_request",
                 target_id=request_id, idempotency_key="erp:" + operation + ":" + _digest(
                     {"scope": [actor.organization_id, actor.store_id], "key": key}),
                 details=details)


def propose_command(db: Session, actor: AuthenticatedSubject,
                    body: ERPCommandPropose, request_key: str, *,
                    now: datetime | None = None) -> dict:
    """Prepare approval candidate; NEVER sends to ERP."""
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    _text(request_key, "idempotency_key")
    _employee(db, actor, "erp.command.propose")
    if body.action not in _ACTIONS:
        _fail("erp_action_unsupported")
    _normal_sales_permissions(db, actor, body.action, "propose")
    _receiving_permissions(db, actor, body.action, "propose")
    params = _params(body.action, body.business_parameters)
    _maintenance_permissions(db, actor, body.action, "propose", params)
    _sha(body.source_sha256)
    deadline = _utc(body.expires_at)
    if not instant < deadline <= instant + timedelta(hours=1):
        _fail("erp_approval_window_invalid")
    _store_lock(db, actor)
    _employee(db, actor, "erp.command.propose")
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    if not instant < deadline <= instant + timedelta(hours=1):
        _fail("erp_approval_window_invalid")
    version, target_digest, kind = _target(db, actor, body.action, body.target_id)
    if version != body.target_version:
        _fail("erp_object_version_changed")
    if body.action == "catalog_maintenance_proposal" and body.source_sha256 != params["context_digest"]:
        _fail("erp_request_digest_changed")
    if body.action == "goods_receipt_document_proposal" and (
            params["source_digest"] != target_digest or body.source_sha256 != target_digest):
        _fail("erp_request_digest_changed")
    payload = {
        "scope": [actor.organization_id, actor.store_id], "requested_by": actor.subject_id,
        "action": body.action, "target": [kind, body.target_id, version, target_digest],
        "source_sha256": body.source_sha256, "parameters": params,
        "expires_at": deadline.isoformat(),
    }
    digest = _digest(payload)
    existing = db.scalar(select(ERPCommandRequest).where(
        ERPCommandRequest.organization_id == actor.organization_id,
        ERPCommandRequest.store_id == actor.store_id,
        ERPCommandRequest.request_key == request_key))
    if existing is not None:
        if existing.request_digest != digest:
            _fail("erp_idempotency_conflict")
        return _response(db, actor, existing, replayed=True)
    row = ERPCommandRequest(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        requested_by=actor.subject_id, request_key=request_key, action=body.action,
        target_kind=kind, target_id=body.target_id, expected_version=version,
        target_digest=target_digest, evidence_digest=body.source_sha256,
        parameters=params, parameters_digest=_digest(params),
        request_digest=digest, expires_at=deadline, created_at=instant)
    db.add(row)
    db.flush()
    _audit(db, actor, "proposed", row.id, request_key, {
        "request_digest": digest, "target_digest": target_digest,
        "action": body.action, "state": "pending"})
    return _response(db, actor, row)


def decide_command(db: Session, actor: AuthenticatedSubject, request_id: str,
                   body: ERPCommandDecide, request_key: str, *,
                   now: datetime | None = None) -> dict:
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    _text(request_key, "idempotency_key")
    _employee(db, actor, "erp.command.approve")
    _sha(body.request_digest)
    reason = _text(body.reason, "reason", 1000)
    _store_lock(db, actor)
    _employee(db, actor, "erp.command.approve")
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    row = _request(db, actor, request_id)
    _normal_sales_permissions(db, actor, row.action, "approve")
    _receiving_permissions(db, actor, row.action, "approve")
    _maintenance_permissions(db, actor, row.action, "approve", row.parameters)
    if row.request_digest != body.request_digest:
        _fail("erp_request_digest_changed")
    existing = _approval(db, actor, row)
    deadline = _utc(body.expires_at)
    if existing is not None:
        if (existing.approval_key != request_key or existing.approved_by != actor.subject_id
                or existing.decision != body.decision or existing.reason != reason
                or _utc(existing.expires_at) != deadline):
            _fail("erp_approval_consumed")
        return _response(db, actor, row, replayed=True)
    if actor.subject_id == row.requested_by:
        _fail("erp_self_approval_denied")
    if instant >= _utc(row.expires_at) or not instant < deadline <= _utc(row.expires_at):
        _fail("erp_approval_expired")
    version, target_digest, kind = _target(db, actor, row.action, row.target_id)
    if version != row.expected_version or target_digest != row.target_digest:
        _fail("erp_object_changed")
    decision = ERPCommandApproval(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        request_id=row.id, approved_by=actor.subject_id, decision=body.decision,
        request_digest=row.request_digest, approval_key=request_key, reason=reason,
        expires_at=deadline, created_at=instant)
    db.add(decision)
    db.flush()
    _audit(db, actor, body.decision, row.id, request_key, {
        "request_digest": row.request_digest, "approval_id": decision.id,
        "expires_at": deadline.isoformat(), "separate_human": True})
    return _response(db, actor, row)


def stage_command(db: Session, actor: AuthenticatedSubject, request_id: str,
                  body: ERPCommandStage, request_key: str, *,
                  now: datetime | None = None) -> dict:
    """Stage exactly one LOCAL intent. No executor/ERP invocation exists here."""
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    _text(request_key, "idempotency_key")
    _employee(db, actor, "erp.command.stage")
    _sha(body.request_digest)
    _store_lock(db, actor)
    _employee(db, actor, "erp.command.stage")
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    row = _request(db, actor, request_id)
    _normal_sales_permissions(db, actor, row.action, "dispatch")
    _receiving_permissions(db, actor, row.action, "dispatch")
    _maintenance_permissions(db, actor, row.action, "dispatch", row.parameters)
    if row.request_digest != body.request_digest:
        _fail("erp_request_digest_changed")
    approval = _approval(db, actor, row)
    if approval is None or approval.decision != "approved" or body.approval_id != approval.id:
        _fail("erp_approval_missing")
    existing = _intent(db, actor, row)
    if existing is not None:
        if existing.idempotency_key != request_key or existing.executor_id != actor.subject_id:
            _fail("erp_approval_consumed")
        if body.expected_target_version != row.expected_version:
            _fail("erp_idempotency_conflict")
        return _response(db, actor, row, replayed=True)
    if actor.subject_id in (row.requested_by, approval.approved_by):
        _fail("erp_executor_separation_denied")
    if (instant >= _utc(row.expires_at) or instant >= _utc(approval.expires_at)
            or approval.request_digest != row.request_digest):
        _fail("erp_approval_expired")
    version, target_digest, kind = _target(db, actor, row.action, row.target_id)
    if (version != body.expected_target_version or version != row.expected_version
            or target_digest != row.target_digest):
        _fail("erp_object_changed")
    command = ERPCommandIntent(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        request_id=row.id, approval_id=approval.id, executor_id=actor.subject_id,
        idempotency_key=request_key, request_digest=row.request_digest,
        target_digest=target_digest, created_at=instant)
    db.add(command)
    db.flush()
    _audit(db, actor, "intent_staged", row.id, request_key, {
        "approval_id": approval.id, "intent_id": command.id,
        "target_digest": target_digest, "external_dispatch": "DISABLED"})
    return _response(db, actor, row)


def read_command(db: Session, actor: AuthenticatedSubject, request_id: str) -> dict:
    _employee(db, actor, "erp.command.read")
    return _response(db, actor, _request(db, actor, request_id))


def reconcile_manual_only(db: Session, actor: AuthenticatedSubject, request_id: str,
                          body: ERPCommandOutcomeInput, request_key: str, *,
                          now: datetime | None = None) -> dict:
    """Trusted-host-only unknown/rejected receipt; no unverifiable 'success'."""
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    _text(request_key, "idempotency_key")
    _employee(db, actor, "erp.command.approve")
    _sha(body.evidence_sha256)
    if body.outcome == "verified_external":
        _fail("erp_external_receipt_verification_unavailable")
    _store_lock(db, actor)
    _employee(db, actor, "erp.command.approve")
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    row = _request(db, actor, request_id)
    if row.action == "catalog_maintenance_proposal":
        _maintenance_permissions(db, actor, row.action, "reconcile", row.parameters)
        _fail("erp_catalog_entry_required")
    intent = _intent(db, actor, row)
    if intent is None:
        _fail("erp_intent_missing")
    old = _outcome(db, actor, intent)
    if old is not None:
        if (old.request_key != request_key or old.outcome != body.outcome
                or old.evidence_ref != body.evidence_ref
                or old.evidence_sha256 != body.evidence_sha256
                or old.verified_by != actor.subject_id):
            _fail("erp_outcome_immutable")
        return _response(db, actor, row, replayed=True)
    out = ERPCommandOutcome(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        intent_id=intent.id, verified_by=actor.subject_id, outcome=body.outcome,
        evidence_ref=_text(body.evidence_ref, "evidence_ref"),
        evidence_sha256=body.evidence_sha256, request_key=request_key,
        occurred_at=instant)
    db.add(out)
    db.flush()
    _audit(db, actor, "manual_" + body.outcome, row.id, request_key, {
        "outcome_id": out.id, "evidence_sha256": out.evidence_sha256,
        "no_erp_network_dispatch": True})
    return _response(db, actor, row)


def _http(exc: ERPCommandError):
    code = exc.code
    if code in ("erp_request_not_found", "erp_object_not_found"):
        return HTTPException(404, code)
    if code in ("erp_permission_denied", "erp_human_session_required",
                "erp_self_approval_denied", "erp_executor_separation_denied"):
        return HTTPException(403, code)
    return HTTPException(409, code)


def _envelope(data: dict):
    return ERPCommandEnvelope(success=True, code="OK", error=None,
                              user_message="ERP 命令仅已本地登记，尚未执行上游写入。",
                              hint=None, data=ERPCommandOut.model_validate(data))


@router.post("", response_model=ERPCommandEnvelope)
def command_propose(body: ERPCommandPropose,
                    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                    db: Session = Depends(get_session),
                    key: str = Depends(require_idempotency_key)):
    try:
        if body.action == "sales_document_proposal":
            _fail("erp_sales_entry_required")
        if body.action == "goods_receipt_document_proposal":
            _fail("erp_receipt_entry_required")
        if body.action == "catalog_maintenance_proposal":
            _fail("erp_catalog_entry_required")
        result = propose_command(db, actor, body, key)
        db.commit()
        return _envelope(result)
    except ERPCommandError as exc:
        db.rollback()
        raise _http(exc) from exc


@router.post("/{request_id}/decide", response_model=ERPCommandEnvelope)
def command_decide(request_id: str, body: ERPCommandDecide,
                   actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                   db: Session = Depends(get_session),
                   key: str = Depends(require_idempotency_key)):
    try:
        _employee(db, actor, "erp.command.approve")
        if _request(db, actor, request_id).action == "goods_receipt_document_proposal":
            _receiving_permissions(db, actor, "goods_receipt_document_proposal", "approve")
            _fail("erp_receipt_entry_required")
        if _request(db, actor, request_id).action == "catalog_maintenance_proposal":
            _maintenance_permissions(db, actor, "catalog_maintenance_proposal", "approve", _request(db, actor, request_id).parameters)
            _fail("erp_catalog_entry_required")
        result = decide_command(db, actor, request_id, body, key)
        db.commit()
        return _envelope(result)
    except ERPCommandError as exc:
        db.rollback()
        raise _http(exc) from exc


@router.post("/{request_id}/stage", response_model=ERPCommandEnvelope)
def command_stage(request_id: str, body: ERPCommandStage,
                  actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                  db: Session = Depends(get_session),
                  key: str = Depends(require_idempotency_key)):
    try:
        _employee(db, actor, "erp.command.stage")
        if _request(db, actor, request_id).action == "goods_receipt_document_proposal":
            _receiving_permissions(db, actor, "goods_receipt_document_proposal", "dispatch")
            _fail("erp_receipt_entry_required")
        if _request(db, actor, request_id).action == "catalog_maintenance_proposal":
            _maintenance_permissions(db, actor, "catalog_maintenance_proposal", "dispatch", _request(db, actor, request_id).parameters)
            _fail("erp_catalog_entry_required")
        result = stage_command(db, actor, request_id, body, key)
        db.commit()
        return _envelope(result)
    except ERPCommandError as exc:
        db.rollback()
        raise _http(exc) from exc


@router.get("/{request_id}", response_model=ERPCommandEnvelope)
def command_read(request_id: str,
                 actor: AuthenticatedSubject = Depends(require_authenticated_subject),
                 db: Session = Depends(get_session)):
    try:
        return _envelope(read_command(db, actor, request_id))
    except ERPCommandError as exc:
        raise _http(exc) from exc
