"""ERP-006 settlement REST endpoints with standard six-field envelope."""
from typing import Any
from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.erp_commands.erp_003_wire import LoopbackSandboxConfig
from backend.app.modules.erp_finance import erp_006_service as service
from backend.app.modules.erp_finance.erp_006_adapter import SyntheticSettlementHTTPAdapter
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


def _get_adapter(actor: Any) -> SyntheticSettlementHTTPAdapter:
    cfg = LoopbackSandboxConfig(
        host="127.0.0.1",
        port=8000,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        secret="sandbox_0123456789abcdef0123456789abcdef",
    )
    return SyntheticSettlementHTTPAdapter(cfg)


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


@router.post("/attempts/{attempt_id}/dispatch", response_model=SettlementEnvelope)
def dispatch_attempt(
    attempt_id: str,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    adapter = _get_adapter(actor)
    attempt = service.dispatch_settlement_attempt(db, actor, attempt_id, adapter)
    db.commit()
    return _ok(
        {
            "attempt_id": attempt.id,
            "state": attempt.state,
            "delivery_ref": attempt.delivery_ref,
            "external_ref": attempt.external_ref,
            "receipt_digest": attempt.receipt_digest,
        },
        "settlement attempt dispatched",
    )


@router.post("/attempts/{attempt_id}/reconcile", response_model=SettlementEnvelope)
def reconcile_attempt(
    attempt_id: str,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> SettlementEnvelope:
    adapter = _get_adapter(actor)
    attempt = service.reconcile_settlement_attempt(db, actor, attempt_id, adapter)
    db.commit()
    return _ok(
        {
            "attempt_id": attempt.id,
            "state": attempt.state,
            "delivery_ref": attempt.delivery_ref,
            "external_ref": attempt.external_ref,
            "receipt_digest": attempt.receipt_digest,
        },
        "settlement attempt reconciled",
    )
