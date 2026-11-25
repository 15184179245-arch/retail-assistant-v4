"""PUR-002: employee-authorized partial receiving observations and human reviews.

Never posts a purchase order, creates an ERP receipt, or mutates book stock.
Commands and audit share the caller's outer DB transaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from backend.app.modules.identity.command_guard import lock_employee_authority

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog.models import CatalogProduct
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.auth.models import EmployeeAccount
from backend.app.modules.knowledge.commands import execute_command
from backend.app.modules.purchasing.models import PurchaseIntent, PurchaseIntentLine
from backend.app.modules.receiving.models import (
    ReceivingBatch, ReceivingCommand, ReceivingDecision, ReceivingDiscrepancy, ReceivingLine,
)

_READ, _WRITE, _REVIEW = "receiving.read", "receiving.write", "receiving.review"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_DEC = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,3})?\Z")
_UNITS = frozenset(("piece", "box", "kg", "meter"))
_DECISIONS = frozenset((
    "accept_observation", "request_recount", "return_supplier_review",
    "hold_identity", "hold_price",
))


class ReceivingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise ReceivingError(code)


def _time(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("receiving_time_invalid")
    return value.astimezone(timezone.utc)


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        _fail("receiving_" + field + "_invalid")
    return value


def _name(value: object, field: str, cap: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > cap or value != value.strip():
        _fail("receiving_" + field + "_invalid")
    return value


def _number(value: object, field: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 40 or _DEC.fullmatch(value) is None:
        _fail("receiving_" + field + "_invalid")
    number = Decimal(value)
    if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
        _fail("receiving_" + field + "_invalid")
    return number


def _payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        _fail("receiving_payload_invalid")
    return json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False))


def _require(db: Session, actor: AuthenticatedSubject, permission: str, now: datetime):
    if actor.account_id is None or actor.session_id is None:
        raise HTTPException(status_code=401, detail="human_session_required")
    db.flush()
    db.expire_all()
    authorize_scope(db, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=permission, now=now)
    if db.scalar(select(Subject.subject_type).where(
            Subject.organization_id == actor.organization_id,
            Subject.id == actor.subject_id)) != "person":
        _fail("receiving_person_required")


def _batch(db: Session, actor: AuthenticatedSubject, batch_id: str) -> ReceivingBatch:
    row = db.scalar(select(ReceivingBatch).where(
        ReceivingBatch.id == batch_id,
        ReceivingBatch.organization_id == actor.organization_id,
        ReceivingBatch.store_id == actor.store_id).execution_options(populate_existing=True))
    if row is None:
        _fail("receiving_batch_not_found")
    return row


def _lines(db: Session, actor: AuthenticatedSubject, batch_id: str) -> list[ReceivingLine]:
    return list(db.scalars(select(ReceivingLine).where(
        ReceivingLine.organization_id == actor.organization_id,
        ReceivingLine.store_id == actor.store_id,
        ReceivingLine.batch_id == batch_id).order_by(ReceivingLine.line_key)))


def _discrepancies(db: Session, actor: AuthenticatedSubject, batch_id: str) -> list[ReceivingDiscrepancy]:
    return list(db.scalars(select(ReceivingDiscrepancy).where(
        ReceivingDiscrepancy.organization_id == actor.organization_id,
        ReceivingDiscrepancy.store_id == actor.store_id,
        ReceivingDiscrepancy.batch_id == batch_id).order_by(ReceivingDiscrepancy.id)))


def _decisions(db: Session, actor: AuthenticatedSubject, batch_id: str) -> list[ReceivingDecision]:
    return list(db.scalars(select(ReceivingDecision).where(
        ReceivingDecision.organization_id == actor.organization_id,
        ReceivingDecision.store_id == actor.store_id,
        ReceivingDecision.batch_id == batch_id).order_by(ReceivingDecision.batch_version)))


def _out(db: Session, actor: AuthenticatedSubject, row: ReceivingBatch) -> dict:
    lines = _lines(db, actor, row.id)
    discrepancies = _discrepancies(db, actor, row.id)
    decisions = _decisions(db, actor, row.id)
    latest = {item.discrepancy_id: item for item in decisions}
    return {
        "id": row.id, "organization_id": row.organization_id, "store_id": row.store_id,
        "intent_id": row.intent_id, "expected_intent_version": row.expected_intent_version,
        "delivery_note_sha256": row.delivery_note_sha256, "created_by": row.created_by,
        "status": row.status, "version": row.version,
        "confirmed_by": row.confirmed_by,
        "created_at": row.created_at.isoformat(),
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "lines": [
            {"id": x.id, "line_key": x.line_key, "intent_line_id": x.intent_line_id,
             "product_id": x.product_id, "product_version": x.product_version,
             "unknown_product_label": x.unknown_product_label,
             "observed_quantity": x.observed_quantity, "unit": x.unit,
             "final_for_intent_line": x.final_for_intent_line,
             "evidence_sha256": x.evidence_sha256, "observed_by": x.observed_by}
            for x in lines
        ],
        "discrepancies": [
            {"id": x.id, "line_id": x.line_id, "kind": x.kind, "detail": x.detail,
             "latest_decision": latest[x.id].decision if x.id in latest else None,
             "responsible_id": latest[x.id].responsible_id if x.id in latest else None,
             "reviewer_id": latest[x.id].reviewer_id if x.id in latest else None}
            for x in discrepancies
        ],
        "open_discrepancies": len([x for x in discrepancies
                                   if x.id not in latest or latest[x.id].decision != "accept_observation"]),
        "source_verification": "employee_observation_only",
        "erp_purchase_order_id": None, "erp_goods_receipt_id": None,
        "book_stock_delta": None, "ledger_write": False,
    }


def _audit(db: Session, actor: AuthenticatedSubject, action: str, batch_id: str,
           version: int, details: dict, now: datetime):
    record_event(db, actor_id=actor.subject_id, organization_id=actor.organization_id,
                 store_id=actor.store_id, action=action, target_type="receiving_batch",
                 target_id=batch_id,
                 idempotency_key=f"receiving:{action}:{batch_id}:{version}",
                 occurred_at=now, details=details)


def _cas(db: Session, actor: AuthenticatedSubject, batch: ReceivingBatch,
         expected_version: int, now: datetime, *, reviewed=False):
    if type(expected_version) is not int or expected_version < 1:
        _fail("receiving_expected_version_invalid")
    changed = db.execute(update(ReceivingBatch).where(
        ReceivingBatch.id == batch.id, ReceivingBatch.organization_id == actor.organization_id,
        ReceivingBatch.store_id == actor.store_id,
        ReceivingBatch.status == "observed", ReceivingBatch.version == expected_version,
    ).values(version=expected_version + 1, **(
        {"status": "reviewed", "confirmed_by": actor.subject_id, "confirmed_at": now}
        if reviewed else {}
    )).execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        _fail("receiving_version_conflict")
    db.expire(batch)
    db.refresh(batch)


def _intent_line(db: Session, actor: AuthenticatedSubject, intent_id: str,
                 line_id: str) -> PurchaseIntentLine:
    row = db.scalar(select(PurchaseIntentLine).where(
        PurchaseIntentLine.id == line_id, PurchaseIntentLine.intent_id == intent_id,
        PurchaseIntentLine.organization_id == actor.organization_id,
        PurchaseIntentLine.store_id == actor.store_id))
    if row is None:
        _fail("receiving_intent_line_not_found")
    return row


def _verified_product(db: Session, actor: AuthenticatedSubject, product_id: str):
    product = db.scalar(select(CatalogProduct).where(
        CatalogProduct.id == product_id, CatalogProduct.organization_id == actor.organization_id,
        CatalogProduct.store_id == actor.store_id,
        CatalogProduct.active.is_(True)))
    if product is None:
        _fail("receiving_product_not_found")
    return product


def record_batch(db: Session, actor: AuthenticatedSubject, payload: dict, *,
                 idempotency_key: str, now: datetime | None = None) -> dict:
    # Serialize all observations for this store before reading intent/prior
    # quantities. The same employee/store fence is held by ERP-005 snapshots.
    # Missing credentials still follow the existing receiving 401 contract.
    if actor.account_id is not None and actor.session_id is not None:
        lock_employee_authority(db, actor)
    current = _time(now)
    _require(db, actor, _WRITE, current)
    data = _payload(payload)
    if set(data) != {"intent_id", "expected_intent_version", "delivery_note_sha256", "lines"}:
        _fail("receiving_payload_fields_invalid")
    _sha(data["delivery_note_sha256"], "delivery_note_sha256")
    raw_lines = data["lines"]
    if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= 100:
        _fail("receiving_line_count_invalid")
    intent_id, version = data["intent_id"], data["expected_intent_version"]
    if intent_id is None:
        if version is not None:
            _fail("receiving_intent_version_invalid")
    elif not isinstance(intent_id, str) or not intent_id or type(version) is not int or version < 1:
        _fail("receiving_intent_version_invalid")

    def effect() -> dict:
        duplicate = db.scalar(select(ReceivingBatch.id).where(
            ReceivingBatch.organization_id == actor.organization_id,
            ReceivingBatch.store_id == actor.store_id,
            ReceivingBatch.delivery_note_sha256 == data["delivery_note_sha256"]))
        if duplicate is not None:
            _fail("receiving_duplicate_delivery_note")
        if intent_id is not None:
            intent = db.scalar(select(PurchaseIntent).where(
                PurchaseIntent.id == intent_id,
                PurchaseIntent.organization_id == actor.organization_id,
                PurchaseIntent.store_id == actor.store_id))
            if intent is None or intent.status != "awaiting_arrival" or intent.version != version:
                _fail("receiving_intent_version_conflict")
        batch = ReceivingBatch(
            id=str(uuid4()), organization_id=actor.organization_id,
            store_id=actor.store_id, intent_id=intent_id,
            expected_intent_version=version, delivery_note_sha256=data["delivery_note_sha256"],
            created_by=actor.subject_id, status="observed", version=1,
            confirmed_by=None, created_at=current, confirmed_at=None,
        )
        db.add(batch)
        db.flush()
        seen = set()
        for entry in raw_lines:
            if not isinstance(entry, dict) or set(entry) != {
                "line_key", "intent_line_id", "product_id", "unknown_product_label",
                "observed_quantity", "unit", "final_for_intent_line", "evidence_sha256",
                "quoted_price", "delivered_price", "price_evidence_sha256",
            }:
                _fail("receiving_line_fields_invalid")
            key = _name(entry["line_key"], "line_key", 80)
            if key in seen:
                _fail("receiving_line_duplicate")
            seen.add(key)
            qty = _number(entry["observed_quantity"], "quantity")
            if entry["unit"] not in _UNITS:
                _fail("receiving_unit_unknown")
            proof = _sha(entry["evidence_sha256"], "line_evidence_sha256")
            product_id, label = entry["product_id"], entry["unknown_product_label"]
            if (product_id is None) == (label is None):
                _fail("receiving_identity_exclusive")
            if product_id is not None:
                product = _verified_product(db, actor, _name(product_id, "product_id", 36))
                product_version = product.version
            else:
                _name(label, "unknown_product_label")
                product_version = None
            if type(entry["final_for_intent_line"]) is not bool:
                _fail("receiving_final_flag_invalid")
            price_fields = (entry["quoted_price"], entry["delivered_price"],
                            entry["price_evidence_sha256"])
            if any(x is not None for x in price_fields):
                if any(x is None for x in price_fields):
                    _fail("receiving_price_evidence_incomplete")
                quote = _number(entry["quoted_price"], "quoted_price")
                delivered = _number(entry["delivered_price"], "delivered_price")
                _sha(entry["price_evidence_sha256"], "price_evidence_sha256")
            else:
                quote = delivered = None
            line_id = entry["intent_line_id"]
            if line_id is not None and intent_id is None:
                _fail("receiving_intent_line_without_intent")
            if entry["final_for_intent_line"] and line_id is None:
                _fail("receiving_final_without_intent_line")
            line = ReceivingLine(
                id=str(uuid4()), organization_id=actor.organization_id,
                store_id=actor.store_id, batch_id=batch.id, line_key=key,
                intent_line_id=line_id, product_id=product_id,
                product_version=product_version, unknown_product_label=label,
                observed_quantity=entry["observed_quantity"], unit=entry["unit"],
                final_for_intent_line=entry["final_for_intent_line"], evidence_sha256=proof,
                quoted_price=entry["quoted_price"], delivered_price=entry["delivered_price"],
                price_evidence_sha256=entry["price_evidence_sha256"],
                observed_by=actor.subject_id, observed_at=current,
            )
            db.add(line)
            db.flush()
            types: dict[str, dict] = {}
            if product_id is None:
                types["identity_unknown"] = {"unmatched_label": label}
            if quote is not None and delivered != quote:
                types["price_change"] = {
                    "quoted_price": entry["quoted_price"],
                    "delivered_price": entry["delivered_price"],
                    "price_proof_sha256": entry["price_evidence_sha256"],
                    "unit": entry["unit"],
                    "source_verification": "employee_attested_not_erp",
                }
            if line_id is not None:
                original = _intent_line(db, actor, intent_id, line_id)
                if original.confirmed_by is None or original.quantity is None:
                    _fail("receiving_intent_line_unconfirmed")
                if product_id != original.product_id:
                    types["wrong_item"] = {"expected_product_id": original.product_id,
                                           "observed_product_id": product_id}
                prior = list(db.scalars(select(ReceivingLine).join(
                    ReceivingBatch, ReceivingBatch.id == ReceivingLine.batch_id
                ).where(
                    ReceivingLine.organization_id == actor.organization_id,
                    ReceivingLine.store_id == actor.store_id,
                    ReceivingBatch.intent_id == intent_id,
                    ReceivingLine.intent_line_id == line_id,
                    ReceivingLine.id != line.id,
                )))
                if any(p.final_for_intent_line for p in prior):
                    _fail("receiving_intent_line_already_final")
                # PurchaseIntentLine has NO native unit. All comparisons are
                # human-attested same-unit candidate discrepancies, never a
                # verified packaging conversion or ERP quantity.
                compatible = product_id == original.product_id and all(
                    p.product_id == product_id and p.unit == entry["unit"] for p in prior
                )
                if compatible:
                    cumulative = qty + sum(
                        (_number(p.observed_quantity, "quantity") for p in prior), Decimal(0)
                    )
                    expected = _number(original.quantity, "intent_quantity")
                    if cumulative > expected:
                        types["overage"] = {
                            "expected_quantity": format(expected, "f"),
                            "cumulative_observed_quantity": format(cumulative, "f"),
                            "unit_verification": "employee_attested_unknown_conversion",
                        }
                    elif entry["final_for_intent_line"] and cumulative < expected:
                        types["shortage"] = {
                            "expected_quantity": format(expected, "f"),
                            "cumulative_observed_quantity": format(cumulative, "f"),
                            "unit_verification": "employee_attested_unknown_conversion",
                        }
                else:
                    types["wrong_item"] = {
                        "reason": "product_or_unit_mismatch_across_batches",
                        "intent_line_id": line_id,
                    }
            for kind, detail in types.items():
                db.add(ReceivingDiscrepancy(
                    id=str(uuid4()), organization_id=actor.organization_id,
                    store_id=actor.store_id, batch_id=batch.id, line_id=line.id,
                    kind=kind, detail=detail, created_at=current,
                ))
        db.flush()
        _audit(db, actor, "receiving.batch_observed", batch.id, batch.version,
               {"delivery_note_sha256": batch.delivery_note_sha256,
                "line_count": len(raw_lines), "intent_id": intent_id}, current)
        return _out(db, actor, batch)

    return execute_command(db, actor, operation="batch.record",
                           key=idempotency_key, payload=data, execute=effect,
                           receipt_model=ReceivingCommand)


def decide_discrepancy(db: Session, actor: AuthenticatedSubject, batch_id: str,
                       discrepancy_id: str, *, expected_version: int,
                       decision: str, reason: str, responsible_id: str,
                       evidence_sha256: str, idempotency_key: str,
                       now: datetime | None = None) -> dict:
    current = _time(now)
    _require(db, actor, _REVIEW, current)
    if decision not in _DECISIONS:
        _fail("receiving_decision_invalid")
    _name(reason, "reason", 1000)
    _sha(evidence_sha256, "decision_evidence_sha256")
    _name(responsible_id, "responsible_id", 36)
    data = {"batch_id": batch_id, "discrepancy_id": discrepancy_id,
            "expected_version": expected_version, "decision": decision,
            "reason": reason, "responsible_id": responsible_id,
            "evidence_sha256": evidence_sha256}

    def effect():
        batch = _batch(db, actor, batch_id)
        if batch.created_by == actor.subject_id:
            _fail("receiving_independent_reviewer_required")
        diff = db.scalar(select(ReceivingDiscrepancy).where(
            ReceivingDiscrepancy.id == discrepancy_id,
            ReceivingDiscrepancy.batch_id == batch.id,
            ReceivingDiscrepancy.organization_id == actor.organization_id,
            ReceivingDiscrepancy.store_id == actor.store_id))
        if diff is None:
            _fail("receiving_discrepancy_not_found")
        owner = db.scalar(select(EmployeeAccount.id).where(
            EmployeeAccount.organization_id == actor.organization_id,
            EmployeeAccount.store_id == actor.store_id,
            EmployeeAccount.subject_id == responsible_id,
            EmployeeAccount.active.is_(True)))
        if owner is None:
            _fail("receiving_responsible_person_unavailable")
        _cas(db, actor, batch, expected_version, current)
        db.add(ReceivingDecision(
            id=str(uuid4()), organization_id=actor.organization_id,
            store_id=actor.store_id, batch_id=batch.id, discrepancy_id=diff.id,
            batch_version=batch.version, reviewer_id=actor.subject_id,
            responsible_id=responsible_id, decision=decision, reason=reason,
            evidence_sha256=evidence_sha256, created_at=current,
        ))
        db.flush()
        _audit(db, actor, "receiving.discrepancy_reviewed", batch.id, batch.version,
               {"discrepancy_id": diff.id, "decision": decision,
                "evidence_sha256": evidence_sha256,
                "responsible_id": responsible_id}, current)
        return _out(db, actor, batch)

    return execute_command(db, actor, operation="discrepancy.decide",
                           key=idempotency_key, payload=data, execute=effect,
                           receipt_model=ReceivingCommand)


def confirm_batch(db: Session, actor: AuthenticatedSubject, batch_id: str, *,
                  expected_version: int, reason: str, idempotency_key: str,
                  now: datetime | None = None) -> dict:
    current = _time(now)
    _require(db, actor, _REVIEW, current)
    _name(reason, "reason", 1000)
    data = {"batch_id": batch_id, "expected_version": expected_version, "reason": reason}

    def effect():
        batch = _batch(db, actor, batch_id)
        if batch.created_by == actor.subject_id:
            _fail("receiving_independent_reviewer_required")
        if not _lines(db, actor, batch.id):
            _fail("receiving_batch_empty")
        detail = _out(db, actor, batch)
        if detail["open_discrepancies"] != 0:
            _fail("receiving_unresolved_discrepancies")
        _cas(db, actor, batch, expected_version, current, reviewed=True)
        _audit(db, actor, "receiving.batch_reviewed", batch.id, batch.version,
               {"reason_sha256": sha256(reason.encode()).hexdigest(),
                "partial_observation_only": True,
                "ledger_write": False}, current)
        return _out(db, actor, batch)
    return execute_command(db, actor, operation="batch.confirm",
                           key=idempotency_key, payload=data, execute=effect,
                           receipt_model=ReceivingCommand)


def get_batch(db: Session, actor: AuthenticatedSubject, batch_id: str, *,
              now: datetime | None = None) -> dict:
    _require(db, actor, _READ, _time(now))
    return _out(db, actor, _batch(db, actor, batch_id))
