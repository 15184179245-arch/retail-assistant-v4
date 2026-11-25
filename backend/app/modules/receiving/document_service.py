"""PUR-003: human-attested PURCHASE document references (never formal ERP writes).

Uses the existing ReceivingCommand receipt, ReceivingBatch version/CAS and
audit in the caller's transaction. No API, external I/O, inventory delta,
purchase creation, ERP endpoint or 'verified upstream' designation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
import json
import re

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.knowledge.commands import execute_command
from backend.app.modules.receiving.models import ReceivingBatch, ReceivingCommand
from backend.app.modules.receiving.service import (ReceivingError, _batch, _lines,
                                                  _out, _require, _time)

OPERATION = "document.verify"
DOC_TYPES = frozenset(("purchase_order", "goods_receipt"))
EVIDENCE_TYPES = frozenset(("erp_screen_view", "erp_paper_document"))
CURRENCIES = frozenset(("CNY",))  # additional currencies require source contract verification
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NUM = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?\Z")
_DOCUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,79}\Z")
_CENT = Decimal("0.01")


class PurchaseDocumentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise PurchaseDocumentError(code)


def _text(value: object, field: str, *, max_length: int = 200) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > max_length):
        _fail("purchase_document_" + field + "_invalid")
    return value


def _digest(value: object) -> str:
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("purchase_document_payload_invalid")
    return sha256(data).hexdigest()


def _number(value: object, field: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, str) or len(value) > 40 or _NUM.fullmatch(value) is None:
        _fail("purchase_document_" + field + "_invalid")
    result = Decimal(value)
    if not result.is_finite() or result > Decimal("1000000000") or (
            result < 0 if allow_zero else result <= 0):
        _fail("purchase_document_" + field + "_invalid")
    return result


def compute_line_total(quantity: str, unit_price: str) -> Decimal:
    """Canonical purchasing arithmetic shared by human refs and ERP commands.

    Inputs remain source values. Zero prices retain the human-reference
    contract; received quantities must be positive and bounded.
    """
    observed = _number(quantity, "quantity")
    price = _number(unit_price, "unit_price", allow_zero=True)
    with localcontext() as context:
        context.prec = 80
        return (observed * price).quantize(_CENT, rounding=ROUND_HALF_UP)


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        _fail("purchase_document_" + field + "_invalid")
    return value


def _iso(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        _fail("purchase_document_" + field + "_invalid")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("purchase_document_" + field + "_invalid")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        _fail("purchase_document_" + field + "_invalid")
    return stamp.astimezone(timezone.utc)


def _authorized(db: Session, actor: AuthenticatedSubject, now: datetime, *, write: bool):
    for permission in ("receiving.read", "purchasing.read") + (
            ("receiving.review",) if write else ()):
        # The original receiving authorization revalidates real employee
        # login/session/account version and scope on every call.
        _require(db, actor, permission, now)


def _document_lines(db: Session, actor: AuthenticatedSubject, batch: ReceivingBatch,
                    requested: object) -> list[dict]:
    if not isinstance(requested, list) or not requested or len(requested) > 200:
        _fail("purchase_document_lines_invalid")
    actual = _lines(db, actor, batch.id)
    if len(actual) != len(requested):
        _fail("purchase_document_line_count_mismatch")
    expected = {row.line_key: row for row in actual}
    if len(expected) != len(actual):
        _fail("purchase_document_internal_line_conflict")
    seen: set[str] = set()
    result: list[dict] = []
    for item in requested:
        if not isinstance(item, dict) or set(item) != {
                "line_key", "product_id", "unit", "quantity", "unit_price",
                "line_total", "source_line_sha256"}:
            _fail("purchase_document_line_schema_invalid")
        key = _text(item["line_key"], "line_key", max_length=80)
        if key in seen or key not in expected:
            _fail("purchase_document_duplicate_or_unknown_line")
        seen.add(key)
        original = expected[key]
        if original.product_id is None or original.unknown_product_label is not None:
            _fail("purchase_document_unknown_product")
        if (_text(item["product_id"], "product_id") != original.product_id
                or _text(item["unit"], "unit", max_length=16) != original.unit
                or _number(item["quantity"], "quantity") !=
                _number(original.observed_quantity, "observed_quantity")):
            _fail("purchase_document_line_mismatch")
        proof = _sha(item["source_line_sha256"], "source_line_sha256")
        if proof in (original.evidence_sha256, batch.delivery_note_sha256):
            _fail("purchase_document_source_not_independent")
        price = item["unit_price"]
        line_total = item["line_total"]
        if (price is None) != (line_total is None):
            _fail("purchase_document_partial_price_claim")
        if price is not None:
            amount = _number(line_total, "line_total", allow_zero=True)
            unit_price = _number(price, "unit_price", allow_zero=True)
            observed = _number(item["quantity"], "quantity")
            if compute_line_total(item["quantity"], price) != amount:
                _fail("purchase_document_amount_mismatch")
            if amount != amount.quantize(_CENT):
                _fail("purchase_document_amount_precision")
        else:
            unit_price = amount = None
        result.append({
            "line_key": key, "product_id": original.product_id,
            "product_version": original.product_version, "unit": original.unit,
            "quantity": item["quantity"], "unit_price": price,
            "line_total": line_total, "source_line_sha256": proof,
        })
    return sorted(result, key=lambda row: row["line_key"])


def _receipts(db: Session, actor: AuthenticatedSubject, batch_id: str) -> list[dict]:
    # JSON filtering works on both PostgreSQL JSON and SQLite JSON1; no other
    # store's responses can ever be exposed through this scoped query.
    records = list(db.scalars(select(ReceivingCommand).where(
        ReceivingCommand.organization_id == actor.organization_id,
        ReceivingCommand.store_id == actor.store_id,
        ReceivingCommand.operation == OPERATION,
        ReceivingCommand.response["batch_id"].as_string() == batch_id,
    )))
    out: list[dict] = []
    for command in records:
        response = command.response
        digest = response.get("receipt_digest") if isinstance(response, dict) else None
        if not isinstance(digest, str) or len(digest) != 64:
            _fail("purchase_document_receipt_corrupt")
        unsigned = {k: v for k, v in response.items() if k != "receipt_digest"}
        if _digest(unsigned) != digest:
            _fail("purchase_document_receipt_corrupt")
        if (response.get("organization_id"), response.get("store_id"),
                response.get("batch_id")) != (
                actor.organization_id, actor.store_id, batch_id):
            _fail("purchase_document_receipt_scope_corrupt")
        if type(response.get("batch_version")) is not int:
            _fail("purchase_document_history_conflict")
        out.append(response)
    out.sort(key=lambda item: item["batch_version"])
    previous = None
    seen = set()
    for record in out:
        version = record.get("batch_version")
        if type(version) is not int or version in seen:
            _fail("purchase_document_history_conflict")
        if record.get("previous_receipt_digest") != previous:
            _fail("purchase_document_history_broken")
        seen.add(version)
        previous = record["receipt_digest"]
    return out


def read_document_references(db: Session, actor: AuthenticatedSubject, batch_id: str,
                             *, now: datetime | None = None) -> dict:
    current = _time(now)
    _authorized(db, actor, current, write=False)
    try:
        batch = _batch(db, actor, batch_id)
    except ReceivingError:
        raise PurchaseDocumentError("purchase_document_batch_not_found") from None
    receipts = _receipts(db, actor, batch.id)
    return {
        "batch_id": batch.id, "batch_version": batch.version,
        "batch_state": batch.status, "batch_reviewed_by": batch.confirmed_by,
        "reference_history": receipts, "current_reference": receipts[-1] if receipts else None,
        "verification_state": "manual_attested_unverified" if receipts else "not_linked",
        "erp_verified": False, "erp_goods_receipt_posted": False,
        "erp_purchase_created": False, "book_stock_delta": None,
    }


def verify_purchase_document(
    db: Session, actor: AuthenticatedSubject, batch_id: str, payload: dict,
    *, expected_version: int, idempotency_key: str, now: datetime | None = None,
) -> dict:
    """Append a locally human-attested document ref, atomic with audit/receipt.

    Caller owns outer DB commit/rollback; never execute ERP purchase/order
    writes. No evidence here independently authenticates an ERP document.
    """
    current = _time(now)
    _authorized(db, actor, current, write=True)
    if type(expected_version) is not int or expected_version < 1:
        _fail("purchase_document_expected_version_invalid")
    if not isinstance(payload, dict) or set(payload) != {
            "document_kind", "claimed_document_no", "evidence_type",
            "evidence_sha256", "source_observed_at", "currency", "total_amount",
            "lines", "human_checked", "note", "previous_receipt_digest"}:
        _fail("purchase_document_payload_invalid")
    if payload["document_kind"] not in DOC_TYPES:
        _fail("purchase_document_kind_invalid")
    if payload["evidence_type"] not in EVIDENCE_TYPES:
        _fail("purchase_document_untrusted_source")
    if payload["human_checked"] is not True:
        _fail("purchase_document_human_confirmation_required")
    document_no = _text(payload["claimed_document_no"], "document_no", max_length=80)
    if _DOCUMENT.fullmatch(document_no) is None:
        _fail("purchase_document_document_no_invalid")
    proof = _sha(payload["evidence_sha256"], "evidence_sha256")
    noted = _text(payload["note"], "note", max_length=1000)
    observed_at = _iso(payload["source_observed_at"], "source_observed_at")
    if observed_at > current:
        _fail("purchase_document_future_source")
    if payload["currency"] not in CURRENCIES:
        _fail("purchase_document_currency_unverified")
    if (payload["previous_receipt_digest"] is not None
            and not isinstance(payload["previous_receipt_digest"], str)):
        _fail("purchase_document_previous_digest_invalid")
    if payload["previous_receipt_digest"] is not None:
        _sha(payload["previous_receipt_digest"], "previous_digest")
    # Immutable JSON normalization; never rely on mutating caller inputs.
    canonical_payload = json.loads(json.dumps(payload, ensure_ascii=False,
                                              sort_keys=True, allow_nan=False))
    command_payload = {"batch_id": batch_id, "expected_version": expected_version,
                       "document": canonical_payload}

    def effect() -> dict:
        batch = db.scalar(select(ReceivingBatch).where(
            ReceivingBatch.id == batch_id,
            ReceivingBatch.organization_id == actor.organization_id,
            ReceivingBatch.store_id == actor.store_id).with_for_update())
        if batch is None:
            _fail("purchase_document_batch_not_found")
        if batch.status != "reviewed" or batch.confirmed_by is None:
            _fail("purchase_document_batch_not_reviewed")
        if batch.created_by == actor.subject_id:
            _fail("purchase_document_independent_reviewer_required")
        if batch.version != expected_version:
            _fail("purchase_document_version_conflict")
        details = _out(db, actor, batch)
        if details["open_discrepancies"] != 0:
            _fail("purchase_document_unresolved_discrepancy")
        if proof == batch.delivery_note_sha256 or any(
                line.evidence_sha256 == proof for line in _lines(db, actor, batch.id)):
            _fail("purchase_document_source_not_independent")
        lines = _document_lines(db, actor, batch, canonical_payload["lines"])
        has_amounts = [line["line_total"] is not None for line in lines]
        if any(has_amounts) and not all(has_amounts):
            _fail("purchase_document_partial_total_claim")
        total = canonical_payload["total_amount"]
        if all(has_amounts):
            expected_total = sum((Decimal(row["line_total"]) for row in lines), Decimal(0))
            claimed_total = _number(total, "total_amount", allow_zero=True)
            if (expected_total.quantize(_CENT, rounding=ROUND_HALF_UP) != claimed_total
                    or claimed_total != claimed_total.quantize(_CENT)):
                _fail("purchase_document_total_mismatch")
        elif total is not None:
            _fail("purchase_document_total_without_line_prices")
        # The same evidence fingerprint cannot be reused to attest a
        # different batch as an independent receipt. A purchase ORDER number
        # itself may legitimately span several deliveries; therefore we
        # compare evidence digests rather than globally forbidding order nos.
        cross_batch = db.scalar(select(ReceivingCommand).where(
            ReceivingCommand.organization_id == actor.organization_id,
            ReceivingCommand.store_id == actor.store_id,
            ReceivingCommand.operation == OPERATION,
            ReceivingCommand.response["evidence_sha256"].as_string() == proof,
            ReceivingCommand.response["batch_id"].as_string() != batch.id,
        ))
        if cross_batch is not None:
            _fail("purchase_document_evidence_used_by_other_batch")
        history = _receipts(db, actor, batch.id)
        last = history[-1] if history else None
        if (canonical_payload["previous_receipt_digest"] !=
                (last["receipt_digest"] if last else None)):
            _fail("purchase_document_previous_receipt_conflict")
        # Guard against accidental use of this claimed number as an additional
        # official receipt for the SAME reviewed batch, including corrections.
        if last and (last["document_kind"], last["claimed_document_no"],
                     last["evidence_sha256"]) == (
                canonical_payload["document_kind"], document_no, proof):
            _fail("purchase_document_correction_requires_new_evidence")
        changed = db.execute(update(ReceivingBatch).where(
            ReceivingBatch.id == batch.id,
            ReceivingBatch.organization_id == actor.organization_id,
            ReceivingBatch.store_id == actor.store_id,
            ReceivingBatch.status == "reviewed",
            ReceivingBatch.version == expected_version,
        ).values(version=expected_version + 1).execution_options(
            synchronize_session=False)).rowcount
        if changed != 1:
            _fail("purchase_document_version_conflict")
        snapshot = {
            "organization_id": actor.organization_id, "store_id": actor.store_id,
            "batch_id": batch.id, "batch_version": expected_version + 1,
            "document_kind": canonical_payload["document_kind"],
            "claimed_document_no": document_no,
            "evidence_type": canonical_payload["evidence_type"],
            "evidence_sha256": proof,
            "source_observed_at": observed_at.isoformat(),
            "lines": lines, "currency": canonical_payload["currency"],
            "total_amount": total, "note": noted,
            "previous_receipt_digest": last["receipt_digest"] if last else None,
            "human_checked_by": actor.subject_id,
            "human_checked_at": current.isoformat(),
            "source_batch_delivery_note_sha256": batch.delivery_note_sha256,
            "source_batch_version": expected_version,
            "verification_state": "manual_attested_unverified",
            "erp_verified": False, "erp_goods_receipt_posted": False,
            "erp_purchase_created": False, "book_stock_delta": None,
        }
        snapshot["receipt_digest"] = _digest(snapshot)
        record_event(
            db, actor_id=actor.subject_id,
            organization_id=actor.organization_id, store_id=actor.store_id,
            action="receiving.purchase_document_attested",
            target_type="receiving_batch", target_id=batch.id,
            idempotency_key="receiving:purchase_document:" + batch.id +
                            ":" + str(expected_version + 1),
            occurred_at=current,
            details={
                "batch_version": expected_version + 1, "receipt_digest": snapshot["receipt_digest"],
                "evidence_sha256": proof, "document_no_digest": sha256(
                    document_no.encode("utf-8")).hexdigest(),
                "erp_verified": False, "ledger_write": False,
            },
        )
        db.flush()
        return snapshot

    return execute_command(
        db, actor, operation=OPERATION, key=idempotency_key,
        payload=command_payload, execute=effect, receipt_model=ReceivingCommand,
    )
