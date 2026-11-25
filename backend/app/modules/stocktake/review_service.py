"""STK-004: blind scoped view, append-only receipt-backed manual review.

Reuses STK-003's stocktake_commands + transaction/audit/authorization and
stocktake_observations (append-only). NO balance/ledger/ERP write is possible.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.media.models import MediaAsset
from backend.app.modules.stocktake import service as existing
from backend.app.modules.stocktake.conversion import (
    ConversionError, active_factor, aware_utc, verified_box_factor,
)
from backend.app.modules.stocktake.models import (
    StocktakeCommand, StocktakeObservation, StocktakeSession,
)
from backend.app.modules.stocktake.schemas import ConversionVerify, ReviewDecision


def _rows(session: Session, actor: AuthenticatedSubject, sid: str) -> list[StocktakeObservation]:
    return session.scalars(select(StocktakeObservation).where(
        *existing._scope(StocktakeObservation, actor),
        StocktakeObservation.session_id == sid,
    ).order_by(StocktakeObservation.event_seq, StocktakeObservation.id)).all()


def _receipts(session: Session, actor: AuthenticatedSubject, sid: str, operation: str) -> list[dict]:
    # Only responses created by the existing scoped idempotent command gateway.
    records = session.scalars(select(StocktakeCommand).where(
        *existing._scope(StocktakeCommand, actor),
        StocktakeCommand.operation == operation,
    ).order_by(StocktakeCommand.created_at, StocktakeCommand.id)).all()
    return [dict(item.response) for item in records
            if isinstance(item.response, dict) and item.response.get("session_id") == sid]


def _session_version_cas(session: Session, actor: AuthenticatedSubject, sid: str,
                         expected: int) -> int:
    count = session.execute(update(StocktakeSession).where(
        *existing._scope(StocktakeSession, actor),
        StocktakeSession.id == sid, StocktakeSession.status == "open",
        StocktakeSession.version == expected,
    ).values(version=expected + 1).execution_options(
        synchronize_session=False)).rowcount
    if count != 1:
        raise HTTPException(409, "stocktake_session_version_conflict")
    # UPDATE(... synchronize_session=False) does not refresh a previously
    # loaded StocktakeSession; an immediate preview must see the new version.
    session.expire_all()
    return expected + 1


def blind_session(session: Session, actor: AuthenticatedSubject, sid: str) -> dict:
    existing._authorize(session, actor, "stocktake.read")
    row = existing._session(session, actor, sid)
    return {
        "session_id": row.id, "session_version": row.version,
        "coverage_kind": row.coverage_kind,
        "coverage_location_id": row.coverage_location_id,
        "coverage_note": row.coverage_note, "opened_at": row.created_at,
        "blind": True, "book_quantity": None, "book_difference": None,
        "inventory_accuracy": None, "full_store_coverage": False,
        "message": "盲盘仅记录现场观察，账面数字不可见；局部覆盖不能代表整店实际库存。",
    }


def _leaves(events: list[StocktakeObservation]) -> list[tuple[StocktakeObservation, list[str]]]:
    by_id = {e.id: e for e in events}
    children = {}
    for event in events:
        if event.supersedes_id is None:
            if event.kind != "observed":
                raise HTTPException(409, "stocktake_correction_chain_invalid")
            continue
        if event.kind != "correction" or event.supersedes_id not in by_id:
            raise HTTPException(409, "stocktake_correction_chain_invalid")
        parent = by_id[event.supersedes_id]
        if (parent.event_seq >= event.event_seq or
                not existing._same_correction_target(parent, event) or
                event.supersedes_id in children):
            raise HTTPException(409, "stocktake_correction_chain_ambiguous")
        children[event.supersedes_id] = event.id
    result = []
    for event in events:
        if event.id in children:
            continue
        chain = [event.id]
        pointer = event
        visited = set(chain)
        while pointer.supersedes_id is not None:
            parent_id = pointer.supersedes_id
            if parent_id in visited:
                raise HTTPException(409, "stocktake_correction_cycle")
            visited.add(parent_id)
            chain.append(parent_id)
            pointer = by_id[parent_id]
        result.append((event, list(reversed(chain))))
    return result


def review_preview(session: Session, actor: AuthenticatedSubject, sid: str) -> dict:
    existing._authorize(session, actor, "stocktake.read")
    stock = existing._session(session, actor, sid)
    now = datetime.now(timezone.utc)
    receipts = _receipts(session, actor, sid, "conversion.verify")
    active = _leaves(_rows(session, actor, sid))
    lines = []
    totals = defaultdict(Decimal)
    grouped = defaultdict(list)
    for event, chain in active:
        quantity = Decimal(event.quantity)
        if not quantity.is_finite() or quantity < 0:
            raise HTTPException(409, "stocktake_quantity_corrupt")
        reason = "original_piece_observation"
        normalized = None
        receipt_ref = None
        if event.product_id is not None and event.location_id is not None:
            if event.unit == "piece":
                normalized = quantity
            elif event.unit == "box":
                try:
                    factor, receipt_ref, reason = active_factor(
                        receipts, product_id=event.product_id,
                        observed_at=aware_utc(event.observed_at), now=now)
                except (ConversionError, ValueError, KeyError):
                    factor, receipt_ref, reason = None, None, "conversion_evidence_invalid"
                if factor is not None:
                    normalized = quantity * factor
            else:
                reason = "no_cross_unit_conversion"
        else:
            reason = "unknown_product_or_location"
        line = {
            "observation_id": event.id, "event_seq": event.event_seq,
            "source_observation_ids": chain,
            "product_id": event.product_id, "unknown_product_label": event.unknown_product_label,
            "location_id": event.location_id, "unknown_location_label": event.unknown_location_label,
            "quantity": format(quantity, "f"), "unit": event.unit,
            "observed_at": event.observed_at, "recorded_at": event.recorded_at,
            "normalized_piece_quantity": format(normalized, "f") if normalized is not None else None,
            "conversion_receipt_ref": receipt_ref, "conversion_status": reason,
            "observed_by": event.observed_by,
        }
        lines.append(line)
        if event.product_id is not None and event.location_id is not None:
            group_key = (event.product_id, event.location_id)
            grouped[group_key].append(line)
            if normalized is not None:
                totals[group_key] += normalized
    # Incomplete group cannot expose a seemingly authoritative total. Units
    # kg/meter and boxes without evidence remain raw lines only.
    summary = []
    for (product, location), own in sorted(grouped.items()):
        incomplete = [line["conversion_status"] for line in own
                      if line["normalized_piece_quantity"] is None]
        complete = not incomplete
        summary.append({
            "product_id": product, "location_id": location,
            "normalized_piece_quantity": format(totals[(product, location)], "f") if complete else None,
            "unit": "piece" if complete else None,
            "conversion_complete": complete,
            "observation_ids": [line["observation_id"] for line in own],
            "incomplete_reasons": incomplete,
        })
    decisions = _receipts(session, actor, sid, "review.decide")
    return {
        "session_id": sid, "session_version": stock.version,
        "coverage_kind": stock.coverage_kind, "coverage_location_id": stock.coverage_location_id,
        "coverage_note": stock.coverage_note, "full_store_coverage": False,
        "preview_as_of": now, "book_quantity": None, "book_difference": None,
        "inventory_accuracy": None, "difference_status": "authoritative_book_balance_unavailable",
        "observation_count": len(active), "observations": lines, "groups": summary,
        "conversion_receipts": receipts,
        "review_receipts": decisions,
        "reviewer_id": decisions[-1]["reviewer_id"] if decisions else None,
        "review_current": bool(decisions and decisions[-1]["session_version"] == stock.version),
        "disclaimer": "仅本次局部现场观察及已核对包装关系，绝不代表全店库存或账面调整。",
    }


def verify_conversion(session: Session, actor: AuthenticatedSubject, sid: str,
                      data: ConversionVerify, *, key: str) -> dict:
    existing._authorize(session, actor, "stocktake.write")
    payload = {"session_id": sid, **data.model_dump(mode="json")}
    def execute() -> dict:
        stock = existing._session(session, actor, sid)
        if stock.version != data.expected_session_version:
            raise HTTPException(409, "stocktake_session_version_conflict")
        try:
            factor = verified_box_factor(data.factor, source_unit=data.source_unit,
                                         target_unit=data.target_unit)
            start, expiry = aware_utc(data.effective_from), aware_utc(data.expires_at)
        except ConversionError as exc:
            raise HTTPException(422, exc.code) from exc
        now = datetime.now(timezone.utc)
        if start > now or expiry <= now or expiry <= start or expiry > now + timedelta(days=365):
            raise HTTPException(422, "stocktake_conversion_interval_invalid")
        product = session.scalar(select(CatalogProduct).where(
            *existing._scope(CatalogProduct, actor),
            CatalogProduct.id == data.product_id,
            CatalogProduct.active.is_(True)))
        if product is None:
            raise HTTPException(404, "stocktake_product_not_found")
        image = session.scalar(select(MediaAsset).where(
            *existing._scope(MediaAsset, actor),
            MediaAsset.id == data.evidence_media_id,
            MediaAsset.category == "inventory_photo"))
        if image is None:
            raise HTTPException(404, "stocktake_media_not_found")
        if stock.opened_by == actor.subject_id:
            raise HTTPException(403, "stocktake_independent_reviewer_required")
        previous = _receipts(session, actor, sid, "conversion.verify")
        for receipt in previous:
            if receipt["product_id"] == data.product_id:
                old_start = aware_utc(datetime.fromisoformat(receipt["effective_from"]))
                old_end = aware_utc(datetime.fromisoformat(receipt["expires_at"]))
                if start < old_end and old_start < expiry:
                    raise HTTPException(409, "stocktake_conversion_overlap")
        # Independence is required for the active observations this new
        # receipt could normalize, not merely for the person who opened the session.
        if any(event.observed_by == actor.subject_id
               and event.product_id == data.product_id
               and event.location_id is not None
               and event.unit == data.source_unit
               and start <= aware_utc(event.observed_at) < expiry
               for event, _ in _leaves(_rows(session, actor, sid))):
            raise HTTPException(403, "stocktake_independent_reviewer_required")
        new_version = _session_version_cas(session, actor, sid,
                                           data.expected_session_version)
        receipt_id = str(uuid4())
        result = {
            "receipt_ref": receipt_id, "session_id": sid,
            "session_version": new_version, "status": "human_verified_with_media",
            "product_id": data.product_id, "source_unit": data.source_unit,
            "target_unit": data.target_unit, "factor": str(factor),
            "evidence_media_id": image.id,
            "effective_from": start.isoformat(), "expires_at": expiry.isoformat(),
            "verified_at": now.isoformat(), "verified_by": actor.subject_id,
            "scope": "partial_packaging_only", "ledger_write": False,
        }
        existing._audit(session, actor, "stocktake.conversion.verified",
                        "stocktake_conversion_receipt", receipt_id,
                        {"session_id": sid, "product_id": product.id,
                         "session_version": new_version,
                         "evidence_media_id": image.id})
        return result
    return existing._cmd(session, actor, "conversion.verify", key, payload, execute)


def review_decision(session: Session, actor: AuthenticatedSubject, sid: str,
                    data: ReviewDecision, *, key: str) -> dict:
    existing._authorize(session, actor, "stocktake.write")
    payload = {"session_id": sid, **data.model_dump(mode="json")}
    def execute() -> dict:
        stock = existing._session(session, actor, sid)
        if stock.version != data.expected_session_version:
            raise HTTPException(409, "stocktake_session_version_conflict")
        observed = _rows(session, actor, sid)
        if not observed:
            raise HTTPException(409, "stocktake_review_requires_observations")
        if any(e.observed_by == actor.subject_id for e in observed):
            raise HTTPException(403, "stocktake_independent_reviewer_required")
        if not data.reason.strip():
            raise HTTPException(422, "stocktake_review_reason_required")
        # Evidence references are actual observations in this store/session,
        # never client-submitted inventory book balances or fabricated totals.
        ids = {e.id for e in observed}
        if not data.observation_ids or not set(data.observation_ids) <= ids:
            raise HTTPException(409, "stocktake_review_sources_invalid")
        view = review_preview(session, actor, sid)
        if data.decision == "accepted_partial":
            leaves = {line["observation_id"] for line in view["observations"]}
            if set(data.observation_ids) != leaves:
                raise HTTPException(409, "stocktake_review_incomplete_sources")
            if any(line["normalized_piece_quantity"] is None
                   for line in view["observations"]):
                raise HTTPException(409, "stocktake_unverified_conversion_requires_recount")
        new_version = _session_version_cas(session, actor, sid,
                                           data.expected_session_version)
        receipt_id = str(uuid4())
        result = {
            "receipt_ref": receipt_id, "session_id": sid,
            "session_version": new_version,
            "decision": data.decision, "reason": data.reason,
            "observation_ids": sorted(set(data.observation_ids)),
            "reviewer_id": actor.subject_id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "coverage_kind": stock.coverage_kind,
            "coverage_location_id": stock.coverage_location_id,
            "full_store_coverage": False,
            "book_difference": None, "stock_adjustment": None,
            "ledger_write": False,
        }
        existing._audit(session, actor, "stocktake.review.decided",
                        "stocktake_review_receipt", receipt_id,
                        {"session_id": sid, "decision": data.decision,
                         "session_version": new_version})
        return result
    return existing._cmd(session, actor, "review.decide", key, payload, execute)
