"""ERP-006 settlement REST endpoints with standard six-field envelope."""
from typing import Any
from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.erp_finance import erp_006_service as service
from backend.app.modules.erp_finance.erp_006_schemas import (
    SettlementApprovalCreate,
    SettlementEnvelope,
    SettlementProposalCreate,
    SettlementStageInput,
)

router = APIRouter(prefix="/erp/settlement", tags=["erp_settlement"])


def _ok(data: Any, message: str = "ok") -> SettlementEnvelope:
    return SettlementEnvelope(
        success=True,
        data=data,
        code="OK",
        error=None,
        user_message=message,
        hint="success",
    )


def _unavailable(db, actor, permission) -> SettlementEnvelope:
    service._require_permission(db, actor, permission)
    return SettlementEnvelope(success=False, data=None,
        code="ERP_SETTLEMENT_ADAPTER_UNAVAILABLE", error="settlement_adapter_unavailable",
        user_message="结算执行适配器尚未接入，未执行或推进结算状态。",
        hint="本地提案、审批和暂存保留；不可将其视为真实ERP或资金结算。")


@router.post("/proposals", response_model=SettlementEnvelope)
def create_proposal(
    payload: SettlementProposalCreate,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    cmd = service.propose_settlement_command(db, actor, payload)
    db.commit()
    return _ok({"command_id": cmd.id, "proposal_key": cmd.proposal_key}, "settlement proposal created")


@router.post("/decisions", response_model=SettlementEnvelope)
def decide_proposal(
    payload: SettlementApprovalCreate,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    cmd = service.decide_settlement_command(db, actor, payload)
    db.commit()
    return _ok({"command_id": cmd.id, "decision": payload.decision}, "settlement proposal decided")


@router.post("/stages", response_model=SettlementEnvelope)
def stage_command(
    payload: SettlementStageInput,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    attempt = service.stage_settlement_command(db, actor, payload)
    db.commit()
    return _ok({"attempt_id": attempt.id, "state": attempt.state}, "settlement command staged")


@router.post("/attempts/{attempt_id}/dispatch", response_model=SettlementEnvelope, status_code=503,
             response_description="Settlement integration unavailable; no state transition performed")
def dispatch_attempt(
    attempt_id: str,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    return _unavailable(db, actor, "erp.settlement.dispatch")


@router.post("/attempts/{attempt_id}/reconcile", response_model=SettlementEnvelope, status_code=503,
             response_description="Settlement integration unavailable; no state transition performed")
def reconcile_attempt(
    attempt_id: str,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    return _unavailable(db, actor, "erp.settlement.reconcile")
