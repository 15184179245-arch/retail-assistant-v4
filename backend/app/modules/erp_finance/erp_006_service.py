"""ERP-006 real-person settlement command binding and execution service.

Caller owns local database transactions. Structure never authorizes;
settlement is local synthetic sandbox only and never triggers banking fund transfers.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.models import EmployeeAccount, LoginSession
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.erp_commands.erp_002_models import (
    ERPCommandApproval,
    ERPCommandIntent,
    ERPCommandRequest,
)
from backend.app.modules.erp_finance.erp_006_adapter import SyntheticSettlementHTTPAdapter
from backend.app.modules.erp_finance.erp_006_models import (
    ERPSettlementAttempt,
    ERPSettlementCommand,
    ERPSettlementObservation,
)
from backend.app.modules.erp_finance.erp_006_schemas import (
    SettlementApprovalCreate,
    SettlementProposalCreate,
    SettlementStageInput,
)
from backend.app.modules.erp_finance.erp_006_wire import (
    SettlementDispatchRequest,
    WireLine,
    canonical_digest,
    sign_wire_payload,
)
from backend.app.modules.identity.command_guard import lock_scope_security_fence
from backend.app.modules.identity.repository import grants_for, load_scope
from backend.app.modules.identity.service import permission_allowed
from backend.app.modules.office_finance.fin_001_models import FinanceWorkpaper


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fail(code: str, detail: str, status_code: int = 400) -> None:
    raise HTTPException(status_code=status_code, detail=f"{code}: {detail}")


def _require_permission(db: Any, actor: Any, permission: str, now: datetime | None = None) -> None:
    current_time = now or _now()
    org, store, subject, roles = load_scope(db, actor.organization_id, actor.store_id, actor.subject_id)
    if (
        not org
        or not org.active
        or not store
        or not store.active
        or not subject
        or not subject.active
        or subject.subject_type != "person"
    ):
        _fail("permission_denied", "inactive subject or scope", 403)
    if not has_usable_account(
        db, organization_id=actor.organization_id, store_id=actor.store_id, subject_id=actor.subject_id, now=current_time
    ):
        _fail("account_unusable", "employee account locked or expired", 403)
    grants = grants_for(db, actor.organization_id, actor.store_id, actor.subject_id, roles, permission)
    if not permission_allowed(grants, actor.subject_id, now=current_time):
        _fail("permission_denied", f"missing required permission: {permission}", 403)


def propose_settlement_command(
    db: Session,
    actor: Any,
    payload: SettlementProposalCreate,
) -> ERPSettlementCommand:
    now = _now()
    _require_permission(db, actor, "erp.settlement.propose", now)
    _require_permission(db, actor, "office.finance.read", now)
    _require_permission(db, actor, f"erp.settlement.{payload.operation}", now)

    # 1. Load workpaper and verify scope
    workpaper = db.scalar(
        select(FinanceWorkpaper).where(
            FinanceWorkpaper.organization_id == actor.organization_id,
            FinanceWorkpaper.store_id == actor.store_id,
            FinanceWorkpaper.id == payload.workpaper_id,
        )
    )
    if not workpaper:
        _fail("workpaper_not_found", "referenced finance workpaper does not exist", 404)

    if workpaper.version != payload.expected_workpaper_version:
        _fail("version_conflict", f"workpaper version is {workpaper.version}, expected {payload.expected_workpaper_version}", 409)

    # 2. Verify workpaper party & currency match
    wp_contents = workpaper.contents or {}
    if wp_contents.get("party_type") != payload.party_type:
        _fail("party_type_mismatch", "party_type does not match workpaper", 422)
    if wp_contents.get("party_ref") != payload.party_ref:
        _fail("party_ref_mismatch", "party_ref does not match workpaper", 422)
    if wp_contents.get("currency") != payload.currency:
        _fail("currency_mismatch", "currency does not match workpaper", 422)

    # 3. Verify lines sum up to settlement_amount
    total_alloc = Decimal("0")
    wp_entries = {e.get("source_id"): e for e in wp_contents.get("entries", []) if isinstance(e, dict) and "source_id" in e}
    for line in payload.lines:
        line_amt = Decimal(line.allocated_amount)
        if line_amt <= 0:
            _fail("invalid_line_amount", f"line amount {line_amt} must be positive", 422)
        total_alloc += line_amt
        if wp_entries and line.line_id not in wp_entries:
            _fail("line_not_in_workpaper", f"line {line.line_id} is not an entry in the workpaper", 422)

    if total_alloc != Decimal(payload.settlement_amount):
        _fail(
            "amount_sum_mismatch",
            f"sum of lines ({total_alloc}) does not equal settlement_amount ({payload.settlement_amount})",
            422,
        )

    # 4. Check idempotency by proposal_key
    existing = db.scalar(
        select(ERPSettlementCommand).where(
            ERPSettlementCommand.organization_id == actor.organization_id,
            ERPSettlementCommand.store_id == actor.store_id,
            ERPSettlementCommand.proposal_key == payload.proposal_key,
        )
    )
    if existing:
        return existing

    # 5. Check workpaper hasn't already been bound to an active command
    existing_wp_cmd = db.scalar(
        select(ERPSettlementCommand).where(
            ERPSettlementCommand.organization_id == actor.organization_id,
            ERPSettlementCommand.store_id == actor.store_id,
            ERPSettlementCommand.workpaper_id == payload.workpaper_id,
        )
    )
    if existing_wp_cmd:
        _fail("workpaper_already_bound", "workpaper is already bound to a settlement command", 409)

    # 6. Build digests
    source_digest = workpaper.content_sha256
    proposal_dict = payload.model_dump(mode="json")
    proposal_digest = canonical_digest(proposal_dict)

    # 7. Create ERPCommandRequest (gateway ERP-002)
    request_id = str(uuid4())
    deadline = now + timedelta(hours=1)
    params = {
        "operation": payload.operation,
        "party_type": payload.party_type,
        "party_ref": payload.party_ref,
        "currency": payload.currency,
        "settlement_amount": payload.settlement_amount,
        "proposal_key": payload.proposal_key,
    }
    request_digest = canonical_digest({
        "scope": [actor.organization_id, actor.store_id],
        "requested_by": actor.subject_id,
        "action": "settlement_document_proposal",
        "target": ["finance_workpaper", workpaper.id, workpaper.version, source_digest],
        "parameters": params,
        "expires_at": deadline.isoformat(),
    })
    erp_req = ERPCommandRequest(
        id=request_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        requested_by=actor.subject_id,
        request_key=f"erp_req:{payload.proposal_key}",
        action="settlement_document_proposal",
        target_kind="finance_workpaper",
        target_id=workpaper.id,
        expected_version=workpaper.version,
        target_digest=source_digest,
        evidence_digest=proposal_digest,
        parameters=params,
        parameters_digest=canonical_digest(params),
        request_digest=request_digest,
        expires_at=deadline,
        created_at=now,
    )
    db.add(erp_req)

    # 8. Create ERPSettlementCommand
    cmd = ERPSettlementCommand(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        workpaper_id=workpaper.id,
        request_id=request_id,
        route="api",
        operation=payload.operation,
        party_type=payload.party_type,
        party_ref=payload.party_ref,
        currency=payload.currency,
        settlement_amount=payload.settlement_amount,
        source_version=workpaper.version,
        source_digest=source_digest,
        source_snapshot=proposal_dict,
        proposal_key=payload.proposal_key,
        proposal_digest=proposal_digest,
        created_by=actor.subject_id,
        created_at=now,
    )
    db.add(cmd)

    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="erp.settlement.proposed",
        target_type="erp_settlement_command",
        target_id=cmd.id,
        idempotency_key=f"erp_prop:{payload.proposal_key}",
        details={"request_id": request_id, "amount": payload.settlement_amount, "operation": payload.operation},
    )
    db.flush()
    return cmd


def decide_settlement_command(
    db: Session,
    actor: Any,
    payload: SettlementApprovalCreate,
) -> ERPSettlementCommand:
    now = _now()
    _require_permission(db, actor, "erp.settlement.approve", now)

    cmd = db.scalar(
        select(ERPSettlementCommand).where(
            ERPSettlementCommand.organization_id == actor.organization_id,
            ERPSettlementCommand.store_id == actor.store_id,
            ERPSettlementCommand.id == payload.command_id,
        )
    )
    if not cmd:
        _fail("command_not_found", "settlement command does not exist", 404)

    # Segregation of duties: approver cannot be proposer
    if cmd.created_by == actor.subject_id:
        _fail("segregation_of_duties_violation", "proposer cannot approve their own settlement command", 403)

    if cmd.source_version != payload.expected_workpaper_version:
        _fail("version_conflict", "workpaper version changed", 409)

    req = db.scalar(
        select(ERPCommandRequest).where(
            ERPCommandRequest.organization_id == actor.organization_id,
            ERPCommandRequest.store_id == actor.store_id,
            ERPCommandRequest.id == cmd.request_id,
        )
    )
    if not req:
        _fail("request_not_found", "associated command request does not exist", 404)

    # Check if already decided
    existing_approval = db.scalar(
        select(ERPCommandApproval).where(
            ERPCommandApproval.organization_id == actor.organization_id,
            ERPCommandApproval.store_id == actor.store_id,
            ERPCommandApproval.request_id == req.id,
        )
    )
    if existing_approval:
        if existing_approval.decision == payload.decision:
            return cmd
        _fail("already_decided", "command request has already been decided", 409)

    # Record approval
    approval = ERPCommandApproval(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        request_id=req.id,
        approved_by=actor.subject_id,
        decision=payload.decision,
        request_digest=req.request_digest,
        approval_key=f"approval:{cmd.id}:{payload.decision}",
        reason=payload.reason or "settlement approval",
        expires_at=req.expires_at,
        created_at=now,
    )
    db.add(approval)

    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="erp.settlement.decided",
        target_type="erp_settlement_command",
        target_id=cmd.id,
        idempotency_key=f"erp_dec:{cmd.id}:{payload.decision}",
        details={"decision": payload.decision, "reason": payload.reason},
    )
    db.flush()
    return cmd


def stage_settlement_command(
    db: Session,
    actor: Any,
    payload: SettlementStageInput,
) -> ERPSettlementAttempt:
    now = _now()
    _require_permission(db, actor, "erp.settlement.dispatch", now)

    cmd = db.scalar(
        select(ERPSettlementCommand).where(
            ERPSettlementCommand.organization_id == actor.organization_id,
            ERPSettlementCommand.store_id == actor.store_id,
            ERPSettlementCommand.id == payload.command_id,
        )
    )
    if not cmd:
        _fail("command_not_found", "settlement command does not exist", 404)

    req = db.scalar(
        select(ERPCommandRequest).where(
            ERPCommandRequest.organization_id == actor.organization_id,
            ERPCommandRequest.store_id == actor.store_id,
            ERPCommandRequest.id == cmd.request_id,
        )
    )
    if not req:
        _fail("request_not_found", "associated command request does not exist", 404)

    approval = db.scalar(
        select(ERPCommandApproval).where(
            ERPCommandApproval.organization_id == actor.organization_id,
            ERPCommandApproval.store_id == actor.store_id,
            ERPCommandApproval.request_id == req.id,
        )
    )
    if not approval or approval.decision != "approved":
        _fail("not_approved", "command must be approved before staging", 409)

    # 3-person segregation of duties: executor cannot be proposer or approver
    if actor.subject_id in (cmd.created_by, approval.approved_by):
        _fail("segregation_of_duties_violation", "executor cannot be proposer or approver", 403)

    existing_attempt = db.scalar(
        select(ERPSettlementAttempt).where(
            ERPSettlementAttempt.organization_id == actor.organization_id,
            ERPSettlementAttempt.store_id == actor.store_id,
            ERPSettlementAttempt.command_id == cmd.id,
        )
    )
    if existing_attempt:
        return existing_attempt

    # Verify security fence and account
    epoch = lock_scope_security_fence(db, actor.organization_id, actor.store_id)
    account = db.scalar(
        select(EmployeeAccount).where(
            EmployeeAccount.organization_id == actor.organization_id,
            EmployeeAccount.store_id == actor.store_id,
            EmployeeAccount.subject_id == actor.subject_id,
            EmployeeAccount.active.is_(True),
        )
    )
    if not account:
        _fail("account_invalid", "active employee account required", 403)

    session = None
    if getattr(actor, "session_id", None):
        session = db.scalar(
            select(LoginSession).where(
                LoginSession.account_id == account.id,
                LoginSession.id == actor.session_id,
                LoginSession.revoked_at.is_(None),
            )
        )
    if not session:
        session = db.scalar(
            select(LoginSession).where(
                LoginSession.account_id == account.id,
                LoginSession.revoked_at.is_(None),
            ).order_by(LoginSession.created_at.desc())
        )
    if not session:
        _fail("session_invalid", "active login session required", 401)

    expiry = min(_as_utc(session.expires_at), _as_utc(req.expires_at), _as_utc(approval.expires_at))
    if now >= expiry:
        _fail("approval_expired", "approval or session expired", 409)

    # Create ERPCommandIntent
    intent_id = str(uuid4())
    intent = ERPCommandIntent(
        id=intent_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        request_id=req.id,
        approval_id=approval.id,
        executor_id=actor.subject_id,
        idempotency_key=f"intent:{payload.queue_key}",
        request_digest=req.request_digest,
        target_digest=req.target_digest,
        created_at=now,
    )
    db.add(intent)

    queue_digest = canonical_digest({"queue_key": payload.queue_key, "command_id": cmd.id})
    input_digest = canonical_digest(cmd.source_snapshot)
    initial_alloc = [
        {"line_id": line["line_id"], "status": "staged", "allocated_amount": line["allocated_amount"]}
        for line in cmd.source_snapshot.get("lines", [])
    ]

    attempt = ERPSettlementAttempt(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        command_id=cmd.id,
        intent_id=intent_id,
        queue_key=payload.queue_key,
        queue_digest=queue_digest,
        input_digest=input_digest,
        executor_id=actor.subject_id,
        account_id=account.id,
        session_id=session.id,
        account_version=account.version,
        authorization_epoch=epoch or 1,
        authorization_expires_at=expiry,
        state="staged",
        version=1,
        allocation_results=initial_alloc,
        created_at=now,
        updated_at=now,
    )
    db.add(attempt)

    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="erp.settlement.staged",
        target_type="erp_settlement_attempt",
        target_id=attempt.id,
        idempotency_key=f"erp_stg:{payload.queue_key}",
        details={"command_id": cmd.id, "intent_id": intent_id},
    )
    db.flush()
    return attempt


def dispatch_settlement_attempt(
    db: Session,
    actor: Any,
    attempt_id: str,
    adapter: SyntheticSettlementHTTPAdapter,
) -> ERPSettlementAttempt:
    now = _now()
    _require_permission(db, actor, "erp.settlement.dispatch", now)

    attempt = db.scalar(
        select(ERPSettlementAttempt).where(
            ERPSettlementAttempt.organization_id == actor.organization_id,
            ERPSettlementAttempt.store_id == actor.store_id,
            ERPSettlementAttempt.id == attempt_id,
        ).with_for_update()
    )
    if not attempt:
        _fail("attempt_not_found", "settlement attempt does not exist", 404)

    if attempt.state not in ("staged", "unknown"):
        _fail("invalid_attempt_state", f"cannot dispatch attempt in state {attempt.state}", 409)

    cmd = db.scalar(
        select(ERPSettlementCommand).where(
            ERPSettlementCommand.organization_id == actor.organization_id,
            ERPSettlementCommand.store_id == actor.store_id,
            ERPSettlementCommand.id == attempt.command_id,
        )
    )
    if not cmd:
        _fail("command_not_found", "associated command missing", 404)

    # 1. Commit pre-dispatch marker
    attempt.state = "dispatched"
    attempt.dispatched_at = now
    attempt.updated_at = now
    db.flush()

    # 2. Build wire request
    lines = [
        WireLine(
            line_id=l["line_id"],
            document_ref=l["document_ref"],
            allocated_amount=l["allocated_amount"],
            note=l.get("note"),
        )
        for l in cmd.source_snapshot.get("lines", [])
    ]
    raw_req = {
        "contract_id": "retail.erp-settlement-sandbox",
        "adapter_id": "synthetic.settlement.v1",
        "protocol_version": 1,
        "mode": "synthetic",
        "organization_id": actor.organization_id,
        "store_id": actor.store_id,
        "route": "api",
        "attempt_id": attempt.id,
        "command_id": cmd.id,
        "workpaper_id": cmd.workpaper_id,
        "operation": cmd.operation,
        "party_type": cmd.party_type,
        "party_ref": cmd.party_ref,
        "currency": cmd.currency,
        "settlement_amount": cmd.settlement_amount,
        "source_version": cmd.source_version,
        "source_digest": cmd.source_digest,
        "proposal_digest": cmd.proposal_digest,
        "lines": [l.model_dump() for l in lines],
        "nonce": "0123456789abcdef0123456789abcdef",
    }
    raw_req["signature"] = sign_wire_payload(raw_req)
    wire_req = SettlementDispatchRequest.model_validate(raw_req)

    # 3. Call adapter
    try:
        ack = adapter.dispatch(wire_req)
    except Exception as exc:
        attempt.state = "unknown"
        attempt.updated_at = _now()
        obs = ERPSettlementObservation(
            id=str(uuid4()),
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            attempt_id=attempt.id,
            command_id=cmd.id,
            observed_at=_now(),
            observed_state="unknown",
            receipt_digest=canonical_digest({"error": str(exc), "attempt_id": attempt.id}),
            details={"error": str(exc)},
            recorded_by=actor.subject_id,
        )
        db.add(obs)
        record_event(
            db,
            actor_id=actor.subject_id,
            organization_id=actor.organization_id,
            store_id=actor.store_id,
            action="erp.settlement.dispatch_failed",
            target_type="erp_settlement_attempt",
            target_id=attempt.id,
            idempotency_key=f"erp_fail:{attempt.id}:{now.isoformat()}",
            details={"error": str(exc)},
        )
        db.flush()
        return attempt

    # 4. Success handling
    attempt.state = ack.status
    attempt.delivery_ref = ack.delivery_ref
    attempt.external_ref = ack.external_ref
    attempt.receipt_digest = ack.receipt_digest
    attempt.allocation_results = ack.allocation_results
    attempt.updated_at = _now()

    obs = ERPSettlementObservation(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        attempt_id=attempt.id,
        command_id=cmd.id,
        observed_at=_now(),
        observed_state=ack.status,
        receipt_digest=ack.receipt_digest,
        details={"ack": ack.model_dump()},
        recorded_by=actor.subject_id,
    )
    db.add(obs)
    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="erp.settlement.dispatched",
        target_type="erp_settlement_attempt",
        target_id=attempt.id,
        idempotency_key=f"erp_ack:{attempt.id}:{ack.status}",
        details={"status": ack.status, "external_ref": ack.external_ref},
    )
    db.flush()
    return attempt


def reconcile_settlement_attempt(
    db: Session,
    actor: Any,
    attempt_id: str,
    adapter: SyntheticSettlementHTTPAdapter,
) -> ERPSettlementAttempt:
    now = _now()
    _require_permission(db, actor, "erp.settlement.reconcile", now)

    attempt = db.scalar(
        select(ERPSettlementAttempt).where(
            ERPSettlementAttempt.organization_id == actor.organization_id,
            ERPSettlementAttempt.store_id == actor.store_id,
            ERPSettlementAttempt.id == attempt_id,
        ).with_for_update()
    )
    if not attempt:
        _fail("attempt_not_found", "settlement attempt does not exist", 404)

    # Reconcile via GET readback
    ack = adapter.readback(attempt.id, attempt.command_id, attempt.receipt_digest)
    attempt.state = ack.status
    attempt.delivery_ref = ack.delivery_ref
    attempt.external_ref = ack.external_ref
    attempt.receipt_digest = ack.receipt_digest
    attempt.updated_at = _now()

    obs = ERPSettlementObservation(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        attempt_id=attempt.id,
        command_id=attempt.command_id,
        observed_at=_now(),
        observed_state=ack.status,
        receipt_digest=ack.receipt_digest,
        details={"reconciled": True, "ack": ack.model_dump()},
        recorded_by=actor.subject_id,
    )
    db.add(obs)
    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="erp.settlement.reconciled",
        target_type="erp_settlement_attempt",
        target_id=attempt.id,
        idempotency_key=f"erp_rec:{attempt.id}:{now.isoformat()}",
        details={"status": ack.status, "external_ref": ack.external_ref},
    )
    db.flush()
    return attempt
