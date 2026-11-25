"""POS delivery/receipt worker boundary (SALE-004); synthetic only."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.integrations.guanjia.pos_handoff import (
    PosDeliveryFailed, PosGateway, deliver_handoff, reconcile_receipts,
    deliver_handoff_owned, reconcile_receipts_owned, _factory_from_session,
)


def process_handoff(session: Session, actor: AuthenticatedSubject, handoff_id: str,
                    gateway: PosGateway, *, now: datetime | None = None) -> dict:
    """Deliver once, then poll once; never auto-resend an unknown result.

    The delivery service durably reserves intent before calling the gateway.
    This worker never commits unrelated caller changes.
    """
    return process_handoff_owned(_factory_from_session(session), actor, handoff_id, gateway)


def process_handoff_owned(session_factory, actor, handoff_id, gateway):
    """Each local phase owns a fresh Session; all gateway calls run between them."""
    outcome = deliver_handoff_owned(session_factory, actor, handoff_id, gateway, with_outcome=True)
    handoff = outcome.handoff
    if handoff.delivery_ref and handoff.handoff_state in ("sent", "receipt_unknown"):
        handoff = reconcile_receipts_owned(session_factory, actor, handoff_id, gateway)
    return {"handoff_id": handoff.id, "state": handoff.handoff_state,
            "delivery_ref": handoff.delivery_ref,
            "external_ref": handoff.external_ref}
