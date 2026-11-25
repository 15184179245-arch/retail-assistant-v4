"""ANA-002: rebuildable scoped partial stocktake evidence snapshots.

No ERP/book inventory, price, reorder, LLM, or external writes. GET never
materializes a snapshot. The host must explicitly invoke build_snapshot in
its own audited DB transaction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.models import UTCDateTime
from backend.app.modules.analytics.models import AnalyticsSnapshot
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.stocktake.models import StocktakeObservation, StocktakeSession
from backend.app.modules.stocktake import review_service

METRIC_ID = "stocktake.partial_evidence_confidence"
METRIC_VERSION = "1.0.0"
FORMULA_VERSION = "ana-002-stocktake-v1"
_FORMULA = {
    "metric_id": METRIC_ID, "metric_version": METRIC_VERSION,
    "observation": "latest correction leaf per within-session chain",
    "coverage": "partial_area|partial_shelf|unknown (never whole_store)",
    "confidence": "partial evidence quality, never ledger accuracy",
    "missing": "unknown product/location or unverified unit conversion",
    "zero_denominator": "null:no_population",
}


def _canon(value):
    return json.dumps(jsonable_encoder(value), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return sha256(_canon(value).encode("utf-8")).hexdigest()


def _scope(session: Session, actor: AuthenticatedSubject):
    session.flush()
    session.expire_all()
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="analytics.read")
    if session.scalar(select(Subject.subject_type).where(
        Subject.organization_id == actor.organization_id,
        Subject.id == actor.subject_id)) != "person":
        raise HTTPException(403, "analytics_employee_required")


def _timezone(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _project_preview(preview: dict) -> tuple[dict, list]:
    """Pure calculation on existing STK-004 validated preview, no fake ledger."""
    leaves = preview["observations"]
    n = len(leaves)
    unknown = sum(1 for row in leaves if row["product_id"] is None
                  or row["location_id"] is None)
    unconverted = sum(1 for row in leaves
                      if row["normalized_piece_quantity"] is None)
    missing = sum(1 for row in leaves if row["product_id"] is None
                  or row["location_id"] is None
                  or row["normalized_piece_quantity"] is None)
    if n == 0:
        confidence = "insufficient_evidence"
    elif preview["coverage_kind"] == "unknown":
        confidence = "unknown_coverage"
    elif preview["review_current"] and missing == 0:
        confidence = "partial_reviewed"
    else:
        confidence = "partial_unreviewed"
    warnings = []
    if preview["coverage_kind"] == "unknown":
        warnings.append("coverage_unknown")
    else:
        warnings.append("partial_coverage_not_whole_store")
    if n == 0:
        warnings.append("no_population")
    if unknown:
        warnings.append("unknown_product_or_location")
    if unconverted:
        warnings.append("conversion_not_verified")
    if not preview["review_current"]:
        warnings.append("manual_review_not_current")
    warnings = sorted(set(warnings))
    ratio = (str(Decimal(missing) / Decimal(n)) if n else None)
    metric = {
        "metric_id": METRIC_ID, "metric_version": METRIC_VERSION,
        "formula_version": FORMULA_VERSION, "formula_digest": _digest(_FORMULA),
        "state": "candidate_only", "source_verified": False, "publishable": False,
        "scope_kind": preview["coverage_kind"],
        "coverage_location_id": preview["coverage_location_id"],
        "session_version": preview["session_version"],
        "confidence": confidence, "warnings": warnings,
        "observation_leaf_count": n,
        "unknown_identity_count": unknown,
        "unverified_conversion_count": unconverted,
        "missing_evidence_count": missing,
        "missing_evidence_rate": ratio,
        "missing_evidence_rate_reason": "no_population" if n == 0 else None,
        "manual_review_current": bool(preview["review_current"]),
        "observed_piece_groups": [
            {
                "product_id": v["product_id"],
                "location_id": v["location_id"],
                "normalized_piece_quantity": v["normalized_piece_quantity"],
                "conversion_complete": v["conversion_complete"],
                "incomplete_reasons": v["incomplete_reasons"],
                "observation_ids": v["observation_ids"],
            } for v in preview["groups"]
        ],
        "full_store_coverage": False,
        "book_quantity": None,
        "difference_to_book": None,
        "inventory_accuracy": None,
        "sales_conversion": None,
        "replenishment_quantity": None,
        "price_change": None,
        "stock_adjustment": None,
        "action": "manual_review_only",
    }
    refs = sorted([
        {"id": row["observation_id"], "event_seq": row["event_seq"],
         "source_observation_ids": row["source_observation_ids"],
         "recorded_at": row["recorded_at"],
         "conversion_receipt_ref": row["conversion_receipt_ref"],
         "conversion_status": row["conversion_status"]}
        for row in leaves
    ], key=lambda x: (x["event_seq"], x["id"]))
    return metric, refs


def build_snapshot(session: Session, actor: AuthenticatedSubject, session_id: str,
                   *, now: datetime | None = None) -> AnalyticsSnapshot:
    """Explicit INTERNAL operation only; caller commits snapshot and audit atomically.

    Existing STK-004 review_preview independently re-checks stocktake.read,
    correction-chain consistency, active verified conversion and review receipts.
    """
    _scope(session, actor)
    preview = review_service.review_preview(session, actor, session_id)
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise HTTPException(422, "analytics_snapshot_time_invalid")
    current = _timezone(now) if now is not None else datetime.now(timezone.utc)
    if current > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise HTTPException(422, "analytics_snapshot_time_invalid")
    metric, refs = _project_preview(preview)
    source_proof = {
        "organization_id": actor.organization_id, "store_id": actor.store_id,
        "session_id": session_id, "session_version": preview["session_version"],
        "coverage_kind": preview["coverage_kind"],
        "observations": preview["observations"],
        "conversion_receipts": preview["conversion_receipts"],
        "review_receipts": preview["review_receipts"],
        "source_refs": refs,
        # Keep replay inputs in the immutable snapshot, but never expose
        # detailed staff/media/source row content through the GET API.
        "replay_preview": {k: v for k, v in preview.items() if k != "preview_as_of"},
    }
    # Immutable internal frozen evidence makes source hashes independently
    # re-verifiable even if live correction/verification rows arrive later.
    source_proof = jsonable_encoder(source_proof)
    refs = jsonable_encoder(refs)
    last_recorded = max(
        (_timezone(x["recorded_at"]) for x in preview["observations"]),
        default=None)
    snapshot = AnalyticsSnapshot(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, session_id=session_id, created_by=actor.subject_id,
        version=1, source_session_version=preview["session_version"],
        metric_id=METRIC_ID, metric_version=METRIC_VERSION,
        formula_version=FORMULA_VERSION, formula_digest=_digest(_FORMULA),
        source_digest=_digest(source_proof), result_digest=_digest(metric),
        coverage_kind=preview["coverage_kind"], confidence=metric["confidence"],
        data={"metric": metric, "source_proof": source_proof},
        sources=refs, source_last_recorded_at=last_recorded,
        snapshot_at=current, created_at=current,
    )
    session.add(snapshot)
    session.flush()
    record_event(session, actor_id=actor.subject_id,
                 organization_id=actor.organization_id, store_id=actor.store_id,
                 action="analytics.snapshot.captured", target_type="analytics_snapshot",
                 target_id=snapshot.id, idempotency_key=f"analytics:snapshot:{snapshot.id}",
                 details={"formula_version": FORMULA_VERSION,
                          "source_digest": snapshot.source_digest,
                          "result_digest": snapshot.result_digest,
                          "source_session_id": session_id})
    return snapshot


def _view(session: Session, actor: AuthenticatedSubject, row: AnalyticsSnapshot,
          *, now: datetime | None = None) -> dict:
    current = _timezone(now) if now is not None else datetime.now(timezone.utc)
    if (not isinstance(row.data, dict)
            or set(row.data) != {"metric", "source_proof"}
            or not isinstance(row.data.get("metric"), dict)
            or not isinstance(row.data.get("source_proof"), dict)
            or row.formula_digest != _digest(_FORMULA)
            or row.result_digest != _digest(row.data["metric"])
            or row.source_digest != _digest(row.data["source_proof"])
            or row.sources != row.data["source_proof"].get("source_refs")):
        raise HTTPException(409, "analytics_snapshot_digest_mismatch")
    try:
        rebuilt_metric, rebuilt_refs = _project_preview(
            row.data["source_proof"]["replay_preview"])
        if (_digest(rebuilt_metric) != row.result_digest
                or jsonable_encoder(rebuilt_refs) != row.sources):
            raise ValueError("snapshot replay mismatch")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "analytics_snapshot_rebuild_failed") from exc
    stock = session.scalar(select(StocktakeSession).where(
        StocktakeSession.id == row.session_id,
        StocktakeSession.organization_id == actor.organization_id,
        StocktakeSession.store_id == actor.store_id))
    if stock is None:
        raise HTTPException(409, "analytics_snapshot_source_missing")
    last_seq = session.scalar(select(func.max(StocktakeObservation.event_seq)).where(
        StocktakeObservation.organization_id == actor.organization_id,
        StocktakeObservation.store_id == actor.store_id,
        StocktakeObservation.session_id == row.session_id)) or 0
    frozen_seq = max((ref["event_seq"] for ref in row.sources), default=0)
    stale_reasons = []
    if stock.version != row.source_session_version or last_seq > frozen_seq:
        stale_reasons.append("source_changed")
    if current - _timezone(row.snapshot_at) > timedelta(hours=24):
        stale_reasons.append("snapshot_expired")
    used_receipts = {ref.get("conversion_receipt_ref")
                     for ref in row.sources if ref.get("conversion_receipt_ref")}
    for receipt in row.data["source_proof"].get("conversion_receipts", []):
        if receipt.get("receipt_ref") not in used_receipts:
            continue
        try:
            expires_at = datetime.fromisoformat(receipt["expires_at"].replace("Z", "+00:00"))
            if _timezone(expires_at) <= current:
                stale_reasons.append("conversion_evidence_expired")
        except (KeyError, TypeError, ValueError, AttributeError):
            stale_reasons.append("conversion_evidence_invalid")
    return {
        "id": row.id, "version": row.version, "metric_id": row.metric_id,
        "metric_version": row.metric_version, "formula_version": row.formula_version,
        "formula_digest": row.formula_digest, "source_digest": row.source_digest,
        "result_digest": row.result_digest,
        "organization_id": row.organization_id, "store_id": row.store_id,
        "session_id": row.session_id, "snapshot_at": row.snapshot_at.isoformat(),
        "source_last_recorded_at": row.source_last_recorded_at.isoformat()
            if row.source_last_recorded_at else None,
        "source_session_version": row.source_session_version,
        "fresh": not stale_reasons, "stale_reasons": stale_reasons,
        "coverage_kind": row.coverage_kind, "confidence": row.confidence,
        "metric": row.data["metric"], "sources": [
            {**ref, "source_url":
             f"/api/v1/stocktake/sessions/{row.session_id}/observations"}
            for ref in row.sources],
        "warnings": sorted(set(row.data["metric"]["warnings"] + stale_reasons)),
        "publishable": False, "source_verified": False,
        "book_quantity": None, "inventory_accuracy": None,
        "full_store_coverage": False, "action": "manual_review_only",
    }


def list_snapshots(session: Session, actor: AuthenticatedSubject, *, limit: int = 20) -> list[dict]:
    _scope(session, actor)
    rows = session.scalars(select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.organization_id == actor.organization_id,
        AnalyticsSnapshot.store_id == actor.store_id
    ).order_by(AnalyticsSnapshot.snapshot_at.desc(), AnalyticsSnapshot.id.desc())
     .limit(limit)).all()
    return [_view(session, actor, row) for row in rows]


def read_snapshot(session: Session, actor: AuthenticatedSubject, snapshot_id: str) -> dict:
    _scope(session, actor)
    row = session.scalar(select(AnalyticsSnapshot).where(
        AnalyticsSnapshot.id == snapshot_id,
        AnalyticsSnapshot.organization_id == actor.organization_id,
        AnalyticsSnapshot.store_id == actor.store_id))
    if row is None:
        raise HTTPException(404, "analytics_snapshot_not_found")
    return _view(session, actor, row)
