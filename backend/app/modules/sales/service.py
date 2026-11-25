"""Sales draft state machine: draft/ready, pending/paid, sync and handoff separate."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.command_guard import lock_command_authority, refresh_authority, check_command_authority
from backend.app.application import commit_guard
from backend.app.modules.sales.models import SalesDraft, SalesDraftLine, SalesHandoff, SalesReceipt
from backend.app.modules.sales.schemas import (
    DraftCreate, DraftLineOut, DraftOut, HandoffCreate, HandoffOut, LineCreate,
    ReceiptCreate,
)

DRAFT = "sales.draft"
READ = "sales.read"
CONFIRM = "sales.confirm"
HANDOFF = "sales.handoff"
MONEY = Decimal("0.01")


class SalesError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise SalesError("sales_time_invalid")
    return current.astimezone(timezone.utc)


def _require(session: Session, actor: AuthenticatedSubject, permission: str, now: datetime) -> None:
    try:
        with session.no_autoflush:
            if permission != READ:
                epoch = lock_command_authority(session, actor)
            else:
                refresh_authority(session)
            authorize_scope(session, actor, organization_id=actor.organization_id,
                            store_id=actor.store_id, permission=permission,
                            now=commit_guard._db_clock(session))
    except HTTPException as exc:
        raise SalesError("sales_permission_denied") from exc
    if permission != READ:
        def final(db, instant):
            try:
                check_command_authority(db, actor, permission=permission, epoch=epoch, now=instant)
            except HTTPException as error:
                raise SalesError("sales_permission_denied") from error
        commit_guard.enlist(session, action=permission, check=final)


def _enlist_quote_use(session, actor, draft_id):
    def final(db, instant):
        draft = _draft(db, actor, draft_id)
        if draft.expires_at is not None and draft.expires_at <= instant:
            raise SalesError("sales_draft_expired")
        validate_draft_quotes(db, actor, draft, instant)
    commit_guard.enlist(session, action="sales.consume_quote", check=final)


def _money(value: Decimal) -> str:
    if not value.is_finite() or value < 0:
        raise SalesError("sales_amount_invalid")
    return format(value.quantize(MONEY, rounding=ROUND_HALF_UP), "f")


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _draft(session: Session, actor: AuthenticatedSubject, draft_id: str,
           *, lock: bool = False) -> SalesDraft:
    scope = (SalesDraft.organization_id == actor.organization_id,
             SalesDraft.store_id == actor.store_id, SalesDraft.id == draft_id)
    if lock:
        # Read a scalar, never trust an already cached ORM object's stage/version.
        version = session.scalar(select(SalesDraft.version).where(*scope))
        if version is None:
            raise SalesError("sales_draft_not_found")
        count = session.execute(update(SalesDraft).where(*scope, SalesDraft.version == version)
            .values(version=version).execution_options(synchronize_session=False)).rowcount
        if count != 1:
            raise SalesError("sales_version_conflict")
    draft = session.scalar(select(SalesDraft).where(*scope).execution_options(populate_existing=True))
    if draft is None:
        raise SalesError("sales_draft_not_found")
    return draft


def _lines(session: Session, draft: SalesDraft) -> list[SalesDraftLine]:
    return list(session.scalars(select(SalesDraftLine).where(
        SalesDraftLine.organization_id == draft.organization_id,
        SalesDraftLine.store_id == draft.store_id,
        SalesDraftLine.draft_id == draft.id,
    ).order_by(SalesDraftLine.created_at, SalesDraftLine.id)))


def _handoffs(session: Session, draft: SalesDraft) -> list[SalesHandoff]:
    return list(session.scalars(select(SalesHandoff).where(
        SalesHandoff.organization_id == draft.organization_id,
        SalesHandoff.store_id == draft.store_id,
        SalesHandoff.draft_id == draft.id,
    ).order_by(SalesHandoff.created_at, SalesHandoff.id)))


def _line_out(line: SalesDraftLine) -> DraftLineOut:
    return DraftLineOut.model_validate(line)


def _handoff_out(handoff: SalesHandoff) -> HandoffOut:
    return HandoffOut.model_validate(handoff)


def draft_out(session: Session, draft: SalesDraft) -> DraftOut:
    return DraftOut(
        id=draft.id, organization_id=draft.organization_id, store_id=draft.store_id,
        creator_id=draft.creator_id, quote_id=draft.quote_id, stage=draft.stage,
        status=draft.status, sync_state=draft.sync_state, version=draft.version,
        currency=draft.currency, total_amount=draft.total_amount, voided_at=draft.voided_at,
        expires_at=draft.expires_at, created_at=draft.created_at, updated_at=draft.updated_at,
        lines=[_line_out(line) for line in _lines(session, draft)],
        handoffs=[_handoff_out(item) for item in _handoffs(session, draft)],
    )


def _audit(session: Session, actor: AuthenticatedSubject, action: str, draft: SalesDraft,
           key: str, now: datetime, details: dict | None = None) -> None:
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action=action, target_type="sales_draft",
                 target_id=draft.id, idempotency_key=f"sales:{draft.id}:{key}",
                 details={"stage": draft.stage, "status": draft.status,
                          "sync_state": draft.sync_state, **(details or {})},
                 occurred_at=now)


def create_draft_with_key(session: Session, actor: AuthenticatedSubject, payload: DraftCreate,
                          *, idempotency_key: str, now: datetime | None = None) -> DraftOut:
    current = _now(now)
    _require(session, actor, DRAFT, current)
    key = (idempotency_key or "").strip()
    if not 1 <= len(key) <= 200:
        raise SalesError("sales_idempotency_key_invalid")
    existing = session.scalar(select(SalesDraft).where(
        SalesDraft.organization_id == actor.organization_id,
        SalesDraft.store_id == actor.store_id,
        SalesDraft.idempotency_key == key,
    ))
    if existing is not None:
        if (existing.creator_id != actor.subject_id
                or existing.currency != payload.currency
                or existing.quote_id != payload.quote_id):
            raise SalesError("sales_idempotency_conflict")
        return draft_out(session, existing)
    if payload.quote_id is not None:
        from backend.app.modules.pricing.models import PricingQuote
        quote = session.scalar(select(PricingQuote).where(
            PricingQuote.organization_id == actor.organization_id,
            PricingQuote.store_id == actor.store_id,
            PricingQuote.id == payload.quote_id,
        ))
        if quote is None or quote.currency != payload.currency or quote.requester_id != actor.subject_id:
            raise SalesError("sales_quote_invalid")
    draft = SalesDraft(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        creator_id=actor.subject_id, idempotency_key=key, quote_id=payload.quote_id,
        stage="draft", status="pending", sync_state="local_only", version=1,
        currency=payload.currency, total_amount="0.00", created_at=current, updated_at=current,
    )
    try:
        with session.begin_nested():
            session.add(draft)
            session.flush()
    except IntegrityError as exc:
        replay = session.scalar(select(SalesDraft).where(
            SalesDraft.organization_id == actor.organization_id,
            SalesDraft.store_id == actor.store_id,
            SalesDraft.idempotency_key == key,
        ))
        if replay is not None and replay.creator_id == actor.subject_id and \
                replay.currency == payload.currency and replay.quote_id == payload.quote_id:
            return draft_out(session, replay)
        raise SalesError("sales_idempotency_conflict") from exc
    _audit(session, actor, "sales.draft_created", draft, "created", current)
    return draft_out(session, draft)


def add_line(session: Session, actor: AuthenticatedSubject, draft_id: str, payload: LineCreate,
             *, now: datetime | None = None) -> DraftOut:
    current = _now(now)
    _require(session, actor, DRAFT, current)
    draft = _draft(session, actor, draft_id, lock=True)
    if draft.stage != "draft" or draft.voided_at is not None:
        raise SalesError("sales_draft_not_editable")
    if any(row.quote_v2_id for row in _lines(session, draft)):
        raise SalesError("sales_v2_legacy_hold")
    existing = session.scalar(select(SalesDraftLine).where(
        SalesDraftLine.organization_id == draft.organization_id,
        SalesDraftLine.store_id == draft.store_id,
        SalesDraftLine.draft_id == draft.id,
        SalesDraftLine.line_key == payload.line_key,
    ))
    if existing is not None:
        if (existing.product_id != payload.product_id or existing.product_name != payload.product_name
                or existing.quantity != format(payload.quantity, "f")
                or existing.unit_price != _money(payload.unit_price)
                or existing.discount_bps != payload.discount_bps or existing.quote_id != payload.quote_id):
            raise SalesError("sales_line_conflict")
        return draft_out(session, draft)
    amount = _money(Decimal(_money(payload.unit_price
                    * (Decimal(10000 - payload.discount_bps) / Decimal(10000)))) * payload.quantity)
    line = SalesDraftLine(
        id=str(uuid4()), organization_id=draft.organization_id, store_id=draft.store_id,
        draft_id=draft.id, quote_id=payload.quote_id, line_key=payload.line_key, product_id=payload.product_id,
        product_name=payload.product_name, quantity=format(payload.quantity, "f"),
        unit_price=_money(payload.unit_price), discount_bps=payload.discount_bps,
        line_amount=amount, created_at=current,
    )
    session.add(line)
    session.flush()
    total = sum((Decimal(item.line_amount) for item in _lines(session, draft)), Decimal("0"))
    draft.total_amount = _money(total)
    draft.version += 1
    draft.updated_at = current
    _audit(session, actor, "sales.line_added", draft, f"line:{payload.line_key}", current,
           {"line_key": payload.line_key})
    return draft_out(session, draft)


def confirm_line(session: Session, actor: AuthenticatedSubject, draft_id: str,
                 line_id: str, *, now: datetime | None = None) -> DraftOut:
    current = _now(now)
    _require(session, actor, CONFIRM, current)
    draft = _draft(session, actor, draft_id, lock=True)
    if draft.stage != "draft" or draft.voided_at is not None:
        raise SalesError("sales_draft_not_editable")
    line = session.scalar(select(SalesDraftLine).where(
        SalesDraftLine.organization_id == draft.organization_id,
        SalesDraftLine.store_id == draft.store_id,
        SalesDraftLine.draft_id == draft.id, SalesDraftLine.id == line_id))
    if line is None:
        raise SalesError("sales_line_not_found")
    if line.confirmed_at is None:
        line.confirmed_by = actor.subject_id
        line.confirmed_at = current
        draft.version += 1
        draft.updated_at = current
        _audit(session, actor, "sales.line_confirmed", draft, f"confirm:{line.id}", current,
               {"line_id": line.id})
    return draft_out(session, draft)


def mark_ready(session: Session, actor: AuthenticatedSubject, draft_id: str, *,
               now: datetime | None = None) -> DraftOut:
    current = _now(now)
    _require(session, actor, CONFIRM, current)
    draft = _draft(session, actor, draft_id, lock=True)
    if draft.stage != "draft" or draft.voided_at is not None:
        raise SalesError("sales_draft_not_ready")
    lines = _lines(session, draft)
    if not lines or any(line.confirmed_at is None for line in lines):
        raise SalesError("sales_lines_unconfirmed")
    if draft.expires_at is not None and draft.expires_at <= current:
        raise SalesError("sales_draft_expired")
    validate_draft_quotes(session, actor, draft, current)
    draft.stage = "ready"
    draft.version += 1
    draft.updated_at = current
    _audit(session, actor, "sales.draft_ready", draft, "ready", current)
    _enlist_quote_use(session, actor, draft.id)
    return draft_out(session, draft)


def create_handoff(session: Session, actor: AuthenticatedSubject, draft_id: str,
                   payload: HandoffCreate, *, idempotency_key: str,
                   now: datetime | None = None) -> HandoffOut:
    current = _now(now)
    _require(session, actor, HANDOFF, current)
    draft = _draft(session, actor, draft_id, lock=True)
    if draft.stage != "ready" or draft.voided_at is not None:
        raise SalesError("sales_draft_not_ready")
    key = (idempotency_key or "").strip()
    if not 1 <= len(key) <= 200:
        raise SalesError("sales_idempotency_key_invalid")
    existing = session.scalar(select(SalesHandoff).where(
        SalesHandoff.organization_id == draft.organization_id,
        SalesHandoff.store_id == draft.store_id,
        SalesHandoff.idempotency_key == key,
    ))
    if existing is not None:
        if existing.draft_id != draft.id or existing.external_ref != payload.external_ref:
            raise SalesError("sales_idempotency_conflict")
        return _handoff_out(existing)
    # One draft may have at most one handoff intent. In particular, a
    # receipt_unknown must not be bypassed with a fresh idempotency key.
    if _handoffs(session, draft):
        raise SalesError("sales_handoff_exists")
    validate_draft_quotes(session, actor, draft, current)
    handoff = SalesHandoff(
        id=str(uuid4()), organization_id=draft.organization_id, store_id=draft.store_id,
        draft_id=draft.id, idempotency_key=key,
        handoff_state="sent" if payload.external_ref else "requested",
        external_ref=payload.external_ref, created_at=current, updated_at=current,
    )
    try:
        with session.begin_nested():
            session.add(handoff)
            session.flush()
    except IntegrityError as exc:
        replay = session.scalar(select(SalesHandoff).where(
            SalesHandoff.organization_id == draft.organization_id,
            SalesHandoff.store_id == draft.store_id,
            SalesHandoff.idempotency_key == key,
        ))
        if replay is not None:
            if replay.draft_id == draft.id and replay.external_ref == payload.external_ref:
                return _handoff_out(replay)
            raise SalesError("sales_idempotency_conflict") from exc
        existing_handoff = session.scalar(select(SalesHandoff).where(
            SalesHandoff.organization_id == draft.organization_id,
            SalesHandoff.store_id == draft.store_id,
            SalesHandoff.draft_id == draft.id,
        ))
        if existing_handoff is not None:
            raise SalesError("sales_handoff_exists") from exc
        raise
    draft.sync_state = "pending"
    draft.version += 1
    draft.updated_at = current
    _audit(session, actor, "sales.handoff_requested", draft, f"handoff:{handoff.id}", current,
           {"handoff_id": handoff.id})
    _enlist_quote_use(session, actor, draft.id)
    return _handoff_out(handoff)


def _response_snapshot(handoff: SalesHandoff, at: datetime) -> dict:
    return {
        "id": handoff.id,
        "handoff_state": handoff.handoff_state,
        "external_ref": handoff.external_ref,
        "created_at": handoff.created_at.isoformat(),
        "updated_at": at.isoformat(),
    }


def _receipt_response(receipt: SalesReceipt) -> HandoffOut:
    response = receipt.response or {}
    return HandoffOut(
        id=response["id"], handoff_state=response["handoff_state"],
        external_ref=response.get("external_ref"),
        created_at=datetime.fromisoformat(response["created_at"]),
        updated_at=datetime.fromisoformat(response["updated_at"]),
    )


def record_receipt(session: Session, actor: AuthenticatedSubject, handoff_id: str,
                   payload: ReceiptCreate, *, idempotency_key: str,
                   receipt_source: str = "manual",
                   now: datetime | None = None) -> HandoffOut:
    current = _now(now)
    _require(session, actor, HANDOFF, current)
    if payload.paid and receipt_source != "pos":
        # A generic receipt/web request may not settle a sale directly; only the
        # POS reconciliation path (verified receipt match) may set paid.
        raise SalesError("sales_receipt_not_verified")
    handoff = session.scalar(select(SalesHandoff).where(
        SalesHandoff.organization_id == actor.organization_id,
        SalesHandoff.store_id == actor.store_id,
        SalesHandoff.id == handoff_id,
    ))
    if handoff is None:
        raise SalesError("sales_handoff_not_found")
    draft = _draft(session, actor, handoff.draft_id, lock=True)
    session.refresh(handoff, with_for_update=True)
    # Amount-only/manual receipts cannot settle or alter a managed ERP-004
    # command. Its full line proof lives in a separate synthetic/formal gate.
    from backend.app.modules.erp_sales.erp_004_source import reject_bound_draft
    reject_bound_draft(session, actor, draft.id)
    key = (idempotency_key or "").strip()
    if not 1 <= len(key) <= 200:
        raise SalesError("sales_idempotency_key_invalid")
    digest = hashlib.sha256(_canonical({
        "handoff_id": handoff.id, "external_ref": payload.external_ref,
        "paid": payload.paid, "unknown": payload.unknown,
    }).encode("utf-8")).hexdigest()
    replay = session.scalar(select(SalesReceipt).where(
        SalesReceipt.organization_id == actor.organization_id,
        SalesReceipt.store_id == actor.store_id,
        SalesReceipt.receipt_key == key,
    ))
    if replay is not None:
        if replay.handoff_id != handoff.id or replay.digest != digest:
            raise SalesError("sales_idempotency_conflict")
        return _receipt_response(replay)
    if handoff.handoff_state in ("confirmed", "failed"):
        raise SalesError("sales_receipt_conflict")
    receipt = SalesReceipt(
        id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        handoff_id=handoff.id, receipt_key=key, digest=digest,
        external_ref=payload.external_ref, paid=payload.paid, unknown=payload.unknown,
        state_after=("receipt_unknown" if payload.unknown
                     else ("confirmed" if payload.paid else "sent")),
        response={}, created_at=current,
    )
    try:
        with session.begin_nested():
            session.add(receipt)
            session.flush()
    except IntegrityError as exc:
        replay_receipt = session.scalar(select(SalesReceipt).where(
            SalesReceipt.organization_id == actor.organization_id,
            SalesReceipt.store_id == actor.store_id,
            SalesReceipt.receipt_key == key,
        ))
        if replay_receipt is not None and replay_receipt.handoff_id == handoff.id \
                and replay_receipt.digest == digest:
            return _receipt_response(replay_receipt)
        raise SalesError("sales_idempotency_conflict") from exc
    if payload.unknown:
        handoff.handoff_state = "receipt_unknown"
        handoff.updated_at = current
        draft.sync_state = "conflict"
        draft.version += 1
        draft.updated_at = current
        receipt.response = _response_snapshot(handoff, current)
        session.flush()
        _audit(session, actor, "sales.receipt_unknown", draft,
               f"receipt-unknown:{digest}", current,
               {"handoff_id": handoff.id, "receipt_key": key})
        return _handoff_out(handoff)
    handoff.external_ref = payload.external_ref
    handoff.updated_at = current
    if payload.paid:
        handoff.handoff_state = "confirmed"
        draft.sync_state = "synced"
        draft.status = "paid"
    else:
        # An unpaid/failed receipt is not terminal: a later paid receipt must
        # still be able to settle the same handoff.
        handoff.handoff_state = "sent"
        draft.sync_state = "pending"
    receipt.response = _response_snapshot(handoff, current)
    draft.version += 1
    draft.updated_at = current
    _audit(session, actor, "sales.receipt_confirmed", draft, f"receipt:{digest}", current,
           {"handoff_id": handoff.id, "paid": payload.paid, "receipt_key": key})
    return _handoff_out(handoff)


def void_draft(session: Session, actor: AuthenticatedSubject, draft_id: str, *,
               now: datetime | None = None) -> DraftOut:
    current = _now(now)
    _require(session, actor, DRAFT, current)
    draft = _draft(session, actor, draft_id, lock=True)
    if draft.stage != "draft" or draft.voided_at is not None:
        raise SalesError("sales_draft_not_voidable")
    draft.version += 1
    draft.voided_at = current
    draft.updated_at = current
    _audit(session, actor, "sales.draft_voided", draft, "voided", current)
    return draft_out(session, draft)


def get_draft(session: Session, actor: AuthenticatedSubject, draft_id: str, *,
              now: datetime | None = None) -> DraftOut:
    current = _now(now)
    _require(session, actor, READ, current)
    return draft_out(session, _draft(session, actor, draft_id))


def list_drafts(session: Session, actor: AuthenticatedSubject, *,
                now: datetime | None = None) -> list[DraftOut]:
    current = _now(now)
    _require(session, actor, READ, current)
    rows = session.scalars(select(SalesDraft).where(
        SalesDraft.organization_id == actor.organization_id,
        SalesDraft.store_id == actor.store_id,
    ).order_by(SalesDraft.created_at.desc(), SalesDraft.id).limit(100))
    return [draft_out(session, draft) for draft in rows]


def quote_object_id(draft_id: str, line_key: str) -> str:
    return hashlib.sha256(_canonical([draft_id, line_key]).encode()).hexdigest()


def quote_line(session, actor, draft_id, line_key, payload, *, now=None):
    from backend.app.modules.pricing import service as pricing
    from backend.app.modules.pricing.schemas import QuoteRequest, QuoteLine
    current = _now(now)
    _require(session, actor, DRAFT, current)
    draft = _draft(session, actor, draft_id, lock=True)
    if draft.stage != "draft" or draft.voided_at is not None:
        raise SalesError("sales_draft_not_editable")
    if not 1 <= len(line_key) <= 200:
        raise SalesError("sales_quote_invalid")
    if any(row.quote_v2_id for row in _lines(session, draft)):
        raise SalesError("sales_v2_legacy_hold")
    try:
        quote = pricing.create_quote(session, actor, QuoteRequest(
            quote_key=payload.quote_key, rule_key=payload.rule_key,
            line=QuoteLine(object_type="sales_draft_line", object_id=quote_object_id(draft.id, line_key),
                object_version="1", product_id=payload.product_id, quantity=payload.quantity),
            discount_bps=payload.discount_bps, price_source=payload.price_source), now=current)
    except pricing.PricingError as error:
        raise SalesError(error.code) from error
    # A saved unquoted line can acquire its exact quote without replacing the line.
    line = session.scalar(select(SalesDraftLine).where(
        SalesDraftLine.organization_id == actor.organization_id,
        SalesDraftLine.store_id == actor.store_id, SalesDraftLine.draft_id == draft.id,
        SalesDraftLine.line_key == line_key))
    if line is not None and line.quote_v2_id:
        raise SalesError("sales_v2_legacy_hold")
    if line is not None:
        if (line.product_id != quote.product_id or Decimal(line.quantity) != Decimal(quote.quantity) or
                Decimal(line.unit_price) != Decimal(quote.list_price) or line.discount_bps != quote.discount_bps or
                Decimal(line.line_amount) != Decimal(quote.final_amount) or draft.currency != quote.currency or
                quote.amount_precision != 2 or quote.rounding != "ROUND_HALF_UP"):
            raise SalesError("sales_quote_invalid")
        if line.quote_id != quote.id:
            line.quote_id = quote.id
            draft.version += 1
            draft.updated_at = current
            _audit(session, actor, "sales.line_quoted", draft, f"quote:{line.id}:{quote.id}", current,
                   {"line_id": line.id, "quote_id": quote.id})
    quote_id = quote.id
    def final(db, instant):
        try:
            authorize_scope(db, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                            permission="pricing.quote", now=instant)
            row = pricing._owned_quote(db, actor, quote_id)
            db.refresh(row)
            pricing._validate_quote_current(db, actor, row, instant)
        except pricing.PricingError as error:
            raise SalesError(error.code) from error
    commit_guard.enlist(session, action="sales.quote_line", check=final)
    return quote


def validate_draft_quotes(session, actor, draft, current):
    from backend.app.modules.pricing import service as pricing
    lines = _lines(session, draft)
    if not lines:
        raise SalesError("sales_quote_invalid")
    total = Decimal("0")
    for line in lines:
        if line.quote_v2_id:
            _validate_v2_line(session, actor, draft, line, current)
            total += Decimal(line.line_amount)
            continue
        if not line.quote_id:
            raise SalesError("sales_quote_required")
        try:
            quote = pricing.validate_quote_for_execution(session, actor, line.quote_id, now=current)
        except pricing.PricingError as error:
            raise SalesError(error.code) from error
        if (quote.object_type != "sales_draft_line" or
                quote.object_id != quote_object_id(draft.id, line.line_key) or quote.object_version != "1" or
                quote.product_id != line.product_id or Decimal(quote.quantity) != Decimal(line.quantity) or
                quote.currency != draft.currency or quote.amount_precision != 2 or quote.rounding != "ROUND_HALF_UP" or
                Decimal(quote.list_price) != Decimal(line.unit_price) or quote.discount_bps != line.discount_bps or
                Decimal(quote.final_amount) != Decimal(line.line_amount)):
            raise SalesError("sales_quote_invalid")
        total += Decimal(line.line_amount)
    if total != Decimal(draft.total_amount) or (draft.quote_id and
            (len(lines) != 1 or draft.quote_id != lines[0].quote_id)):
        raise SalesError("sales_quote_invalid")


# Separate v2 services. The legacy DTOs, quote algorithm and receipts remain intact.
def _v2_enabled():
    import os
    return os.environ.get("SALES_QUOTE_V2_ENABLED") == "1"


def _v2_authority(session, actor):
    from backend.app.modules.identity.models import ScopeSecurityFence, Subject
    if not actor.account_id or not actor.session_id or type(actor.account_version) is not int:
        raise SalesError("sales_permission_denied")
    kind = session.scalar(select(Subject.subject_type).where(Subject.id == actor.subject_id,
        Subject.organization_id == actor.organization_id))
    if kind != "person":
        raise SalesError("sales_permission_denied")
    epoch = session.scalar(select(ScopeSecurityFence.version).where(
        ScopeSecurityFence.organization_id == actor.organization_id,
        ScopeSecurityFence.store_id == actor.store_id))
    # The existing security-fence protocol provisions exactly epoch 1 at first write.
    return dict(organization_id=actor.organization_id, store_id=actor.store_id, subject_id=actor.subject_id,
                account_id=actor.account_id, session_id=actor.session_id,
                account_version=actor.account_version, scope_epoch=epoch if epoch is not None else 1)


def _v2_require(session, actor, *, writing, now):
    _require(session, actor, DRAFT if writing else READ, now)
    _require(session, actor, "pricing.quote", now) if writing else _v2_read_pricing(session, actor, now)
    return _v2_authority(session, actor)


def _v2_read_pricing(session, actor, now):
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id, store_id=actor.store_id,
                        permission="pricing.quote", now=now)
    except HTTPException as error:
        raise SalesError("sales_permission_denied") from error


def _v2_resolve(session, actor, product_id, authority, now, currency=None):
    from backend.app.modules.pricing import service as pricing
    try:
        return pricing.resolve_context_v2(session, actor, product_id, authority, currency=currency, now=now)
    except pricing.PricingError as error:
        raise SalesError(error.code) from error


def _v2_owned_draft(session, actor, draft_id, *, lock=False):
    draft = _draft(session, actor, draft_id, lock=lock)
    if draft.creator_id != actor.subject_id:
        raise SalesError("sales_draft_not_found")
    return draft


def _v2_editable(session, draft, now):
    if not _v2_enabled():
        raise SalesError("sales_v2_disabled")
    if draft.stage != "draft" or draft.voided_at is not None or (draft.expires_at and draft.expires_at <= now):
        raise SalesError("sales_draft_not_editable")
    if draft.quote_id or any(not row.quote_v2_id for row in _lines(session, draft)):
        raise SalesError("sales_v2_legacy_hold")


def quote_context_v2(session, actor, catalog_product_id, *, now=None):
    current = _now(now)
    authority = _v2_require(session, actor, writing=False, now=current)
    if not _v2_enabled():
        raise SalesError("sales_v2_disabled")
    return _v2_resolve(session, actor, catalog_product_id, authority, current)[0]


def _v2_quote(session, actor, draft_id, quote_key, line_key):
    from backend.app.modules.pricing.models import PricingQuoteV2
    return session.scalar(select(PricingQuoteV2).where(
        PricingQuoteV2.organization_id == actor.organization_id, PricingQuoteV2.store_id == actor.store_id,
        PricingQuoteV2.draft_id == draft_id, PricingQuoteV2.requester_id == actor.subject_id,
        PricingQuoteV2.quote_key == quote_key, PricingQuoteV2.line_key == line_key
    ).execution_options(populate_existing=True))


def _v2_validate_quote(session, actor, row, draft, current):
    from backend.app.modules.pricing import service as pricing
    if row is None or row.requester_id != actor.subject_id or row.draft_id != draft.id:
        raise SalesError("sales_v2_quote_unknown")
    if not _v2_enabled():
        raise SalesError("sales_v2_disabled")
    if row.expires_at <= current:
        raise SalesError("sales_v2_quote_expired")
    authority = _v2_authority(session, actor)
    if row.authority != authority:
        raise SalesError("sales_v2_authority_changed")
    context, rule = _v2_resolve(session, actor, row.context["catalog_product_id"], authority, current, draft.currency)
    if context != row.context:
        raise SalesError("sales_v2_context_changed")
    original = dict(row.response)
    digest = original.pop("snapshot_digest", None)
    if digest != row.snapshot_digest or pricing._digest(original) != row.snapshot_digest:
        raise SalesError("sales_v2_quote_corrupt")
    if row.response["status"] != "quoted":
        raise SalesError("sales_v2_approval_required")
    return row


def _v2_eligibility(session, actor, row, draft, current):
    try:
        if draft.stage != "draft" or draft.voided_at is not None or (draft.expires_at and draft.expires_at <= current):
            raise SalesError("sales_draft_not_editable")
        _v2_validate_quote(session, actor, row, draft, current)
    except SalesError as error:
        return {"usable": False, "code": error.code}
    return {"usable": True, "code": "OK"}


def _v2_quote_result(session, actor, row, draft, current):
    from backend.app.modules.sales.schemas import QuoteLookupV2
    return QuoteLookupV2(outcome="recorded" if row else "unknown",
        original_result=row.response if row else None,
        current_eligibility=_v2_eligibility(session, actor, row, draft, current))


def get_line_quote_v2(session, actor, draft_id, quote_key, line_key, *, now=None):
    current = _now(now)
    _v2_require(session, actor, writing=False, now=current)
    draft = _v2_owned_draft(session, actor, draft_id)
    return _v2_quote_result(session, actor, _v2_quote(session, actor, draft.id, quote_key, line_key), draft, current)


def _v2_enlist(session, actor, row_id, draft_id):
    def final(db, instant):
        from backend.app.modules.pricing.models import PricingQuoteV2
        _v2_read_pricing(db, actor, instant)
        row = db.get(PricingQuoteV2, row_id, populate_existing=True)
        draft = _v2_owned_draft(db, actor, draft_id)
        if draft.voided_at is not None or (draft.expires_at and draft.expires_at <= instant):
            raise SalesError("sales_draft_not_editable")
        _v2_validate_quote(db, actor, row, draft, instant)
    commit_guard.enlist(session, action="sales.quote_v2", check=final)


def quote_line_v2(session, actor, draft_id, payload, *, idempotency_key, now=None):
    from backend.app.modules.pricing import service as pricing
    from backend.app.modules.pricing.models import PricingQuoteV2
    from backend.app.modules.sales.schemas import LineQuoteRequestV2
    from datetime import timedelta
    payload = LineQuoteRequestV2.model_validate(payload)
    current = _now(now)
    authority = _v2_require(session, actor, writing=True, now=current)
    draft = _v2_owned_draft(session, actor, draft_id, lock=True)
    if idempotency_key != payload.quote_key:
        raise SalesError("sales_v2_key_mismatch")
    request = {"draft_id": draft.id, "requester_id": actor.subject_id, **payload.model_dump()}
    digest = pricing._digest(request)
    existing = session.scalar(select(PricingQuoteV2).where(
        PricingQuoteV2.organization_id == actor.organization_id, PricingQuoteV2.store_id == actor.store_id,
        PricingQuoteV2.quote_key == payload.quote_key))
    if existing is not None:
        if existing.requester_id != actor.subject_id:
            raise SalesError("sales_v2_key_conflict")
        if existing.request_digest != digest:
            raise SalesError("sales_v2_key_conflict")
        return _v2_quote_result(session, actor, existing, draft, current)
    _v2_editable(session, draft, current)
    # A line key already bound must never be reassigned by a fresh quote.
    if any(line.line_key == payload.line_key for line in _lines(session, draft)):
        raise SalesError("sales_v2_line_exists")
    context, rule = _v2_resolve(session, actor, payload.catalog_product_id, authority, current, draft.currency)
    if context["context_digest"] != payload.context_digest:
        raise SalesError("sales_v2_context_changed")
    try:
        unit_price, total = pricing.quote_values_v2(context, rule, payload.quantity, payload.discount_bps)
    except pricing.PricingError as error:
        raise SalesError(error.code) from error
    # v1 approval facts are not implicitly usable by v2; no new approval permission is invented.
    if payload.discount_bps > rule.approval_threshold_bps:
        raise SalesError("sales_v2_approval_required")
    expiry = min(datetime.fromisoformat(context["expires_at"]), current + timedelta(seconds=rule.quote_ttl_seconds))
    identity = str(uuid4())
    response = dict(version="2", id=identity, draft_id=draft.id, quote_key=payload.quote_key,
        line_key=payload.line_key, catalog_product_id=payload.catalog_product_id, context=context,
        quantity=payload.quantity, discount_bps=payload.discount_bps, unit_price=unit_price,
        final_amount=total, status="quoted", created_at=current.isoformat(), expires_at=expiry.isoformat())
    snapshot_digest = pricing._digest(response)
    response["snapshot_digest"] = snapshot_digest
    row = PricingQuoteV2(id=identity, organization_id=actor.organization_id, store_id=actor.store_id,
        draft_id=draft.id, requester_id=actor.subject_id, quote_key=payload.quote_key, line_key=payload.line_key,
        request_digest=digest, snapshot_digest=snapshot_digest, request=request, authority=authority,
        context=context, response=response, created_at=current, expires_at=expiry)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as error:
        raise SalesError("sales_v2_key_conflict") from error
    _audit(session, actor, "sales.line_quoted_v2", draft, "quote-v2:"+payload.quote_key, current,
           {"quote_id": row.id, "line_key": row.line_key, "snapshot_digest": snapshot_digest})
    _v2_enlist(session, actor, row.id, draft.id)
    return _v2_quote_result(session, actor, row, draft, current)


def _v2_line_lookup(session, actor, draft, receipt, current):
    from backend.app.modules.sales.schemas import LineLookupV2
    from backend.app.modules.pricing.models import PricingQuoteV2
    quote = session.get(PricingQuoteV2, receipt.quote_id) if receipt else None
    return LineLookupV2(outcome="recorded" if receipt else "unknown",
        original_result=receipt.response if receipt else None,
        current_eligibility=_v2_eligibility(session, actor, quote, draft, current))


def _v2_line_receipt(session, actor, draft_id, line_key):
    from backend.app.modules.sales.models import SalesLineCommandV2
    return session.scalar(select(SalesLineCommandV2).where(
        SalesLineCommandV2.organization_id == actor.organization_id, SalesLineCommandV2.store_id == actor.store_id,
        SalesLineCommandV2.draft_id == draft_id, SalesLineCommandV2.line_key == line_key))


def get_line_command_v2(session, actor, draft_id, line_key, *, now=None):
    current = _now(now)
    _v2_require(session, actor, writing=False, now=current)
    draft = _v2_owned_draft(session, actor, draft_id)
    receipt = _v2_line_receipt(session, actor, draft.id, line_key)
    if receipt is not None and receipt.actor_id != actor.subject_id:
        raise SalesError("sales_v2_key_conflict")
    return _v2_line_lookup(session, actor, draft, receipt, current)


def bind_line_v2(session, actor, draft_id, payload, *, idempotency_key, now=None):
    from backend.app.modules.pricing import service as pricing
    from backend.app.modules.sales.models import SalesLineCommandV2
    from backend.app.modules.sales.schemas import LineBindRequestV2
    payload = LineBindRequestV2.model_validate(payload)
    current = _now(now)
    _v2_require(session, actor, writing=True, now=current)
    draft = _v2_owned_draft(session, actor, draft_id, lock=True)
    if idempotency_key != payload.line_key:
        raise SalesError("sales_v2_key_mismatch")
    digest = pricing._digest({"draft_id": draft.id, "actor_id": actor.subject_id, **payload.model_dump()})
    existing = _v2_line_receipt(session, actor, draft.id, payload.line_key)
    if existing is not None:
        if existing.actor_id != actor.subject_id or existing.request_digest != digest:
            raise SalesError("sales_v2_key_conflict")
        return _v2_line_lookup(session, actor, draft, existing, current)
    _v2_editable(session, draft, current)
    quote = _v2_quote(session, actor, draft.id, payload.quote_key, payload.line_key)
    _v2_validate_quote(session, actor, quote, draft, current)
    if quote.snapshot_digest != payload.quote_digest:
        raise SalesError("sales_v2_key_conflict")
    if any(line.line_key == payload.line_key for line in _lines(session, draft)):
        raise SalesError("sales_v2_line_exists")
    original, context = quote.response, quote.context
    line = SalesDraftLine(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        draft_id=draft.id, quote_id=None, quote_v2_id=quote.id, line_key=payload.line_key,
        product_id=context["catalog_product_id"], catalog_product_id=context["catalog_product_id"],
        unit=context["unit"], product_name=context["product_name"], quantity=original["quantity"],
        unit_price=context["list_price"], discount_bps=original["discount_bps"],
        line_amount=original["final_amount"], created_at=current)
    session.add(line)
    session.flush()
    response = dict(version="2", id=line.id, draft_id=draft.id, line_key=line.line_key,
        quote_key=quote.quote_key, quote_id=quote.id, catalog_product_id=line.catalog_product_id,
        unit=line.unit, currency=draft.currency, quantity=line.quantity, unit_price=original["unit_price"],
        line_amount=line.line_amount, product_name=line.product_name, stage="draft")
    receipt = SalesLineCommandV2(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
        draft_id=draft.id, actor_id=actor.subject_id, line_key=line.line_key, quote_id=quote.id,
        line_id=line.id, request_digest=digest, response=response, created_at=current)
    session.add(receipt)
    session.flush()
    draft.total_amount = format(sum((Decimal(x.line_amount) for x in _lines(session, draft)), Decimal(0)), "f")
    draft.version += 1
    draft.updated_at = current
    _audit(session, actor, "sales.line_bound_v2", draft, "line-v2:"+line.line_key, current,
           {"line_id": line.id, "quote_id": quote.id, "request_digest": digest})
    _v2_enlist(session, actor, quote.id, draft.id)
    return _v2_line_lookup(session, actor, draft, receipt, current)


def _validate_v2_line(session, actor, draft, line, current):
    from backend.app.modules.pricing.models import PricingQuoteV2
    row = session.get(PricingQuoteV2, line.quote_v2_id)
    _v2_validate_quote(session, actor, row, draft, current)
    original, context = row.response, row.context
    if (line.quote_id is not None or row.line_key != line.line_key
            or line.product_id != context["catalog_product_id"] or line.catalog_product_id != line.product_id
            or line.unit != context["unit"] or line.product_name != context["product_name"]
            or line.quantity != original["quantity"] or line.unit_price != context["list_price"]
            or line.discount_bps != original["discount_bps"] or line.line_amount != original["final_amount"]):
        raise SalesError("sales_quote_invalid")
