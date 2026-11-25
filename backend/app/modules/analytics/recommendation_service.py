"""ANA-005: deterministic, scoped purchase-analysis candidates and human receipts.

INTERNAL ONLY. Caller owns transaction. None of these functions create a
PurchaseIntent, send an ERP request, infer an authoritative inventory balance,
or make a true external source verification claim.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from hashlib import sha256
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.analytics.recommendation_models import (
    PurchaseRecommendation, PurchaseRecommendationDecision)
from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.identity.models import Store, Subject
from backend.app.modules.identity.service import authorize_scope

FORMULA_VERSION = "ana-005-synthetic-replenishment-v1"
REVIEW_DAYS = 7
SAFETY_DAYS = 2
_MAX_PIECES = 1_000_000
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?\Z")
_FIELDS = {
    "sales": {"daily_units", "sample_days", "source"},
    "stock": {"available_units", "coverage", "source"},
    "inbound": {"open_units", "source"},
    "supplier": {"lead_time_days", "minimum_order_units", "source"},
    "packaging": {"pieces_per_pack", "conversion_verified", "source"},
}
_MAX_SOURCE_AGE = {
    "sales": timedelta(days=7), "stock": timedelta(days=1),
    "inbound": timedelta(days=1), "supplier": timedelta(days=30),
    "packaging": timedelta(days=30),
}


def _fail(code: str, status: int = 409):
    raise HTTPException(status_code=status, detail=code)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("recommendation_timestamp_invalid", 422)
    return value.astimezone(timezone.utc)


def _canonical(value) -> str:
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ident(value: object, field: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail("recommendation_" + field + "_invalid", 422)
    return value


def _number(value: object, field: str, *, integer: bool = True,
            maximum: int = _MAX_PIECES) -> Decimal:
    if (type(value) is not str or len(value) > 30
            or _DECIMAL.fullmatch(value) is None
            or (integer and "." in value)):
        _fail("recommendation_" + field + "_invalid", 422)
    n = Decimal(value)
    if n > maximum:
        _fail("recommendation_" + field + "_invalid", 422)
    return n


def _source(row: dict, name: str, now: datetime,
            organization_id: str, store_id: str) -> list[str]:
    if type(row) is not dict or set(row) != {
            "organization_id", "store_id", "ref", "version", "sha256", "as_of",
            "verified"}:
        _fail("recommendation_source_contract_invalid", 422)
    if (row["organization_id"], row["store_id"]) != (organization_id, store_id):
        _fail("recommendation_source_scope_denied", 403)
    _ident(row["ref"], "source_ref")
    if type(row["version"]) is not int or row["version"] < 1:
        _fail("recommendation_source_version_invalid", 422)
    if type(row["sha256"]) is not str or _HASH.fullmatch(row["sha256"]) is None:
        _fail("recommendation_source_digest_invalid", 422)
    if type(row["verified"]) is not bool:
        _fail("recommendation_source_verified_invalid", 422)
    if type(row["as_of"]) is not str or len(row["as_of"]) > 40:
        _fail("recommendation_source_time_invalid", 422)
    try:
        observed = _utc(datetime.fromisoformat(row["as_of"].replace("Z", "+00:00")))
    except ValueError:
        _fail("recommendation_source_time_invalid", 422)
    issues = []
    if not row["verified"]:
        issues.append(name + "_not_attested")
    if observed > now + timedelta(minutes=5) or now - observed > _MAX_SOURCE_AGE[name]:
        issues.append(name + "_stale")
    return issues


def _project(evidence: dict, now: datetime,
             organization_id: str, store_id: str) -> dict:
    """Pure projection; even fully asserted inputs remain UNVERIFIED externally."""
    if type(evidence) is not dict or not set(evidence) <= set(_FIELDS):
        _fail("recommendation_evidence_contract_invalid", 422)
    reasons: list[str] = []
    for name in _FIELDS:
        if name not in evidence or evidence[name] is None:
            reasons.append(name + "_missing")
            continue
        row = evidence[name]
        if type(row) is not dict or set(row) != _FIELDS[name]:
            _fail("recommendation_" + name + "_contract_invalid", 422)
        reasons.extend(_source(row["source"], name, now, organization_id, store_id))
    if evidence.get("sales") is not None:
        s = evidence["sales"]
        _number(s["daily_units"], "daily_units", integer=False)
        sample = _number(s["sample_days"], "sample_days")
        if not 7 <= sample <= 365:
            reasons.append("sales_window_incomplete")
    if evidence.get("stock") is not None:
        st = evidence["stock"]
        _number(st["available_units"], "available_units")
        if st["coverage"] not in ("full", "partial", "unknown"):
            _fail("recommendation_stock_coverage_invalid", 422)
        if st["coverage"] != "full":
            reasons.append("stock_full_coverage_unproven")
    if evidence.get("inbound") is not None:
        _number(evidence["inbound"]["open_units"], "open_units")
    if evidence.get("supplier") is not None:
        su = evidence["supplier"]
        days = _number(su["lead_time_days"], "lead_time_days")
        minimum = _number(su["minimum_order_units"], "minimum_order_units")
        if days < 1 or days > 90 or minimum < 1:
            reasons.append("supplier_terms_incomplete")
    if evidence.get("packaging") is not None:
        pk = evidence["packaging"]
        size = _number(pk["pieces_per_pack"], "pieces_per_pack")
        if size < 1:
            _fail("recommendation_pack_size_invalid", 422)
        if type(pk["conversion_verified"]) is not bool:
            _fail("recommendation_conversion_invalid", 422)
        if not pk["conversion_verified"]:
            reasons.append("packaging_conversion_unverified")

    # No fake zero-fill, no partly scoped stock as an authoritative store balance.
    if reasons:
        return {
            "action": "manual_review", "quantity_pieces": None,
            "expected_benefit_pieces": None, "formula_version": FORMULA_VERSION,
            "reason_codes": sorted(set(reasons)),
            "review_period_days": REVIEW_DAYS, "safety_days": SAFETY_DAYS,
            "source_verified_external": False, "automatic_purchase": False,
            "note": "来源的verified为调用方自述；没有外部认证，不能正式下单。",
        }

    velocity = _number(evidence["sales"]["daily_units"], "daily_units", integer=False)
    lead = int(evidence["supplier"]["lead_time_days"])
    safety = int((velocity * SAFETY_DAYS).to_integral_value(rounding=ROUND_CEILING))
    expected = int((velocity * (lead + REVIEW_DAYS)).to_integral_value(
        rounding=ROUND_CEILING)) + safety
    available = int(evidence["stock"]["available_units"])
    inbound = int(evidence["inbound"]["open_units"])
    shortfall = max(0, expected - available - inbound)
    pack = int(evidence["packaging"]["pieces_per_pack"])
    minimum = int(evidence["supplier"]["minimum_order_units"])
    needed = max(minimum, shortfall) if shortfall else 0
    rounded = ((needed + pack - 1) // pack) * pack
    if rounded > _MAX_PIECES or expected > _MAX_PIECES:
        return {
            "action": "manual_review", "quantity_pieces": None,
            "expected_benefit_pieces": None, "formula_version": FORMULA_VERSION,
            "reason_codes": ["projection_limit_exceeded"],
            "review_period_days": REVIEW_DAYS, "safety_days": SAFETY_DAYS,
            "source_verified_external": False, "automatic_purchase": False,
            "note": "投影超出安全上限，请人工核算。",
        }
    return {
        "action": "replenish_candidate" if shortfall else "defer_candidate",
        "quantity_pieces": rounded if shortfall else 0,
        "expected_benefit_pieces": shortfall,
        "formula_version": FORMULA_VERSION,
        "reason_codes": ["source_attestations_not_independently_verified"],
        "calculation": {
            "daily_units": str(velocity), "lead_time_days": lead,
            "review_period_days": REVIEW_DAYS, "safety_days": SAFETY_DAYS,
            "safety_pieces": safety, "forecast_demand_pieces": expected,
            "available_pieces": available, "open_inbound_pieces": inbound,
            "shortfall_pieces": shortfall, "minimum_order_pieces": minimum,
            "pack_size_pieces": pack, "rounded_candidate_pieces": rounded,
        },
        "source_verified_external": False, "automatic_purchase": False,
        "note": "仅合成/自述来源的确定性候选；真实订单需另行人工源核查。",
    }


def _authorize(db: Session, actor: AuthenticatedSubject, *permissions: str) -> None:
    if actor.account_id is None or actor.session_id is None:
        _fail("recommendation_human_session_required", 403)
    db.flush()
    db.expire_all()
    for permission in permissions:
        authorize_scope(db, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission)
    if db.scalar(select(Subject.subject_type).where(
            Subject.id == actor.subject_id,
            Subject.organization_id == actor.organization_id)) != "person":
        _fail("recommendation_person_required", 403)


def _store_lock(db: Session, actor: AuthenticatedSubject) -> None:
    result = db.execute(update(Store).where(
        Store.organization_id == actor.organization_id, Store.id == actor.store_id,
        Store.active.is_(True)).values(active=Store.active).execution_options(
        synchronize_session=False))
    if result.rowcount != 1:
        _fail("recommendation_store_inactive", 403)
    db.expire_all()


def _product(db: Session, actor: AuthenticatedSubject,
             product_id: str, version: int) -> None:
    if type(version) is not int or version <= 0:
        _fail("recommendation_product_version_invalid", 422)
    row = db.scalar(select(CatalogProduct).where(
        CatalogProduct.organization_id == actor.organization_id,
        CatalogProduct.store_id == actor.store_id,
        CatalogProduct.id == product_id).with_for_update())
    if row is None or not row.active:
        _fail("recommendation_product_missing", 404)
    if row.version != version:
        _fail("recommendation_product_version_changed")


def _signature(row: PurchaseRecommendation) -> str:
    return _digest({
        "product_id": row.product_id, "product_version": row.product_version,
        "source_digest": row.source_digest,
        "formula_version": row.formula_version, "projection": row.projection,
    })


def _get(db: Session, actor: AuthenticatedSubject, recommendation_id: str):
    row = db.scalar(select(PurchaseRecommendation).where(
        PurchaseRecommendation.id == recommendation_id,
        PurchaseRecommendation.organization_id == actor.organization_id,
        PurchaseRecommendation.store_id == actor.store_id))
    if row is None:
        _fail("recommendation_not_found", 404)
    return row


def _decision(db: Session, actor: AuthenticatedSubject, row: PurchaseRecommendation):
    return db.scalar(select(PurchaseRecommendationDecision).where(
        PurchaseRecommendationDecision.organization_id == actor.organization_id,
        PurchaseRecommendationDecision.store_id == actor.store_id,
        PurchaseRecommendationDecision.recommendation_id == row.id))


def _assert_snapshot(row: PurchaseRecommendation) -> None:
    try:
        rebuilt = _project(row.source_evidence, _utc(row.created_at),
                           row.organization_id, row.store_id)
    except (ValueError, TypeError) as exc:
        _fail("recommendation_source_corrupted")
    if (row.formula_version != FORMULA_VERSION
            or row.source_digest != _digest(row.source_evidence)
            or row.projection != rebuilt
            or row.candidate_digest != _signature(row)):
        _fail("recommendation_snapshot_digest_mismatch")


def _view(db: Session, actor: AuthenticatedSubject,
          row: PurchaseRecommendation, *, replayed: bool = False) -> dict:
    _assert_snapshot(row)
    decision = _decision(db, actor, row)
    return {
        "id": row.id, "scope": [row.organization_id, row.store_id],
        "product_id": row.product_id, "product_version": row.product_version,
        "created_by": row.created_by, "created_at": _utc(row.created_at).isoformat(),
        "formula_version": row.formula_version, "source_digest": row.source_digest,
        "candidate_digest": row.candidate_digest,
        "sources": [
            { "kind": k, "ref": v["source"]["ref"],
              "version": v["source"]["version"], "sha256": v["source"]["sha256"],
              "as_of": v["source"]["as_of"] }
            for k, v in sorted(row.source_evidence.items()) if v is not None
        ],
        "projection": row.projection,
        "decision": None if decision is None else {
            "kind": decision.decision, "decided_by": decision.decided_by,
            "reason": decision.reason, "quantity_pieces": decision.quantity_pieces,
            "local_draft": decision.local_draft,
        },
        "replayed": replayed, "official_order_created": False,
    }


def capture_recommendation(
    db: Session, actor: AuthenticatedSubject, *,
    product_id: str, product_version: int, evidence: dict,
    idempotency_key: str, now: datetime | None = None,
) -> dict:
    """Trusted host captures an immutable calculation, never a purchasing action."""
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    _ident(product_id, "product_id")
    _ident(idempotency_key, "idempotency_key")
    _authorize(db, actor, "analytics.read", "purchasing.read", "catalog.read")
    projection = _project(evidence, instant, actor.organization_id, actor.store_id)
    frozen = jsonable_encoder(evidence)
    source_digest = _digest(frozen)
    _store_lock(db, actor)
    _authorize(db, actor, "analytics.read", "purchasing.read", "catalog.read")
    existing = db.scalar(select(PurchaseRecommendation).where(
        PurchaseRecommendation.organization_id == actor.organization_id,
        PurchaseRecommendation.store_id == actor.store_id,
        PurchaseRecommendation.created_by == actor.subject_id,
        PurchaseRecommendation.request_key == idempotency_key))
    if existing:
        if (existing.product_id != product_id
                or existing.product_version != product_version
                or existing.source_digest != source_digest):
            _fail("recommendation_idempotency_conflict")
        return _view(db, actor, existing, replayed=True)
    _product(db, actor, product_id, product_version)
    row = PurchaseRecommendation(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, product_id=product_id,
        product_version=product_version, created_by=actor.subject_id,
        request_key=idempotency_key, formula_version=FORMULA_VERSION,
        source_digest=source_digest, candidate_digest="0" * 64,
        source_evidence=frozen, projection=projection, created_at=instant)
    row.candidate_digest = _signature(row)
    db.add(row)
    db.flush()
    record_event(
        db, actor_id=actor.subject_id,
        organization_id=actor.organization_id, store_id=actor.store_id,
        action="analytics.recommendation.captured",
        target_type="purchase_recommendation", target_id=row.id,
        idempotency_key="ana005:capture:" + row.id,
        details={"candidate_digest": row.candidate_digest,
                 "source_digest": source_digest, "action": projection["action"],
                 "external_dispatch": False})
    return _view(db, actor, row)


def decide_recommendation(
    db: Session, actor: AuthenticatedSubject, recommendation_id: str, *,
    candidate_digest: str, expected_product_version: int, decision: str,
    reason: str, idempotency_key: str, quantity_pieces: int | None = None,
    now: datetime | None = None,
) -> dict:
    """One immutable human decision; accepted result is LOCAL handoff only."""
    instant = _utc(now) if now is not None else datetime.now(timezone.utc)
    _ident(idempotency_key, "idempotency_key")
    if decision not in ("accept", "modify", "reject", "defer"):
        _fail("recommendation_decision_invalid", 422)
    if type(reason) is not str or not 1 <= len(reason.strip()) <= 1000:
        _fail("recommendation_reason_required", 422)
    if type(candidate_digest) is not str or _HASH.fullmatch(candidate_digest) is None:
        _fail("recommendation_candidate_digest_invalid", 422)
    _authorize(db, actor, "analytics.read", "purchasing.read", "purchasing.write")
    _store_lock(db, actor)
    _authorize(db, actor, "analytics.read", "purchasing.read", "purchasing.write")
    row = _get(db, actor, recommendation_id)
    _assert_snapshot(row)
    if row.candidate_digest != candidate_digest or row.product_version != expected_product_version:
        _fail("recommendation_snapshot_cas_changed")
    old = _decision(db, actor, row)
    if old is not None:
        if (old.decided_by != actor.subject_id or old.decision_key != idempotency_key
                or old.decision != decision or old.reason != reason.strip()
                or old.quantity_pieces != quantity_pieces
                and not (old.decision == "accept" and quantity_pieces is None)):
            _fail("recommendation_already_decided")
        return _view(db, actor, row, replayed=True)
    if instant < _utc(row.created_at) or instant - _utc(row.created_at) > timedelta(hours=24):
        _fail("recommendation_expired")
    _product(db, actor, row.product_id, row.product_version)
    refreshed = _project(row.source_evidence, instant,
                         row.organization_id, row.store_id)
    if refreshed["action"] != row.projection["action"]:
        _fail("recommendation_sources_changed_or_stale")
    proposal = row.projection
    if decision in ("accept", "modify"):
        if proposal["action"] != "replenish_candidate":
            _fail("recommendation_quantity_unproven")
        pack = proposal["calculation"]["pack_size_pieces"]
        minimum = proposal["calculation"]["minimum_order_pieces"]
        if decision == "accept":
            if quantity_pieces is not None:
                _fail("recommendation_accept_override_denied", 422)
            quantity = proposal["quantity_pieces"]
        else:
            if type(quantity_pieces) is not int or not 1 <= quantity_pieces <= _MAX_PIECES:
                _fail("recommendation_modified_quantity_invalid", 422)
            quantity = quantity_pieces
        if quantity < minimum or quantity % pack != 0:
            _fail("recommendation_pack_constraint", 422)
        draft = {
            "kind": "unverified_analytical_handoff", "recommendation_id": row.id,
            "product_id": row.product_id, "expected_product_version": row.product_version,
            "quantity_pieces": quantity, "candidate_digest": row.candidate_digest,
            "source_verified_external": False, "requires_purchase_review": True,
            "formal_purchase_intent_id": None, "erp_order_id": None,
        }
    else:
        if quantity_pieces is not None:
            _fail("recommendation_quantity_unexpected", 422)
        quantity, draft = None, None
    receipt = PurchaseRecommendationDecision(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, recommendation_id=row.id,
        decided_by=actor.subject_id, decision_key=idempotency_key,
        decision=decision, reason=reason.strip(),
        expected_candidate_digest=row.candidate_digest,
        quantity_pieces=quantity, local_draft=draft, decided_at=instant)
    db.add(receipt)
    db.flush()
    record_event(
        db, actor_id=actor.subject_id,
        organization_id=actor.organization_id, store_id=actor.store_id,
        action="analytics.recommendation.decided",
        target_type="purchase_recommendation", target_id=row.id,
        idempotency_key="ana005:decision:" + receipt.id,
        details={"decision": decision, "decision_id": receipt.id,
                 "candidate_digest": row.candidate_digest,
                 "formal_purchase_created": False})
    return _view(db, actor, row)


def read_recommendation(db: Session, actor: AuthenticatedSubject,
                        recommendation_id: str) -> dict:
    _authorize(db, actor, "analytics.read", "purchasing.read")
    return _view(db, actor, _get(db, actor, recommendation_id))
