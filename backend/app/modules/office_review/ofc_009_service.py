"""OFC-009 unified office review workbench service.

Aggregates clerical reviews, optimistic locking, concurrency claim guards,
side-by-side diffs, itemized batch risk calculation, and immutable receipts.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

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
    BatchItemResult,
    BatchReviewDecide,
    BatchReviewOut,
    ReviewItemClaim,
    ReviewItemCreate,
    ReviewItemDecide,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fail(code: str, detail: str, status_code: int = 400) -> None:
    raise HTTPException(status_code=status_code, detail=f"{code}: {detail}")


def _canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_digest(data: Any) -> str:
    raw = _canonical_json(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    now = _now()
    _require_permission(db, actor, "office.review.read", now)

    existing = db.scalar(
        select(OfficeReviewItem).where(
            OfficeReviewItem.organization_id == actor.organization_id,
            OfficeReviewItem.store_id == actor.store_id,
            OfficeReviewItem.target_kind == payload.target_kind,
            OfficeReviewItem.target_id == payload.target_id,
        )
    )
    if existing:
        if existing.state == "pending":
            existing.target_version = payload.target_version
            existing.title = payload.title
            existing.summary = payload.summary
            existing.risk_level = payload.risk_level
            existing.diff_payload = payload.diff_payload
            existing.updated_at = now
            db.flush()
        return existing

    item = OfficeReviewItem(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        target_kind=payload.target_kind,
        target_id=payload.target_id,
        target_version=payload.target_version,
        title=payload.title,
        summary=payload.summary,
        risk_level=payload.risk_level,
        diff_payload=payload.diff_payload,
        state="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="office.review.item_created",
        target_type="office_review_item",
        target_id=item.id,
        idempotency_key=f"ori_crt:{item.id}",
        details={"target_kind": payload.target_kind, "target_id": payload.target_id},
    )
    db.flush()
    return item


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

    # Release expired locks in-memory / state visibility
    for item in items:
        if item.state == "claimed" and item.lock_expires_at and _as_utc(item.lock_expires_at) <= now:
            item.state = "pending"
            item.claimed_by = None
            item.lock_token = None
            item.lock_expires_at = None

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

    # Clean expired lock
    if item.state == "claimed" and item.lock_expires_at and _as_utc(item.lock_expires_at) <= now:
        item.state = "pending"
        item.claimed_by = None
        item.lock_token = None
        item.lock_expires_at = None

    return item


def claim_review_item(
    db: Session,
    actor: Any,
    payload: ReviewItemClaim,
) -> OfficeReviewItem:
    now = _now()
    _require_permission(db, actor, "office.review.claim", now)

    item = db.scalar(
        select(OfficeReviewItem).where(
            OfficeReviewItem.organization_id == actor.organization_id,
            OfficeReviewItem.store_id == actor.store_id,
            OfficeReviewItem.id == payload.item_id,
        ).with_for_update()
    )
    if not item:
        _fail("item_not_found", f"review item {payload.item_id} not found", 404)

    if item.state in ("approved", "rejected", "modified", "escalated"):
        _fail("already_decided", f"item {item.id} is already in state {item.state}", 409)

    # Check existing active claim by someone else
    if (
        item.claimed_by
        and item.claimed_by != actor.subject_id
        and item.lock_expires_at
        and _as_utc(item.lock_expires_at) > now
    ):
        _fail(
            "item_already_claimed",
            f"item {item.id} is currently claimed by reviewer {item.claimed_by}",
            409,
        )

    lock_token = secrets.token_hex(16)
    expires_at = now + timedelta(seconds=payload.lock_duration_seconds)

    item.claimed_by = actor.subject_id
    item.claimed_at = now
    item.lock_token = lock_token
    item.lock_expires_at = expires_at
    item.state = "claimed"
    item.updated_at = now

    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action="office.review.item_claimed",
        target_type="office_review_item",
        target_id=item.id,
        idempotency_key=f"ori_clm:{item.id}:{lock_token}",
        details={"lock_duration": payload.lock_duration_seconds},
    )
    db.flush()
    return item


def release_review_item(
    db: Session,
    actor: Any,
    item_id: str,
    lock_token: str | None = None,
) -> OfficeReviewItem:
    now = _now()
    _require_permission(db, actor, "office.review.claim", now)

    item = db.scalar(
        select(OfficeReviewItem).where(
            OfficeReviewItem.organization_id == actor.organization_id,
            OfficeReviewItem.store_id == actor.store_id,
            OfficeReviewItem.id == item_id,
        ).with_for_update()
    )
    if not item:
        _fail("item_not_found", f"review item {item_id} not found", 404)

    if item.state == "claimed":
        if lock_token and item.lock_token and item.lock_token != lock_token:
            _fail("lock_token_mismatch", "invalid lock token for item release", 403)
        if item.claimed_by != actor.subject_id and item.lock_expires_at and _as_utc(item.lock_expires_at) > now:
            _fail("permission_denied", "cannot release lock held by another active reviewer", 403)

        item.state = "pending"
        item.claimed_by = None
        item.claimed_at = None
        item.lock_token = None
        item.lock_expires_at = None
        item.updated_at = now
        db.flush()

    return item


def decide_review_item(
    db: Session,
    actor: Any,
    payload: ReviewItemDecide,
) -> tuple[OfficeReviewItem, OfficeReviewDecision]:
    now = _now()
    _require_permission(db, actor, "office.review.decide", now)

    item = db.scalar(
        select(OfficeReviewItem).where(
            OfficeReviewItem.organization_id == actor.organization_id,
            OfficeReviewItem.store_id == actor.store_id,
            OfficeReviewItem.id == payload.item_id,
        ).with_for_update()
    )
    if not item:
        _fail("item_not_found", f"review item {payload.item_id} not found", 404)

    if item.state in ("approved", "rejected", "modified", "escalated"):
        _fail("already_decided", f"review item {item.id} has already been decided ({item.state})", 409)

    # 1. Optimistic locking on target version
    if item.target_version != payload.expected_target_version:
        _fail(
            "version_conflict",
            f"target version changed during review (current={item.target_version}, expected={payload.expected_target_version}); reload and re-verify",
            409,
        )

    # 2. Lock / Claim validation
    if item.state == "claimed":
        if item.claimed_by != actor.subject_id and item.lock_expires_at and _as_utc(item.lock_expires_at) > now:
            _fail(
                "item_locked_by_other",
                f"item {item.id} is locked by reviewer {item.claimed_by}",
                403,
            )
        if payload.lock_token and item.lock_token and payload.lock_token != item.lock_token:
            _fail("lock_token_mismatch", "provided lock token does not match active claim", 403)

    # 3. Build receipt digest
    digest_input = {
        "organization_id": actor.organization_id,
        "store_id": actor.store_id,
        "item_id": item.id,
        "target_kind": item.target_kind,
        "target_id": item.target_id,
        "target_version": item.target_version,
        "decision": payload.decision,
        "modifications": payload.modifications,
        "actor_id": actor.subject_id,
        "reason": payload.reason,
        "decided_at": now.isoformat(),
    }
    receipt_digest = _canonical_digest(digest_input)

    # 4. Update item state
    item.state = payload.decision
    item.reviewed_by = actor.subject_id
    item.reviewed_at = now
    item.review_decision = payload.decision
    item.review_notes = payload.reason
    item.modifications = payload.modifications
    item.receipt_digest = receipt_digest
    item.claimed_by = None
    item.lock_token = None
    item.lock_expires_at = None
    item.updated_at = now

    # 5. Insert immutable decision record
    decision = OfficeReviewDecision(
        id=str(uuid4()),
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        item_id=item.id,
        target_kind=item.target_kind,
        target_id=item.target_id,
        target_version=item.target_version,
        decision=payload.decision,
        modifications=payload.modifications,
        actor_id=actor.subject_id,
        reason=payload.reason,
        receipt_digest=receipt_digest,
        decided_at=now,
    )
    db.add(decision)

    record_event(
        db,
        actor_id=actor.subject_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        action=f"office.review.{payload.decision}",
        target_type="office_review_item",
        target_id=item.id,
        idempotency_key=f"ori_dec:{item.id}:{receipt_digest}",
        details={"decision": payload.decision, "reason": payload.reason},
    )
    db.flush()
    return item, decision


def batch_decide_review_items(
    db: Session,
    actor: Any,
    payload: BatchReviewDecide,
) -> BatchReviewOut:
    now = _now()
    _require_permission(db, actor, "office.review.batch", now)
    _require_permission(db, actor, "office.review.decide", now)

    results: list[BatchItemResult] = []
    succeeded = 0
    failed = 0

    for decide_req in payload.items:
        # Savepoint per item to ensure itemized risk isolation
        nested = db.begin_nested()
        try:
            item, _ = decide_review_item(db, actor, decide_req)
            nested.commit()
            succeeded += 1
            results.append(
                BatchItemResult(
                    item_id=decide_req.item_id,
                    success=True,
                    decision=decide_req.decision,
                    risk_level=item.risk_level,
                    error_code=None,
                    message="decision applied",
                )
            )
        except HTTPException as exc:
            nested.rollback()
            failed += 1
            err_code = exc.detail.split(":")[0] if ":" in exc.detail else "HTTP_ERROR"
            results.append(
                BatchItemResult(
                    item_id=decide_req.item_id,
                    success=False,
                    decision=decide_req.decision,
                    risk_level="unknown",
                    error_code=err_code,
                    message=exc.detail,
                )
            )
        except Exception as exc:
            nested.rollback()
            failed += 1
            results.append(
                BatchItemResult(
                    item_id=decide_req.item_id,
                    success=False,
                    decision=decide_req.decision,
                    risk_level="unknown",
                    error_code="INTERNAL_ERROR",
                    message=str(exc),
                )
            )

    return BatchReviewOut(
        total=len(payload.items),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


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
