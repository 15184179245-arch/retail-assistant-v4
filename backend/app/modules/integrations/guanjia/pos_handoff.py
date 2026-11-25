"""Synthetic POS handoff/receipt boundary for SALE-004.

Delivery and settlement are separate facts: a successful delivery does not mark
a draft paid, and an unmatched/ambiguous/partial receipt stays unknown for human
reconciliation. No real GuanJia/plugin credentials or network calls exist here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from fastapi import HTTPException
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from contextlib import contextmanager

from backend.app.application import commit_guard
from backend.app.modules.identity.command_guard import refresh_authority

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.sales import service as sales_service
from backend.app.modules.sales.models import SalesDraft, SalesHandoff, PosDispatch
from backend.app.modules.sales.schemas import ReceiptCreate


class PosError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PosDeliveryFailed(PosError):
    """The outcome is unknown; durable intent and audit prohibit automatic resend."""


@dataclass(frozen=True)
class DeliveryOutcome:
    handoff: SalesHandoff
    created: bool


@dataclass(frozen=True)
class PosReceipt:
    delivery_ref: str
    store_id: str
    draft_id: str
    external_ref: str
    amount: str
    currency: str
    paid: bool
    received_at: datetime

    @property
    def digest(self) -> str:
        payload = {
            "delivery_ref": self.delivery_ref, "store_id": self.store_id,
            "draft_id": self.draft_id, "external_ref": self.external_ref,
            "amount": self.amount, "currency": self.currency,
            "paid": self.paid, "received_at": self.received_at.isoformat(),
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()


class PosGateway(Protocol):
    def deliver(self, *, organization_id: str, command_key: str, store_id: str, draft_id: str, amount: str,
                currency: str) -> str: ...
    def lookup_delivery(self, *, organization_id: str, store_id: str, command_key: str) -> str | None: ...
    def read_receipts(self, *, delivery_ref: str) -> list[PosReceipt]: ...


class SyntheticPosGateway:
    """Deterministic in-memory gateway used only by synthetic tests."""

    def __init__(self) -> None:
        self.deliveries: dict[tuple[str, str, str], str] = {}
        self.receipts: dict[str, list[PosReceipt]] = {}
        self.deliver_calls = 0
        self.fail_delivery = False
        self.fail_receipts = False

    def deliver(self, *, organization_id: str, command_key: str, store_id: str, draft_id: str, amount: str,
                currency: str) -> str:
        self.deliver_calls += 1
        if self.fail_delivery:
            raise PosError("pos_delivery_unavailable")
        key = (organization_id, store_id, command_key)
        if key not in self.deliveries:
            self.deliveries[key] = f"pos-{store_id}-{draft_id}"
        return self.deliveries[key]

    def lookup_delivery(self, *, organization_id: str, store_id: str, command_key: str) -> str | None:
        return self.deliveries.get((organization_id, store_id, command_key))

    def read_receipts(self, *, delivery_ref: str) -> list[PosReceipt]:
        if self.fail_receipts:
            raise PosError("pos_receipt_query_unavailable")
        return list(self.receipts.get(delivery_ref, []))


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise PosError("pos_time_invalid")
    return current.astimezone(timezone.utc)


def _handoff(session: Session, actor: AuthenticatedSubject, handoff_id: str,
             *, lock: bool = False) -> SalesHandoff:
    statement = select(SalesHandoff).where(
        SalesHandoff.organization_id == actor.organization_id,
        SalesHandoff.store_id == actor.store_id,
        SalesHandoff.id == handoff_id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    handoff = session.scalar(statement)
    if handoff is None:
        raise PosError("pos_handoff_not_found")
    return handoff


def _draft(session: Session, handoff: SalesHandoff) -> SalesDraft:
    draft = session.get(SalesDraft, handoff.draft_id)
    if draft is None:
        raise PosError("pos_draft_not_found")
    return draft


def _audit(session: Session, actor: AuthenticatedSubject, action: str,
           handoff: SalesHandoff, key: str, now: datetime, details: dict | None = None) -> None:
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action=action, target_type="sales_handoff",
                 target_id=handoff.id, idempotency_key=f"pos:{handoff.id}:{key}",
                 details={"draft_id": handoff.draft_id,
                          "delivery_ref": handoff.delivery_ref,
                          "state": handoff.handoff_state, **(details or {})},
                 occurred_at=now)


def _locked_pair(session, actor, handoff_id):
    handoff = _handoff(session, actor, handoff_id)
    draft = sales_service._draft(session, actor, handoff.draft_id, lock=True)
    session.refresh(handoff, with_for_update=True)
    return draft, handoff


@dataclass(frozen=True)
class HandoffSnapshot:
    id: str
    organization_id: str
    store_id: str
    draft_id: str
    handoff_state: str
    delivery_ref: str | None
    external_ref: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class _Binding:
    handoff: HandoffSnapshot
    draft_version: int
    amount: str
    currency: str
    attempt_digest: str | None
    attempt_state: str | None
    attempt_facts: tuple | None


def _snapshot(row):
    return HandoffSnapshot(**{name: getattr(row, name) for name in HandoffSnapshot.__dataclass_fields__})


def _attempt(session, handoff):
    return session.scalar(select(PosDispatch).where(PosDispatch.organization_id == handoff.organization_id,
        PosDispatch.store_id == handoff.store_id, PosDispatch.id == handoff.id)
        .execution_options(populate_existing=True))


def _binding(draft, handoff, attempt):
    facts = None
    if attempt is not None:
        values = dict(organization_id=attempt.organization_id, store_id=attempt.store_id,
            command_key=attempt.id, draft_id=attempt.draft_id, amount=attempt.amount, currency=attempt.currency)
        if (attempt.id != handoff.id or attempt.draft_id != draft.id
                or (attempt.organization_id, attempt.store_id) != (draft.organization_id, draft.store_id)
                or attempt.amount != draft.total_amount or attempt.currency != draft.currency
                or hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest() != attempt.request_digest):
            raise PosError("pos_binding_changed")
        facts = (attempt.id, attempt.draft_id, attempt.draft_version, attempt.actor_id,
                 attempt.amount, attempt.currency, attempt.request_digest)
    return _Binding(_snapshot(handoff), draft.version, draft.total_amount, draft.currency,
                    attempt.request_digest if attempt else None, attempt.state if attempt else None, facts)


def _single_engine(session):
    # get_bind() without a mapper describes only the default. SQLAlchemy has
    # no public binding-map enumerator; on the pinned version fail shut unless
    # its actual map is empty and binding resolution is the standard method.
    if (not isinstance(session, Session) or type(session).get_bind is not Session.get_bind
            or getattr(session, "_Session__binds", None) != {}):
        return None
    return session.bind if isinstance(session.bind, Engine) else None


def _factory_from_session(session):
    # An apparently clean Session may have flushed work or join an external
    # Connection transaction. Do not commit, roll back, flush, or close either.
    if (not isinstance(session, Session) or session.in_transaction() or session.new
            or session.dirty or session.deleted or _single_engine(session) is None):
        raise PosError("pos_transaction_not_clean")
    return sessionmaker(session.bind, autoflush=False, expire_on_commit=False)


@contextmanager
def _owned_session(factory):
    if not isinstance(factory, sessionmaker):
        raise PosError("pos_owned_factory_required")
    if factory.kw.get("binds") or not isinstance(factory.kw.get("bind"), Engine):
        # Reject mixed/Connection-backed configuration before constructing a
        # Session that might join or close somebody else's transaction.
        raise PosError("pos_session_not_owned")
    session = factory()
    if (not isinstance(session, Session) or session.in_transaction() or session.new
            or session.dirty or session.deleted or session.identity_map
            or _single_engine(session) is None):
        # A rejected factory may have returned caller work. Never close it.
        raise PosError("pos_session_not_owned")
    try:
        yield session
    finally:
        session.close()


def _require(session, actor, now):
    try:
        sales_service._require(session, actor, sales_service.HANDOFF, now)
    except sales_service.SalesError as error:
        raise PosError("pos_permission_denied") from error


def _read_pair(session, actor, handoff_id):
    with session.no_autoflush:
        refresh_authority(session)
        try:
            authorize_scope(session, actor, organization_id=actor.organization_id,
                store_id=actor.store_id, permission=sales_service.HANDOFF,
                now=commit_guard._db_clock(session))
        except HTTPException as error:
            raise PosError("pos_permission_denied") from error
        handoff = _handoff(session, actor, handoff_id)
        draft = _draft(session, handoff)
        from backend.app.modules.erp_sales.erp_004_source import reject_bound_draft
        reject_bound_draft(session, actor, draft.id)
        return draft, handoff, _attempt(session, handoff)


def _apply_pair(session, actor, expected):
    current = commit_guard._db_clock(session)
    _require(session, actor, current)
    draft, handoff = _locked_pair(session, actor, expected.handoff.id)
    attempt = _attempt(session, handoff)
    from backend.app.modules.erp_sales.erp_004_source import reject_bound_draft
    reject_bound_draft(session, actor, draft.id)
    if _binding(draft, handoff, attempt) != expected:
        raise PosError("pos_binding_changed")
    return draft, handoff, attempt, commit_guard._db_clock(session)


def _prepare_delivery(factory, actor, handoff_id):
    with _owned_session(factory) as session:
        draft, handoff, attempt = _read_pair(session, actor, handoff_id)
        initial = _binding(draft, handoff, attempt)
        if handoff.delivery_ref or sales_service._receipt_terminal(draft, handoff):
            return initial, "known"
        if handoff.external_ref:
            raise PosError("pos_legacy_delivery_requires_manual")
        if attempt is not None:
            return initial, "lookup"
        if handoff.handoff_state == "receipt_unknown":
            return initial, "known"
        _require(session, actor, commit_guard._db_clock(session))
        draft, handoff = _locked_pair(session, actor, handoff_id)
        attempt = _attempt(session, handoff)
        # The first read located the object only. Another authorized local
        # receipt can have made it unknown before this command got its fence.
        if sales_service._receipt_terminal(draft, handoff):
            return _binding(draft, handoff, attempt), "known"
        if handoff.external_ref:
            raise PosError("pos_legacy_delivery_requires_manual")
        if attempt is not None or handoff.delivery_ref:
            return _binding(draft, handoff, attempt), "lookup" if not handoff.delivery_ref else "known"
        if handoff.handoff_state == "receipt_unknown":
            return _binding(draft, handoff, attempt), "known"
        now = commit_guard._db_clock(session)
        if (draft.stage != "ready" or draft.voided_at is not None or draft.status != "pending"
                or (draft.expires_at is not None and draft.expires_at <= now)):
            raise PosError("pos_draft_not_ready")
        sales_service.validate_draft_quotes(session, actor, draft, now)
        values = dict(organization_id=actor.organization_id, command_key=handoff.id, store_id=actor.store_id,
                      draft_id=draft.id, amount=draft.total_amount, currency=draft.currency)
        digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        attempt = PosDispatch(id=handoff.id, organization_id=actor.organization_id, store_id=actor.store_id,
            draft_id=draft.id, draft_version=draft.version, actor_id=actor.subject_id,
            amount=draft.total_amount, currency=draft.currency, request_digest=digest,
            state="dispatching", created_at=now, updated_at=now)
        session.add(attempt)
        handoff.handoff_state, handoff.updated_at = "receipt_unknown", now
        draft.sync_state, draft.updated_at = "conflict", now
        draft.version += 1
        _audit(session, actor, "sales.pos_dispatch_started", handoff, "dispatch-started", now,
               {"command_key": handoff.id, "request_digest": digest})
        sales_service._enlist_quote_use(session, actor, draft.id)
        expected = _binding(draft, handoff, attempt)
        session.commit()  # the sole durable authorization to issue this delivery
        return expected, "deliver"


def _record_delivery(factory, actor, expected, ref, *, failed=False, recovered=False):
    with _owned_session(factory) as session:
        draft, handoff, attempt, now = _apply_pair(session, actor, expected)
        if attempt is None:
            raise PosError("pos_delivery_missing")
        if failed:
            attempt.state, attempt.updated_at = "unknown", now
            _audit(session, actor, "sales.pos_delivery_unknown", handoff, "delivery-unknown", now)
        else:
            attempt.state, attempt.delivery_ref, attempt.updated_at = "delivered", ref, now
            handoff.delivery_ref, handoff.handoff_state, handoff.updated_at = ref, "sent", now
            draft.sync_state, draft.updated_at = "pending", now
            draft.version += 1
            _audit(session, actor, "sales.pos_delivery_recovered" if recovered else "sales.pos_delivered",
                handoff, "recovered" if recovered else "delivered", now)
        result = _snapshot(handoff)
        session.commit()
        return result


def deliver_handoff_owned(session_factory, actor, handoff_id, gateway, *, with_outcome=False):
    expected, mode = _prepare_delivery(session_factory, actor, handoff_id)
    if mode == "known":
        result = expected.handoff
    elif mode == "lookup":
        # Every owned Session has ended and returned its checkout before I/O.
        try:
            ref = gateway.lookup_delivery(organization_id=actor.organization_id,
                                         store_id=actor.store_id, command_key=handoff_id)
        except Exception:
            ref = None
        result = (_record_delivery(session_factory, actor, expected, ref, recovered=True)
                  if isinstance(ref, str) and 1 <= len(ref) <= 200 else expected.handoff)
    else:
        values = dict(organization_id=actor.organization_id, store_id=actor.store_id,
                      command_key=handoff_id, draft_id=expected.handoff.draft_id,
                      amount=expected.amount, currency=expected.currency)
        try:
            ref = gateway.deliver(**values)
            if not isinstance(ref, str) or not 1 <= len(ref) <= 200:
                raise PosError("pos_delivery_invalid")
        except Exception as error:
            try:
                _record_delivery(session_factory, actor, expected, None, failed=True)
            except PosError:
                # Lost authority cannot erase the already-durable unknown intent.
                pass
            raise PosDeliveryFailed("pos_delivery_unknown") from error
        result = _record_delivery(session_factory, actor, expected, ref)
    return DeliveryOutcome(result, mode == "deliver") if with_outcome else result


def deliver_handoff(session, actor, handoff_id, gateway, *, now=None, with_outcome=False):
    """Compatibility boundary: caller work is never committed or rolled back."""
    return deliver_handoff_owned(_factory_from_session(session), actor, handoff_id, gateway,
                                  with_outcome=with_outcome)


def reconcile_receipts_owned(session_factory, actor, handoff_id, gateway):
    with _owned_session(session_factory) as session:
        draft, handoff, attempt = _read_pair(session, actor, handoff_id)
        if not handoff.delivery_ref:
            raise PosError("pos_delivery_missing")
        expected = _binding(draft, handoff, attempt)
    receipts = gateway.read_receipts(delivery_ref=expected.handoff.delivery_ref)
    with _owned_session(session_factory) as session:
        draft, handoff, _attempt_row, current = _apply_pair(session, actor, expected)
        _apply_receipt_candidates(session, actor, draft, handoff, receipts, current)
        result = _snapshot(handoff)
        session.commit()
        return result


def reconcile_receipts(session, actor, handoff_id, gateway, *, now=None):
    return reconcile_receipts_owned(_factory_from_session(session), actor, handoff_id, gateway)


def _note_receipt_conflict(session, actor, draft, handoff, current, action, key, details=None):
    terminal = sales_service._receipt_terminal(draft, handoff)
    if terminal:
        # Report a disputed settlement without altering its reference, timestamp
        # or state. The same evidence may also have been seen before settlement.
        key = f"terminal:{key}"
        changed = draft.sync_state != "conflict"
    else:
        changed = handoff.handoff_state != "receipt_unknown" or draft.sync_state != "conflict"
        if changed:
            handoff.handoff_state, handoff.updated_at = "receipt_unknown", current
    if changed:
        draft.version += 1
        draft.sync_state, draft.updated_at = "conflict", current
    _audit(session, actor, action, handoff, key, current, details)
    return handoff


def _apply_receipt_candidates(session, actor, draft, handoff, receipts, current):
    # Every receipt for this delivery participates in the decision. A wrong
    # store/draft, partial amount or wrong currency is a conflict, never a row
    # that can be silently filtered away.
    all_candidates = [item for item in receipts if item.delivery_ref == handoff.delivery_ref]
    unique = {item.digest: item for item in all_candidates}
    conflicts = [item for item in unique.values()
                 if item.store_id != handoff.store_id
                 or item.draft_id != handoff.draft_id
                 or item.currency != draft.currency
                 or Decimal(item.amount) != Decimal(draft.total_amount)]
    matching = [item for item in unique.values() if item not in conflicts]
    classification = sorted(
        ("mismatch" if item in conflicts else "match") + ":" + item.digest
        for item in unique.values())
    audit_digest = hashlib.sha256("|".join(classification).encode()).hexdigest()
    if conflicts:
        return _note_receipt_conflict(session, actor, draft, handoff, current,
            "sales.pos_receipt_partial_or_mismatch", f"mismatch:{audit_digest}",
            {"candidate_count": len(unique), "classified": classification})
    if not matching:
        return handoff
    if len(matching) > 1:
        return _note_receipt_conflict(session, actor, draft, handoff, current,
            "sales.pos_receipt_ambiguous", f"ambiguous:{audit_digest}",
            {"candidate_count": len(unique), "classified": classification})
    receipt = matching[0]
    try:
        recorded = sales_service.record_receipt(
            session, actor, handoff.id,
            ReceiptCreate(external_ref=receipt.external_ref, paid=receipt.paid),
            idempotency_key=f"pos:{receipt.digest}", receipt_source="pos", now=current,
        )
    except sales_service.SalesError as error:
        if error.code in ("sales_idempotency_conflict", "sales_receipt_conflict"):
            return _note_receipt_conflict(session, actor, draft, handoff, current,
                "sales.pos_receipt_conflict", f"conflict:{receipt.digest}")
        raise
    # Exact replays keep the original audit meaning even if current state differs.
    _audit(session, actor, "sales.pos_receipt_matched", handoff,
           f"matched:{receipt.digest}", current, {"state": recorded.handoff_state})
    return handoff
