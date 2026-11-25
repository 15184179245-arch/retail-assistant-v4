"""AGT-014: trusted-host-only, scoped stocktake/location evidence.

No model-visible tool registration, inventory mutation, placement write, or
auto-approval. Only the existing human-reviewed Task tool may later persist
the returned *proposal*, after a fresh host-side human confirmation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.locations.models import Location, PlacementAssertion
from backend.app.modules.media.models import MediaAsset
from backend.app.modules.stocktake.models import StocktakeObservation
from backend.app.modules.stocktake import review_service


class EvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=36)


class EvidenceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str
    store_id: str
    session_id: str
    session_version: int
    coverage_kind: str
    coverage_location_id: str | None
    coverage_note: str
    full_store_coverage: bool = False
    book_quantity: None = None
    book_difference: None = None
    inventory_adjustment: None = None
    review_current: bool
    review_receipt_refs: list[str]
    lines: list[dict]
    groups: list[dict]
    source_digest: str
    confidence: str
    confidence_reasons: list[str]
    message: str


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False, default=_safe_datetime)


def _safe_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stocktake timestamp lacks timezone")
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError("unexpected source value")


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _independent_review_limit(preview: dict, session_id: str, observed_ids: list[str]) -> str | None:
    # A successful review advances the session version. That is the authority
    # for currentness, independent of receipt display order or the wall clock.
    unverified = "independent_review_acceptance_unverified"
    version = preview["session_version"]
    if type(version) is not int or version < 1:
        return unverified
    current = None
    for receipt in preview["review_receipts"]:
        if not isinstance(receipt, dict) or receipt.get("session_id") != session_id:
            return unverified
        receipt_version = receipt.get("session_version")
        if type(receipt_version) is not int or not 1 <= receipt_version <= version:
            return unverified
        if receipt_version == version:
            if current is not None:
                return unverified
            current = receipt
    if current is None:
        return "independent_review_not_current"
    if (not observed_ids or current.get("decision") != "accepted_partial"
            or not isinstance(current.get("receipt_ref"), str) or not current["receipt_ref"]):
        return unverified
    accepted = current.get("observation_ids")
    if type(accepted) is not list or any(type(item) is not str or not item for item in accepted):
        return unverified
    leaves = set(observed_ids)
    if len(accepted) != len(observed_ids) or len(leaves) != len(observed_ids) or set(accepted) != leaves:
        return unverified
    return None


def read_evidence(session: Session, requester: AuthenticatedSubject,
                  payload: EvidenceInput) -> EvidenceOutput:
    """Read STK-004 preview; all source filters are taken from requester scope."""
    # review_preview independently re-authorizes stocktake.read and verifies
    # session scope. The gateway ALSO checks locations.read for requester.
    preview = review_service.review_preview(session, requester, payload.session_id)
    scope = (requester.organization_id, requester.store_id)
    rows = list(session.scalars(select(StocktakeObservation).where(
        StocktakeObservation.organization_id == scope[0],
        StocktakeObservation.store_id == scope[1],
        StocktakeObservation.session_id == payload.session_id,
    )))
    by_id = {row.id: row for row in rows}
    observed_ids = [line["observation_id"] for line in preview["observations"]]
    location_ids = {line["location_id"] for line in preview["observations"]
                    if line["location_id"] is not None}
    product_ids = {line["product_id"] for line in preview["observations"]
                   if line["product_id"] is not None}
    placements = list(session.scalars(select(PlacementAssertion).where(
        PlacementAssertion.organization_id == scope[0],
        PlacementAssertion.store_id == scope[1],
        PlacementAssertion.product_id.in_(product_ids),
        PlacementAssertion.location_id.in_(location_ids),
    ).order_by(PlacementAssertion.event_seq)))
    latest = {(row.product_id, row.location_id): row for row in placements}
    locs = list(session.scalars(select(Location).where(
        Location.organization_id == scope[0], Location.store_id == scope[1],
        Location.id.in_(location_ids),
    )))
    location_map = {loc.id: loc for loc in locs}
    media_ids = {r.media_id for r in rows if r.media_id}
    media = list(session.scalars(select(MediaAsset).where(
        MediaAsset.organization_id == scope[0],
        MediaAsset.store_id == scope[1],
        MediaAsset.category == "inventory_photo",
        MediaAsset.id.in_(media_ids),
    )))
    media_map = {item.id: item for item in media}
    # The human verified box-conversion receipt points to its supporting
    # photo. Do not treat photo/OCR as an independent verified SKU or location.
    conversion_by_ref = {
        item["receipt_ref"]: item for item in preview["conversion_receipts"]
        if isinstance(item, dict) and item.get("receipt_ref")
    }
    conversion_media_ids = {
        r["evidence_media_id"] for r in conversion_by_ref.values()
        if isinstance(r.get("evidence_media_id"), str)
    }
    if conversion_media_ids:
        linked = session.scalars(select(MediaAsset).where(
            MediaAsset.organization_id == scope[0],
            MediaAsset.store_id == scope[1],
            MediaAsset.category == "inventory_photo",
            MediaAsset.id.in_(conversion_media_ids),
        )).all()
        media_map.update({m.id: m for m in linked})

    all_lines = []
    reasons = []
    for line in preview["observations"]:
        obs = by_id.get(line["observation_id"])
        if obs is None or obs.event_seq != line["event_seq"]:
            raise ValueError("stocktake evidence version mismatch")
        placement = latest.get((obs.product_id, obs.location_id))
        location = location_map.get(obs.location_id)
        image = media_map.get(obs.media_id)
        conversion = conversion_by_ref.get(line["conversion_receipt_ref"])
        factor_image = (media_map.get(conversion.get("evidence_media_id"))
                        if conversion is not None else None)
        item_reasons = []
        if obs.product_id is None:
            item_reasons.append("unknown_product_identity")
        if obs.location_id is None or location is None or not location.active:
            item_reasons.append("unknown_or_inactive_location")
        if line["normalized_piece_quantity"] is None:
            item_reasons.append(line["conversion_status"])
        if placement is None or placement.kind != "present":
            item_reasons.append("placement_not_human_confirmed")
        if obs.media_id is not None and image is None:
            item_reasons.append("photo_reference_unverified")
        if line["conversion_receipt_ref"] is not None and factor_image is None:
            item_reasons.append("conversion_photo_reference_unverified")
        # Photos and placement assertions are corroborating evidence only.
        # Neither proves today's authoritative inventory/book balance.
        source = {
            "observation_id": obs.id, "event_seq": obs.event_seq,
            "observation_chain": list(line["source_observation_ids"]),
            "observed_by": obs.observed_by,
            "observed_at": _safe_datetime(obs.observed_at),
            "recorded_at": _safe_datetime(obs.recorded_at),
            "product_id": obs.product_id,
            "unknown_product_label": obs.unknown_product_label,
            "location_id": obs.location_id,
            "unknown_location_label": obs.unknown_location_label,
            "location_kind": location.kind if location is not None else None,
            "quantity": line["quantity"], "unit": line["unit"],
            "normalized_piece_quantity": line["normalized_piece_quantity"],
            "conversion_status": line["conversion_status"],
            "conversion_receipt_ref": line["conversion_receipt_ref"],
            "conversion_evidence_media_id": factor_image.id if factor_image else None,
            "conversion_evidence_media_sha256": factor_image.sha256 if factor_image else None,
            "media_id": image.id if image else None,
            "media_sha256": image.sha256 if image else None,
            "placement_event_seq": placement.event_seq if placement else None,
            "placement_kind": placement.kind if placement else None,
            "placement_asserted_by": placement.asserted_by if placement else None,
            "reasons": sorted(set(item_reasons)),
        }
        source["source_digest"] = _digest(source)
        reasons.extend(item_reasons)
        all_lines.append(source)
    groups = [{
        "product_id": g["product_id"],
        "location_id": g["location_id"],
        "normalized_piece_quantity": g["normalized_piece_quantity"]
            if g["conversion_complete"] else None,
        "conversion_complete": g["conversion_complete"],
        "observation_ids": g["observation_ids"],
        "incomplete_reasons": g["incomplete_reasons"],
        "full_store_coverage": False,
        "book_difference": None,
    } for g in preview["groups"]]
    if not all_lines:
        reasons.append("no_observations")
    review_limit = _independent_review_limit(preview, payload.session_id, observed_ids)
    if review_limit is not None:
        reasons.append(review_limit)
    if preview["coverage_kind"] == "unknown":
        reasons.append("coverage_unknown")
    else:
        reasons.append("partial_coverage_only")
    all_lines.sort(key=lambda x: (x["event_seq"], x["observation_id"]))
    groups.sort(key=lambda x: (x["product_id"], x["location_id"]))
    source = {
        "scope": scope,
        "session_id": payload.session_id,
        "session_version": preview["session_version"],
        "coverage_kind": preview["coverage_kind"],
        "coverage_location_id": preview["coverage_location_id"],
        "lines": all_lines, "groups": groups,
        "conversion_receipts": preview["conversion_receipts"],
        "review_receipts": preview["review_receipts"],
    }
    # Confidence is a bounded evidence classification, not a probability of
    # SKU, location, quantity or the whole-store ledger being correct.
    if not all_lines or any(x["reasons"] for x in all_lines):
        confidence = "insufficient"
    elif review_limit is not None:
        confidence = "needs_human_review"
    else:
        confidence = "supported_by_source_receipt"
    return EvidenceOutput(
        organization_id=requester.organization_id, store_id=requester.store_id,
        session_id=payload.session_id, session_version=preview["session_version"],
        coverage_kind=preview["coverage_kind"],
        coverage_location_id=preview["coverage_location_id"],
        coverage_note=preview["coverage_note"], full_store_coverage=False,
        review_current=preview["review_current"],
        review_receipt_refs=sorted(
            str(r["receipt_ref"]) for r in preview["review_receipts"]
            if "receipt_ref" in r),
        lines=all_lines, groups=groups, source_digest=_digest(source),
        confidence=confidence, confidence_reasons=sorted(set(reasons)),
        message="仅同店局部盘点/库位证据候选；需要员工复核，非全店准确库存、账面调账或正式库位更新。",
    )


def prepare_review_task(evidence: EvidenceOutput, *, reviewer_id: str,
                        requester: AuthenticatedSubject) -> dict:
    """Generate a stable proposal, NEVER invoke tasks.suggest_review itself."""
    if not isinstance(evidence, EvidenceOutput):
        raise ValueError("stocktake evidence type invalid")
    if not isinstance(reviewer_id, str) or not 1 <= len(reviewer_id) <= 36:
        raise ValueError("stocktake reviewer invalid")
    if (requester.organization_id, requester.store_id) != (
            evidence.organization_id, evidence.store_id):
        raise ValueError("stocktake evidence scope changed")
    ref = _digest([
        requester.organization_id, requester.store_id, evidence.session_id,
        evidence.session_version, evidence.source_digest, reviewer_id,
    ])
    summary = (
        f"盘点会话 {evidence.session_id} 版本 {evidence.session_version}；"
        f"覆盖 {evidence.coverage_kind}，并非整店。"
        f"来源 SHA256 {evidence.source_digest}；"
        f"置信 {evidence.confidence}，证据 {len(evidence.lines)} 条。"
        "请人工核对原始盘点、照片、包装换算与位置断言。"
        "禁止依据本候选调库存、直接确定正式货位或回写管家婆。"
    )
    return {
        "tool_key": "tasks.suggest_review", "version": "1",
        "requires_requester_confirmation": True,
        "expected_stocktake_session_version": evidence.session_version,
        "source_digest": evidence.source_digest,
        "idempotency_key": "stocktake-review:" + ref,
        "arguments": {
            "title": "盘点差异与库位证据人工复核",
            "description": summary, "reason": "局部证据尚需独立确认，不得自动调账",
            "reviewer_id": reviewer_id,
            "proposal_ref": "stocktake:" + ref,
        },
        "business_write": False, "inventory_adjustment": None,
        "full_store_coverage": False,
    }
