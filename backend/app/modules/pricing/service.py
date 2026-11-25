"""Deterministic quote policy, immutable snapshots and approval binding (SALE-001)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, localcontext
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.approvals.models import ApprovalRequest
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.pricing.models import PricingQuote, PricingQuoteApproval, PricingRule
from backend.app.modules.pricing.schemas import (
    ApprovalBindingResponse, PriceSourceInput, QuoteLine, QuoteRequest, QuoteResponse,
    RevalidationResponse,
)


class PricingError(Exception):
    """Stable, non-leaking pricing rejection."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


ROUNDINGS = {
    "ROUND_HALF_UP": ROUND_HALF_UP,
    "ROUND_CEILING": ROUND_CEILING,
    "ROUND_DOWN": ROUND_DOWN,
}
DIGEST_FIELDS = (
    "rule_id", "rule_key", "rule_version", "requester_id", "quote_key", "created_at",
    "object_type", "object_id", "object_version", "price_source_snapshot_id",
    "price_source_payload_sha256",
    "product_id", "quantity", "currency", "amount_precision", "rounding", "list_price",
    "unit_price", "discount_bps", "final_amount", "price_source_type", "price_source_ref",
    "price_source_version", "source_observed_at", "source_expires_at", "cost_amount",
    "cost_source_type", "cost_source_ref", "cost_observed_at", "status", "expires_at",
)


def _utc(value: datetime | None, *, required: bool = True) -> datetime:
    if value is None:
        if required:
            raise PricingError("pricing_amount_invalid")
        return None  # type: ignore[return-value]
    if value.tzinfo is None or value.utcoffset() is None:
        raise PricingError("pricing_amount_invalid")
    return value.astimezone(timezone.utc)


def _current(value: datetime | None) -> datetime:
    return _utc(value) if value is not None else datetime.now(timezone.utc)


