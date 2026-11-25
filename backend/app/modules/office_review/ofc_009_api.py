"""REST endpoints for OFC-009 office review workbench."""
from typing import Any
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.office_review import ofc_009_service as service
from backend.app.modules.office_review.ofc_009_schemas import (
    BatchReviewDecide,
    ReviewEnvelope,
    ReviewItemClaim,
    ReviewItemCreate,
    ReviewItemDecide,
    ReviewItemOut,
)

router = APIRouter(prefix="/office/review", tags=["office_review"])


def _ok(data: Any, message: str = "ok") -> ReviewEnvelope:
    return ReviewEnvelope(
        success=True,
        data=data,
        code="OK",
        error=None,
        user_message=message,
        hint="success",
    )


@router.post("/items", response_model=ReviewEnvelope, status_code=409,
             response_description="Source not integrated; historical review records are read-only")
def create_item(
    payload: ReviewItemCreate,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    service.create_review_item(db, actor, payload)


@router.get("/items", response_model=ReviewEnvelope)
def list_items(
    state: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    items = service.list_review_items(db, actor, state=state, risk_level=risk_level, limit=limit, offset=offset)
    out = [ReviewItemOut.model_validate(i, from_attributes=True).model_dump() for i in items]
    return _ok(out, "review items retrieved")


@router.get("/items/{item_id}", response_model=ReviewEnvelope)
def get_item(
    item_id: str,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    item = service.get_review_item(db, actor, item_id)
    out = ReviewItemOut.model_validate(item, from_attributes=True)
    return _ok(out.model_dump(), "review item retrieved")


@router.post("/items/{item_id}/claim", response_model=ReviewEnvelope, status_code=409,
             response_description="Source not integrated; historical review records are read-only")
def claim_item(
    item_id: str,
    duration_seconds: int = Query(default=300, ge=30, le=1800),
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    payload = ReviewItemClaim(item_id=item_id, lock_duration_seconds=duration_seconds)
    service.claim_review_item(db, actor, payload)


@router.post("/items/{item_id}/release", response_model=ReviewEnvelope, status_code=409,
             response_description="Source not integrated; historical review records are read-only")
def release_item(
    item_id: str,
    lock_token: str | None = Query(default=None),
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    service.release_review_item(db, actor, item_id, lock_token=lock_token)


@router.post("/items/{item_id}/decide", response_model=ReviewEnvelope, status_code=409,
             response_description="Source not integrated; historical review records are read-only")
def decide_item(
    item_id: str,
    payload: ReviewItemDecide,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    if payload.item_id != item_id:
        payload = ReviewItemDecide(
            item_id=item_id,
            expected_target_version=payload.expected_target_version,
            lock_token=payload.lock_token,
            decision=payload.decision,
            reason=payload.reason,
            modifications=payload.modifications,
        )
    service.decide_review_item(db, actor, payload)


@router.post("/batch-decide", response_model=ReviewEnvelope, status_code=409,
             response_description="Source not integrated; historical review records are read-only")
def batch_decide(
    payload: BatchReviewDecide,
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    service.batch_decide_review_items(db, actor, payload)


@router.get("/export", response_model=ReviewEnvelope)
def export_reviews(
    state: str | None = Query(default=None),
    actor: AuthenticatedSubject = Depends(require_authenticated_subject),
    db: Session = Depends(get_session),
) -> ReviewEnvelope:
    summary = service.export_review_summary(db, actor, state=state)
    db.commit()
    return _ok(summary, "review export generated")
