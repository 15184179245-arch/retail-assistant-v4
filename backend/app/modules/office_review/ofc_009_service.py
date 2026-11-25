"""OFC-009 unified office review workbench service.

Historical review records only. Unwired client-authored approval mutations are
retired; actual domain approval services remain the only authoritative writers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.service import has_usable_account
from backend.app.modules.identity.repository import grants_for, load_scope
from backend.app.modules.identity.service import permission_allowed
from backend.app.modules.office_review.ofc_009_models import (
    OfficeReviewDecision,
    OfficeReviewItem,
)
from backend.app.modules.office_review.ofc_009_schemas import (
    BatchReviewDecide,
    BatchReviewOut,
    ReviewItemClaim,
    ReviewItemCreate,
    ReviewItemDecide,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(code: str, detail: str, status_code: int = 400) -> None:
    raise HTTPException(status_code=status_code, detail=f"{code}: {detail}")


def _require_permission(db: Session, actor: Any, permission: str, now: datetime | None = None) -> None:
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


def create_review_item(
    db: Session,
    actor: Any,
    payload: ReviewItemCreate,
) -> OfficeReviewItem:
    _require_permission(db, actor, "office.review.read")
    raise HTTPException(409, "office_review_source_not_integrated")


def list_review_items(
    db: Session,
    actor: Any,
    state: str | None = None,
    risk_level: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[OfficeReviewItem]:
    now = _now()
    _require_permission(db, actor, "office.review.read", now)

    query = select(OfficeReviewItem).where(
        OfficeReviewItem.organization_id == actor.organization_id,
        OfficeReviewItem.store_id == actor.store_id,
    )
    if state:
        query = query.where(OfficeReviewItem.state == state)
    if risk_level:
        query = query.where(OfficeReviewItem.risk_level == risk_level)

    query = query.order_by(OfficeReviewItem.created_at.desc()).offset(offset).limit(limit)
    items = list(db.scalars(query).all())

    return items


def get_review_item(
    db: Session,
    actor: Any,
    item_id: str,
) -> OfficeReviewItem:
    now = _now()
    _require_permission(db, actor, "office.review.read", now)

    item = db.scalar(
        select(OfficeReviewItem).where(
            OfficeReviewItem.organization_id == actor.organization_id,
            OfficeReviewItem.store_id == actor.store_id,
            OfficeReviewItem.id == item_id,
        )
    )
    if not item:
        _fail("item_not_found", f"review item {item_id} not found", 404)

    return item


def claim_review_item(
    db: Session,
    actor: Any,
    payload: ReviewItemClaim,
) -> OfficeReviewItem:
    _require_permission(db, actor, "office.review.claim")
    raise HTTPException(409, "office_review_source_not_integrated")


def release_review_item(
    db: Session,
    actor: Any,
    item_id: str,
    lock_token: str | None = None,
) -> OfficeReviewItem:
    _require_permission(db, actor, "office.review.claim")
    raise HTTPException(409, "office_review_source_not_integrated")


def decide_review_item(
    db: Session,
    actor: Any,
    payload: ReviewItemDecide,
) -> tuple[OfficeReviewItem, OfficeReviewDecision]:
    _require_permission(db, actor, "office.review.decide")
    raise HTTPException(409, "office_review_source_not_integrated")


def batch_decide_review_items(
    db: Session,
    actor: Any,
    payload: BatchReviewDecide,
) -> BatchReviewOut:
    _require_permission(db, actor, "office.review.batch")
    _require_permission(db, actor, "office.review.decide")
    raise HTTPException(409, "office_review_source_not_integrated")


def export_review_summary(
    db: Session,
    actor: Any,
    state: str | None = None,
) -> dict[str, Any]:
    now = _now()
    _require_permission(db, actor, "office.review.export", now)

    query = select(OfficeReviewDecision).where(
        OfficeReviewDecision.organization_id == actor.organization_id,
        OfficeReviewDecision.store_id == actor.store_id,
    )
    if state:
        query = query.where(OfficeReviewDecision.decision == state)
    query = query.order_by(OfficeReviewDecision.decided_at.desc()).limit(200)

    decisions = list(db.scalars(query).all())
    export_records = [
        {
            "id": d.id,
            "item_id": d.item_id,
            "target_kind": d.target_kind,
            "target_id": d.target_id,
            "target_version": d.target_version,
            "decision": d.decision,
            "modifications": d.modifications,
            "actor_id": d.actor_id,
            "reason": d.reason,
            "receipt_digest": d.receipt_digest,
            "decided_at": d.decided_at.isoformat(),
        }
        for d in decisions
    ]

    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="office.review.exported",
        target_type="office_review_export",
        target_id=f"exp_{now.strftime('%Y%m%d%H%M%S')}",
        idempotency_key=f"ori_exp:{now.isoformat()}",
        details={"record_count": len(export_records)},
    )
    db.flush()
    return {
        "exported_at": now.isoformat(),
        "record_count": len(export_records),
        "records": export_records,
    }