def _stored_utc(value: datetime | None) -> datetime | None:
    """Normalize a DB value: SQLite drops tzinfo from DateTime columns."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _require(session: Session, actor: AuthenticatedSubject, permission: str, now: datetime) -> None:
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)
    except HTTPException as exc:
        raise PricingError("pricing_scope_denied") from exc


def _decimal(value: Decimal, code: str = "pricing_amount_invalid") -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PricingError(code)
    return value


def _quantize(value: Decimal, precision: int, rounding: str) -> Decimal:
    quantum = Decimal(1).scaleb(-precision)
    try:
        return value.quantize(quantum, rounding=ROUNDINGS[rounding])
    except (InvalidOperation, KeyError) as exc:
        raise PricingError("pricing_amount_invalid") from exc


def _money(value: Decimal) -> str:
    return format(value, "f")


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(values: dict) -> str:
    return hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()


def _rule(session: Session, actor: AuthenticatedSubject, key: str, version: int | None,
          now: datetime) -> PricingRule:
    stmt = select(PricingRule).where(
        PricingRule.organization_id == actor.organization_id,
        PricingRule.store_id == actor.store_id,
        PricingRule.rule_key == key,
    )
    if version is not None:
        stmt = stmt.where(PricingRule.version == version)
    else:
        stmt = stmt.where(PricingRule.active.is_(True)).where(
            PricingRule.effective_from <= now).where(
            (PricingRule.effective_until.is_(None)) | (PricingRule.effective_until > now))
    rows = list(session.scalars(stmt.order_by(PricingRule.version.desc())))
    if not rows:
        raise PricingError("pricing_rule_missing")
    if len(rows) > 1 and version is None:
        raise PricingError("pricing_rule_ambiguous")
    rule = rows[0]
    if not rule.active or rule.effective_from > now or (
            rule.effective_until is not None and rule.effective_until <= now):
        raise PricingError("pricing_rule_expired")
    return rule


def _source(session: Session, rule: PricingRule, source: PriceSourceInput,
            now: datetime) -> tuple:
    if source.source_type not in (rule.price_source_types or []):
        raise PricingError("pricing_source_denied")
    if source.source_type != "catalog":
        raise PricingError("pricing_source_denied")
    if source.currency != rule.currency:
        raise PricingError("pricing_currency_mismatch")
    from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
    external_code = source.source_ref.split(":", 1)[-1]
    if source.source_ref != f"{source.source_type}:{external_code}" or not external_code:
        raise PricingError("pricing_source_denied")
    product = session.scalar(select(CatalogProduct).where(
        CatalogProduct.organization_id == rule.organization_id,
        CatalogProduct.store_id == rule.store_id,
        CatalogProduct.source_system == source.source_type,
        CatalogProduct.external_code == external_code,
        CatalogProduct.source_version == source.source_version,
        CatalogProduct.active.is_(True),
    ))
    if product is None or product.current_snapshot_id is None:
        raise PricingError("pricing_source_denied")
    snapshot = session.scalar(select(CatalogSnapshot).where(
        CatalogSnapshot.id == product.current_snapshot_id,
        CatalogSnapshot.product_id == product.id,
        CatalogSnapshot.organization_id == rule.organization_id,
        CatalogSnapshot.store_id == rule.store_id,
        CatalogSnapshot.source_version == source.source_version,
    ))
    if snapshot is None:
        raise PricingError("pricing_source_denied")
    payload = snapshot.payload or {}
    sale = payload.get("sell_price")
    if not isinstance(sale, (int, float)) or sale <= 0:
        raise PricingError("pricing_source_denied")
    observed = snapshot.source_updated_at or snapshot.captured_at
    observed = _stored_utc(observed)
    if observed is None or observed > now or \
            (now - observed).total_seconds() > rule.source_max_age_seconds:
        raise PricingError("pricing_source_expired")
    if source.expires_at is not None and _utc(source.expires_at) <= now:
        raise PricingError("pricing_source_expired")
    return product, snapshot, observed, Decimal(str(sale)), payload.get("cost_price")


def _compute(session: Session, rule: PricingRule, request: QuoteRequest, now: datetime) -> dict:
    product, snapshot, observed, list_price, raw_cost = _source(
        session, rule, request.price_source, now)
    if request.line.product_id != product.external_code:
        raise PricingError("pricing_source_denied")
    list_price = _quantize(list_price, rule.amount_precision, rule.rounding)
    if list_price <= 0:
        raise PricingError("pricing_source_denied")
    if request.discount_bps > rule.max_discount_bps:
        raise PricingError("pricing_discount_exceeded")
    with localcontext() as context:
        context.prec = 80
        unit_price = _quantize(
            list_price * (Decimal(10000 - request.discount_bps) / Decimal(10000)),
            rule.amount_precision, rule.rounding)
        final_amount = _quantize(unit_price * _decimal(request.line.quantity),
                                 rule.amount_precision, rule.rounding)
    if final_amount < 0:
        raise PricingError("pricing_amount_invalid")
    source_expiry = _utc(request.price_source.expires_at, required=False)
    expiries = [now + timedelta(seconds=rule.quote_ttl_seconds),
                observed + timedelta(seconds=rule.source_max_age_seconds)]
    if rule.effective_until is not None:
        expiries.append(_utc(rule.effective_until))
    if source_expiry is not None:
        expiries.append(source_expiry)
    expires_at = min(expiries)
    if expires_at <= now:
        raise PricingError("pricing_source_expired")
    cost_amount = None
    cost_source_type = cost_source_ref = None
    cost_observed_at = None
    if raw_cost is not None:
        if not isinstance(raw_cost, (int, float)) or raw_cost < 0:
            raise PricingError("pricing_source_denied")
        cost_amount = _money(_quantize(Decimal(str(raw_cost)),
                                       rule.amount_precision, rule.rounding))
        cost_source_type = request.price_source.source_type
        cost_source_ref = request.price_source.source_ref
        cost_observed_at = observed
    status = "approval_required" if request.discount_bps > rule.approval_threshold_bps else "quoted"
    return {
        "rule_id": rule.id, "rule_key": rule.rule_key, "rule_version": rule.version,
        "object_type": request.line.object_type, "object_id": request.line.object_id,
        "object_version": request.line.object_version, "product_id": request.line.product_id,
        "quantity": format(_decimal(request.line.quantity), "f"), "currency": rule.currency,
        "amount_precision": rule.amount_precision, "rounding": rule.rounding,
        "list_price": _money(list_price), "unit_price": _money(unit_price),
        "discount_bps": request.discount_bps, "final_amount": _money(final_amount),
        "price_source_type": request.price_source.source_type,
        "price_source_ref": request.price_source.source_ref,
        "price_source_version": request.price_source.source_version,
        "price_source_snapshot_id": snapshot.id,
        "price_source_payload_sha256": snapshot.payload_sha256,
        "source_observed_at": observed,
        "source_expires_at": source_expiry,
        "cost_amount": cost_amount, "cost_source_type": cost_source_type,
        "cost_source_ref": cost_source_ref, "cost_observed_at": cost_observed_at,
        "status": status, "expires_at": expires_at,
    }


def _snapshot_digest(values: dict) -> str:
    payload = {key: values[key] for key in DIGEST_FIELDS}
    payload["source_observed_at"] = values["source_observed_at"].isoformat()
    payload["source_expires_at"] = values["source_expires_at"].isoformat() if values["source_expires_at"] else None
    payload["cost_observed_at"] = values["cost_observed_at"].isoformat() if values["cost_observed_at"] else None
    payload["created_at"] = values["created_at"].isoformat()
    payload["expires_at"] = values["expires_at"].isoformat()
    return _digest(payload)


def _matches(quote: PricingQuote, values: dict) -> bool:
    for key in ("rule_id", "rule_key", "rule_version", "object_type", "object_id",
                "object_version", "product_id", "quantity", "currency", "amount_precision",
                "rounding", "list_price", "unit_price", "discount_bps", "final_amount",
                "price_source_type", "price_source_ref", "price_source_version",
                "price_source_snapshot_id", "price_source_payload_sha256", "status",
                "cost_amount", "cost_source_type", "cost_source_ref"):
        if getattr(quote, key) != values[key]:
            return False
    for key in ("source_observed_at", "source_expires_at", "cost_observed_at"):
        stored = _stored_utc(getattr(quote, key))
        wanted = values[key]
        if stored is None or wanted is None:
            if stored is not wanted:
                return False
        elif stored != wanted:
            return False
    return True


def _response(quote: PricingQuote, *, include_cost: bool = False) -> QuoteResponse:
    return QuoteResponse(
        id=quote.id, quote_key=quote.quote_key, rule_key=quote.rule_key,
        rule_version=quote.rule_version, currency=quote.currency,
        amount_precision=quote.amount_precision, rounding=quote.rounding,
        object_type=quote.object_type, object_id=quote.object_id,
        object_version=quote.object_version, product_id=quote.product_id,
        quantity=quote.quantity, list_price=quote.list_price, unit_price=quote.unit_price,
        discount_bps=quote.discount_bps, final_amount=quote.final_amount,
        price_source_type=quote.price_source_type, price_source_ref=quote.price_source_ref,
        price_source_version=quote.price_source_version,
        source_observed_at=_stored_utc(quote.source_observed_at),
        source_expires_at=_stored_utc(quote.source_expires_at),
        status=quote.status, requires_approval=quote.status == "approval_required",
        expires_at=_stored_utc(quote.expires_at), snapshot_digest=quote.snapshot_digest,
        created_at=_stored_utc(quote.created_at),
        cost_amount=quote.cost_amount if include_cost else None,
        cost_source_type=quote.cost_source_type if include_cost else None,
        cost_source_ref=quote.cost_source_ref if include_cost else None,
        cost_observed_at=_stored_utc(quote.cost_observed_at) if include_cost else None,
    )


def _owned_quote(session: Session, actor: AuthenticatedSubject, quote_id: str) -> PricingQuote:
    quote = session.scalar(select(PricingQuote).where(
        PricingQuote.organization_id == actor.organization_id,
        PricingQuote.store_id == actor.store_id,
        PricingQuote.id == quote_id,
    ))
    if quote is None:
        raise PricingError("pricing_scope_denied")
    return quote


def _audit_rejection(session: Session, actor: AuthenticatedSubject, quote_key: str,
                     code: str, now: datetime) -> None:
    if not isinstance(quote_key, str) or not 1 <= len(quote_key) <= 200:
        return
    digest = hashlib.sha256(
        f"{actor.organization_id}:{actor.store_id}:{quote_key}:{code}".encode("utf-8")
    ).hexdigest()
    try:
        record_event(
            session, actor_id=actor.subject_id, organization_id=actor.organization_id,
            store_id=actor.store_id, action="pricing.quote_rejected",
            target_type="pricing_request", target_id=digest,
            idempotency_key=f"pricing.reject:{digest}",
            details={"code": code}, occurred_at=now,
        )
    except Exception as error:
        raise PricingError("pricing_audit_failed") from error


def create_quote(session: Session, actor: AuthenticatedSubject, request: QuoteRequest,
                 *, now: datetime | None = None) -> QuoteResponse:
    current = _current(now)
    _require(session, actor, "pricing.quote", current)
    try:
        return _create_quote(session, actor, request, current)
    except PricingError as error:
        _audit_rejection(session, actor, request.quote_key, error.code, current)
        raise


def _create_quote(session: Session, actor: AuthenticatedSubject, request: QuoteRequest,
                  current: datetime) -> QuoteResponse:
    rule = _rule(session, actor, request.rule_key, None, current)
    values = _compute(session, rule, request, current)
    existing = session.scalar(select(PricingQuote).where(
        PricingQuote.organization_id == actor.organization_id,
        PricingQuote.store_id == actor.store_id,
        PricingQuote.quote_key == request.quote_key,
    ))
    if existing is not None:
        if existing.requester_id != actor.subject_id:
            raise PricingError("pricing_scope_denied")
        if not _matches(existing, values):
            raise PricingError("pricing_snapshot_conflict")
        include_cost = _has_cost_permission(session, actor, current)
        return _response(existing, include_cost=include_cost)
    digest = _snapshot_digest({**values, "requester_id": actor.subject_id,
                               "quote_key": request.quote_key, "created_at": current})
    quote = PricingQuote(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        quote_key=request.quote_key, requester_id=actor.subject_id, snapshot_digest=digest,
        created_at=current, **values,
    )
    try:
        with session.begin_nested():
            session.add(quote)
            session.flush()
    except IntegrityError as exc:
        existing = session.scalar(select(PricingQuote).where(
            PricingQuote.organization_id == actor.organization_id,
            PricingQuote.store_id == actor.store_id,
            PricingQuote.quote_key == request.quote_key,
        ))
        if existing is None:
            raise PricingError("pricing_snapshot_conflict") from exc
        if existing.requester_id != actor.subject_id:
            raise PricingError("pricing_scope_denied") from exc
        if not _matches(existing, values):
            raise PricingError("pricing_snapshot_conflict") from exc
        return _response(existing, include_cost=_has_cost_permission(session, actor, current))
    record_event(
        session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action="pricing.quote_created", target_type="pricing_quote",
        target_id=quote.id, idempotency_key=f"pricing.quote:{quote.id}:created",
        details={"rule_key": rule.rule_key, "rule_version": rule.version,
                 "status": quote.status, "currency": quote.currency,
                 "final_amount": quote.final_amount},
        occurred_at=current,
    )
    return _response(quote, include_cost=_has_cost_permission(session, actor, current))


def _has_cost_permission(session: Session, actor: AuthenticatedSubject, now: datetime) -> bool:
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission="pricing.cost.read", now=now)
        return True
    except HTTPException:
        return False


def get_quote(session: Session, actor: AuthenticatedSubject, quote_id: str,
              *, now: datetime | None = None, include_cost: bool = False) -> QuoteResponse:
    current = _current(now)
    _require(session, actor, "pricing.quote", current)
    quote = _owned_quote(session, actor, quote_id)
    if quote.requester_id != actor.subject_id and not _has_cost_permission(session, actor, current):
        raise PricingError("pricing_scope_denied")
    return _response(quote, include_cost=include_cost and _has_cost_permission(session, actor, current))


def _approval(session: Session, actor: AuthenticatedSubject, approval_id: str) -> ApprovalRequest:
    approval = session.scalar(select(ApprovalRequest).where(
        ApprovalRequest.organization_id == actor.organization_id,
        ApprovalRequest.store_id == actor.store_id,
        ApprovalRequest.id == approval_id,
    ))
    if approval is None:
        raise PricingError("pricing_approval_invalid")
    return approval


def _approver_authorized(session: Session, approval: ApprovalRequest, now: datetime) -> None:
    if approval.decided_by is None:
        raise PricingError("pricing_approval_invalid")
    approver = AuthenticatedSubject(approval.decided_by, approval.organization_id,
                                    approval.store_id, now + timedelta(hours=1))
    try:
        authorize_scope(session, approver, organization_id=approval.organization_id,
                        store_id=approval.store_id, permission="pricing.approve", now=now)
    except HTTPException as exc:
        raise PricingError("pricing_approval_invalid") from exc


def bind_approval(session: Session, actor: AuthenticatedSubject, *, quote_id: str,
                  approval_id: str, now: datetime | None = None) -> ApprovalBindingResponse:
    current = _current(now)
    _require(session, actor, "pricing.approve", current)
    quote = _owned_quote(session, actor, quote_id)
    if quote.status != "approval_required":
        raise PricingError("pricing_approval_invalid")
    if _stored_utc(quote.expires_at) <= current:
        raise PricingError("pricing_quote_expired")
    approval = _approval(session, actor, approval_id)
    if approval.state != "approved" or approval.subject_type != "quote_snapshot" or \
            approval.subject_id != quote.id or approval.action_key != "pricing.quote_exception":
        raise PricingError("pricing_approval_invalid")
    if _stored_utc(approval.expires_at) <= current:
        raise PricingError("pricing_approval_expired")
    _approver_authorized(session, approval, current)
    binding = session.scalar(select(PricingQuoteApproval).where(
        PricingQuoteApproval.organization_id == actor.organization_id,
        PricingQuoteApproval.store_id == actor.store_id,
        PricingQuoteApproval.quote_id == quote.id,
    ))
    if binding is not None:
        if binding.approval_id != approval.id:
            raise PricingError("pricing_snapshot_conflict")
        return ApprovalBindingResponse(
            id=binding.id, quote_id=binding.quote_id, approval_id=binding.approval_id,
            object_type=binding.object_type, object_id=binding.object_id,
            object_version=binding.object_version, snapshot_digest=binding.snapshot_digest,
            rule_version=binding.rule_version, state=binding.state,
            expires_at=_stored_utc(binding.expires_at),
            last_validated_at=_stored_utc(binding.last_validated_at),
        )
    expires_at = min(_stored_utc(quote.expires_at), _stored_utc(approval.expires_at))
    binding = PricingQuoteApproval(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        quote_id=quote.id, approval_id=approval.id, object_type=quote.object_type,
        object_id=quote.object_id, object_version=quote.object_version,
        snapshot_digest=quote.snapshot_digest, rule_id=quote.rule_id,
        rule_version=quote.rule_version, state="bound", bound_by=actor.subject_id,
        bound_at=current, expires_at=expires_at,
    )
    try:
        with session.begin_nested():
            session.add(binding)
            session.flush()
    except IntegrityError as exc:
        existing_binding = session.scalar(select(PricingQuoteApproval).where(
            PricingQuoteApproval.organization_id == actor.organization_id,
            PricingQuoteApproval.store_id == actor.store_id,
            PricingQuoteApproval.quote_id == quote.id,
        ))
        if existing_binding is not None and existing_binding.approval_id == approval.id:
            return ApprovalBindingResponse(
                id=existing_binding.id, quote_id=existing_binding.quote_id,
                approval_id=existing_binding.approval_id,
                object_type=existing_binding.object_type, object_id=existing_binding.object_id,
                object_version=existing_binding.object_version,
                snapshot_digest=existing_binding.snapshot_digest,
                rule_version=existing_binding.rule_version, state=existing_binding.state,
                expires_at=_stored_utc(existing_binding.expires_at),
                last_validated_at=_stored_utc(existing_binding.last_validated_at),
            )
        raise PricingError("pricing_snapshot_conflict") from exc
    record_event(
        session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action="pricing.quote_approval_bound",
        target_type="pricing_quote_approval", target_id=binding.id,
        idempotency_key=f"pricing.binding:{binding.id}:bound",
        details={"quote_id": quote.id, "approval_id": approval.id,
                 "rule_version": quote.rule_version, "snapshot_digest": quote.snapshot_digest},
        occurred_at=current,
    )
    return ApprovalBindingResponse(
        id=binding.id, quote_id=quote.id, approval_id=approval.id,
        object_type=binding.object_type, object_id=binding.object_id,
        object_version=binding.object_version, snapshot_digest=binding.snapshot_digest,
        rule_version=binding.rule_version, state=binding.state,
        expires_at=_stored_utc(binding.expires_at), last_validated_at=None,
    )


def revoke_binding(session: Session, actor: AuthenticatedSubject, *, binding_id: str,
                   reason: str, now: datetime | None = None) -> ApprovalBindingResponse:
    current = _current(now)
    _require(session, actor, "pricing.approve", current)
    if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
        raise PricingError("pricing_approval_invalid")
    binding = session.scalar(select(PricingQuoteApproval).where(
        PricingQuoteApproval.organization_id == actor.organization_id,
        PricingQuoteApproval.store_id == actor.store_id,
        PricingQuoteApproval.id == binding_id,
    ))
    if binding is None or binding.state != "bound":
        raise PricingError("pricing_approval_invalid")
    binding.state = "revoked"
    record_event(
        session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action="pricing.quote_binding_revoked",
        target_type="pricing_quote_approval", target_id=binding.id,
        idempotency_key=f"pricing.binding:{binding.id}:revoked",
        details={"quote_id": binding.quote_id, "reason": reason}, occurred_at=current,
    )
    return ApprovalBindingResponse(
        id=binding.id, quote_id=binding.quote_id, approval_id=binding.approval_id,
        object_type=binding.object_type, object_id=binding.object_id,
        object_version=binding.object_version, snapshot_digest=binding.snapshot_digest,
        rule_version=binding.rule_version, state=binding.state,
        expires_at=_stored_utc(binding.expires_at),
        last_validated_at=_stored_utc(binding.last_validated_at),
    )


def revalidate_approval(session: Session, actor: AuthenticatedSubject, *, binding_id: str,
                        object_version: str, source_version: str,
                        now: datetime | None = None) -> RevalidationResponse:
    current = _current(now)
    _require(session, actor, "pricing.approve", current)
    binding = session.scalar(select(PricingQuoteApproval).where(
        PricingQuoteApproval.organization_id == actor.organization_id,
        PricingQuoteApproval.store_id == actor.store_id,
        PricingQuoteApproval.id == binding_id,
    ))
    if binding is None or binding.state != "bound":
        raise PricingError("pricing_approval_invalid")
    quote = _owned_quote(session, actor, binding.quote_id)
    if _stored_utc(binding.expires_at) <= current or _stored_utc(quote.expires_at) <= current:
        raise PricingError("pricing_approval_expired")
    if object_version != binding.object_version or quote.object_version != binding.object_version:
        raise PricingError("pricing_object_changed")
    if source_version != quote.price_source_version:
        raise PricingError("pricing_source_expired")
    if quote.snapshot_digest != binding.snapshot_digest:
        raise PricingError("pricing_object_changed")
    _validate_quote_current(session, actor, quote, current)
    approval = _approval(session, actor, binding.approval_id)
    if approval.state != "approved" or approval.subject_type != "quote_snapshot" or \
            approval.subject_id != quote.id or approval.action_key != "pricing.quote_exception":
        raise PricingError("pricing_approval_invalid")
    if _stored_utc(approval.expires_at) <= current:
        raise PricingError("pricing_approval_expired")
    _approver_authorized(session, approval, current)
    binding.last_validated_at = current
    record_event(
        session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action="pricing.quote_revalidated",
        target_type="pricing_quote_approval", target_id=binding.id,
        idempotency_key=f"pricing.binding:{binding.id}:revalidated:{current.isoformat()}",
        details={"quote_id": quote.id, "object_version": object_version,
                 "rule_version": quote.rule_version},
        occurred_at=current,
    )
    return RevalidationResponse(
        binding_id=binding.id, quote_id=quote.id, object_version=object_version,
        valid=True, revalidated_at=current, expires_at=_stored_utc(binding.expires_at),
    )


def _validate_quote_current(session, actor, quote, current):
    """Internal policy check; callers authorize the business operation first."""
    if _stored_utc(quote.expires_at) <= current:
        raise PricingError("pricing_quote_expired")
    rule = _rule(session, actor, quote.rule_key, quote.rule_version, current)
    from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
    external_code = quote.price_source_ref.split(":", 1)[-1]
    if quote.price_source_ref != f"{quote.price_source_type}:{external_code}":
        raise PricingError("pricing_source_changed")
    product = session.scalar(select(CatalogProduct).where(
        CatalogProduct.organization_id == actor.organization_id,
        CatalogProduct.store_id == actor.store_id,
        CatalogProduct.source_system == quote.price_source_type,
        CatalogProduct.external_code == external_code,
        CatalogProduct.active.is_(True),
    ))
    if product is None or product.current_snapshot_id != quote.price_source_snapshot_id:
        raise PricingError("pricing_source_changed")
    snapshot = session.scalar(select(CatalogSnapshot).where(
        CatalogSnapshot.id == quote.price_source_snapshot_id,
        CatalogSnapshot.product_id == product.id,
        CatalogSnapshot.organization_id == actor.organization_id,
        CatalogSnapshot.store_id == actor.store_id,
        CatalogSnapshot.source_version == quote.price_source_version,
    ))
    if snapshot is None or snapshot.payload_sha256 != quote.price_source_payload_sha256:
        raise PricingError("pricing_source_changed")
    sell = (snapshot.payload or {}).get("sell_price")
    if not isinstance(sell, (int, float)) or sell <= 0 or \
            _money(_quantize(Decimal(str(sell)), quote.amount_precision, quote.rounding)) != quote.list_price:
        raise PricingError("pricing_source_changed")
    raw_cost = (snapshot.payload or {}).get("cost_price")
    expected_cost = None if raw_cost is None else _money(_quantize(
        Decimal(str(raw_cost)), quote.amount_precision, quote.rounding))
    if quote.cost_amount != expected_cost:
        raise PricingError("pricing_source_changed")
    observed = _stored_utc(quote.source_observed_at)
    if observed is None or observed > current or (current - observed).total_seconds() > rule.source_max_age_seconds:
        raise PricingError("pricing_source_expired")
    values = {key: getattr(quote, key) for key in DIGEST_FIELDS}
    for key in ("source_observed_at", "source_expires_at", "cost_observed_at",
                "expires_at", "created_at"):
        values[key] = _stored_utc(values[key])
    if _snapshot_digest(values) != quote.snapshot_digest:
        raise PricingError("pricing_object_changed")
    if rule.id != quote.rule_id:
        raise PricingError("pricing_rule_expired")
    if quote.price_source_type not in (rule.price_source_types or []):
        raise PricingError("pricing_source_denied")
    if quote.source_expires_at is not None and _stored_utc(quote.source_expires_at) <= current:
        raise PricingError("pricing_source_expired")


def validate_quote_for_execution(session, actor, quote_id, *, now):
    """Internal use by authorized sales operations; grants no approval rights."""
    quote = _owned_quote(session, actor, quote_id)
    session.refresh(quote)
    _validate_quote_current(session, actor, quote, now)
    if quote.status == "approval_required":
        binding = session.scalar(select(PricingQuoteApproval).where(
            PricingQuoteApproval.organization_id == actor.organization_id,
            PricingQuoteApproval.store_id == actor.store_id,
            PricingQuoteApproval.quote_id == quote.id))
        if (binding is None or binding.state != "bound" or
                _stored_utc(binding.expires_at) <= now or
                binding.snapshot_digest != quote.snapshot_digest or
                binding.object_type != quote.object_type or binding.object_id != quote.object_id or
                binding.object_version != quote.object_version or binding.rule_id != quote.rule_id or
                binding.rule_version != quote.rule_version):
            raise PricingError("pricing_approval_invalid")
        approval = _approval(session, actor, binding.approval_id)
        session.refresh(approval)
        if (approval.state != "approved" or approval.subject_type != "quote_snapshot" or
                approval.subject_id != quote.id or approval.action_key != "pricing.quote_exception" or
                _stored_utc(approval.expires_at) <= now):
            raise PricingError("pricing_approval_invalid")
        _approver_authorized(session, approval, now)
        from backend.app.modules.auth.models import EmployeeAccount
        account = session.scalar(select(EmployeeAccount).where(
            EmployeeAccount.organization_id == actor.organization_id,
            EmployeeAccount.store_id == actor.store_id,
            EmployeeAccount.subject_id == approval.decided_by).execution_options(populate_existing=True))
        if (account is None or not account.active or
                (account.locked_until is not None and _stored_utc(account.locked_until) > now)):
            raise PricingError("pricing_approval_invalid")
    elif quote.status != "quoted":
        raise PricingError("pricing_approval_invalid")
    return quote


# SALE-005 v2 is deliberately independent of _source/_compute/DIGEST_FIELDS.
# Versioned supported ISO 4217 monetary codes (no unknown/test/fund codes).
# SIX maintenance amendments 176/180: XCG replaces ANG; BGN is historic from 2026.
# https://www.six-group.com/en/products-services/financial-information/market-reference-data/data-standards.html
CURRENCIES_V2 = frozenset("AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG".split())


def _v2_row_digest(row):
    return _digest({c.name: getattr(row, c.name) for c in row.__table__.columns})


def currency_policy_v2(session, actor, source_system, now):
    from backend.app.modules.pricing.models import SourceCurrencyPolicyV2
    rows = session.scalars(select(SourceCurrencyPolicyV2).where(
        SourceCurrencyPolicyV2.organization_id == actor.organization_id,
        SourceCurrencyPolicyV2.store_id == actor.store_id,
        SourceCurrencyPolicyV2.source_system == source_system,
    ).order_by(SourceCurrencyPolicyV2.version.desc()).execution_options(populate_existing=True))
    latest = {}
    for row in rows:
        latest.setdefault(row.policy_key, row)
    valid = [p for p in latest.values() if p.state == "active" and p.effective_from <= now < p.effective_until]
    if len(valid) != 1:
        raise PricingError("pricing_v2_currency_policy_hold")
    policy = valid[0]
    import re
    if (policy.currency not in CURRENCIES_V2 or not policy.evidence_ref.strip()
            or re.fullmatch(r"[0-9a-f]{64}", policy.evidence_sha256) is None):
        raise PricingError("pricing_v2_currency_policy_hold")
    return policy


def resolve_context_v2(session, actor, product_id, authority, *, currency=None, now=None):
    """One read-only verifier used at preview, quote, binding and root commit."""
    from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
    current = _current(now)
    product = session.scalar(select(CatalogProduct).where(
        CatalogProduct.id == product_id, CatalogProduct.organization_id == actor.organization_id,
        CatalogProduct.store_id == actor.store_id, CatalogProduct.active.is_(True)
    ).execution_options(populate_existing=True))
    if product is None or not product.current_snapshot_id or product.source_system in ("", "catalog"):
        raise PricingError("pricing_v2_identity_hold")
    snapshot = session.scalar(select(CatalogSnapshot).where(
        CatalogSnapshot.id == product.current_snapshot_id, CatalogSnapshot.product_id == product.id,
        CatalogSnapshot.organization_id == actor.organization_id, CatalogSnapshot.store_id == actor.store_id,
        CatalogSnapshot.source_version == product.source_version
    ).execution_options(populate_existing=True))
    if snapshot is None or not isinstance(snapshot.payload, dict):
        raise PricingError("pricing_v2_snapshot_hold")
    payload = snapshot.payload
    observed = _stored_utc(snapshot.source_updated_at or snapshot.captured_at)
    captured = _stored_utc(snapshot.captured_at)
    updated = _stored_utc(snapshot.source_updated_at)
    observation = {"payload": payload, "source_version": snapshot.source_version,
                   "source_updated_at": updated.isoformat() if updated else None}
    if (_digest(observation) != snapshot.payload_sha256 or payload.get("external_code") != product.external_code
            or _stored_utc(product.source_updated_at) != updated
            or observed is None or captured is None or observed > current or captured > current or observed > captured):
        raise PricingError("pricing_v2_snapshot_hold")
    raw = payload.get("raw")
    standards = raw.get("baseProductStandardList") if isinstance(raw, dict) else None
    if standards is not None and (not isinstance(standards, list) or len(standards) != 1):
        raise PricingError("pricing_v2_unit_hold")
    unit = payload.get("unit")
    name = payload.get("name")
    if (not isinstance(unit, str) or not unit.strip() or len(unit) > 80
            or not isinstance(name, str) or not name.strip() or len(name) > 200):
        raise PricingError("pricing_v2_unit_hold")
    price = payload.get("sell_price")
    if type(price) not in (int, float):
        raise PricingError("pricing_v2_price_hold")
    price = Decimal(str(price))
    if not price.is_finite() or price <= 0 or price > Decimal("1000000000"):
        raise PricingError("pricing_v2_price_hold")
    policy = currency_policy_v2(session, actor, product.source_system, current)
    if currency is not None and policy.currency != currency:
        raise PricingError("pricing_v2_currency_mismatch")
    rules = list(session.scalars(select(PricingRule).where(
        PricingRule.organization_id == actor.organization_id, PricingRule.store_id == actor.store_id,
        PricingRule.currency == policy.currency, PricingRule.active.is_(True),
        PricingRule.effective_from <= current,
        (PricingRule.effective_until.is_(None)) | (PricingRule.effective_until > current)
    ).execution_options(populate_existing=True)))
    rules = [r for r in rules if "catalog" in (r.price_source_types or [])]
    if len(rules) != 1:
        raise PricingError("pricing_v2_rule_hold")
    rule = rules[0]
    if (current - observed).total_seconds() > rule.source_max_age_seconds:
        raise PricingError("pricing_v2_snapshot_expired")
    amount = _quantize(price, rule.amount_precision, rule.rounding)
    if amount <= 0:
        raise PricingError("pricing_v2_price_hold")
    expiry = min(policy.effective_until, observed + timedelta(seconds=rule.source_max_age_seconds),
                 rule.effective_until or policy.effective_until)
    if expiry <= current:
        raise PricingError("pricing_v2_snapshot_expired")
    result = dict(version="2", catalog_product_id=product.id, source_system=product.source_system,
        external_code=product.external_code, source_category="catalog", product_name=name, unit=unit,
        currency=policy.currency, list_price=format(amount, "f"), product_version=product.version,
        snapshot_id=snapshot.id, source_version=snapshot.source_version, observed_at=observed.isoformat(),
        captured_at=captured.isoformat(), payload_sha256=snapshot.payload_sha256, policy_id=policy.id,
        policy_version=policy.version, policy_digest=_v2_row_digest(policy), rule_id=rule.id,
        rule_version=rule.version, rule_digest=_v2_row_digest(rule), expires_at=expiry.isoformat())
    result["context_digest"] = _digest({"context": result, "authority": authority})
    return result, rule


def quote_values_v2(context, rule, quantity, discount_bps):
    if discount_bps > rule.max_discount_bps:
        raise PricingError("pricing_discount_exceeded")
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        unit = _quantize(Decimal(context["list_price"]) * Decimal(10000-discount_bps) / Decimal(10000),
                         rule.amount_precision, rule.rounding)
        total = _quantize(unit * Decimal(quantity), rule.amount_precision, rule.rounding)
    if unit <= 0 or total <= 0:
        raise PricingError("pricing_v2_price_hold")
    return format(unit, "f"), format(total, "f")
