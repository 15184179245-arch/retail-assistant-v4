"""AGT-015: trusted-host-only purchasing/receiving evidence candidates.

Never fetches WeChat itself, never infers a verified vendor/SKU, never writes
purchase orders, inventory, prices, ERP or even local human-review decisions.
The existing platform tool gateway MUST authorize both the employee and Agent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.purchasing import service as purchase_service
from backend.app.modules.receiving import service as receiving_service
from backend.app.modules.receiving.models import ReceivingBatch, ReceivingLine, ReceivingDiscrepancy, ReceivingDecision


class PurchaseEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str = Field(min_length=1, max_length=36)


class ReceivingExplanationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    batch_id: str = Field(min_length=1, max_length=36)


class PurchaseEvidenceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str
    store_id: str
    intent_id: str
    intent_version: int
    source_kind: str
    supplier_claim: str
    evidence_id: str
    evidence_sha256: str
    consent_confirmed: bool
    candidates: list[dict]
    source_digest: str
    requires_human_confirmation: bool = True
    formal_purchase_order_id: None = None
    goods_receipt_id: None = None
    inventory_delta: None = None
    price_write: bool = False


class ReceivingExplanationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str
    store_id: str
    batch_id: str
    batch_version: int
    batch_status: str
    intent_id: str | None
    expected_intent_version: int | None
    delivery_note_sha256: str
    confirmed_by: str | None
    independent_human_review: bool
    partial_observation_only: bool = True
    source_digest: str
    lines: list[dict]
    discrepancies: list[dict]
    open_discrepancy_count: int
    requires_human_confirmation: bool = True
    formal_purchase_order_id: None = None
    goods_receipt_id: None = None
    inventory_delta: None = None
    price_write: bool = False


_QUANTITY = re.compile(r"(?<![A-Za-z0-9_.])([0-9]+(?:\.[0-9]{1,4})?)\s*(件|个|箱|盒|包|米|piece|box)(?![A-Za-z0-9_.])", re.IGNORECASE)
_ALLOWED = {
    "shortage": ("少货线索：仅按已记录的分批终结状态解释，不得据部分到货断言缺货", "manual_recount"),
    "overage": ("多货线索：须人工核对累积实收和原订货行", "manual_recount"),
    "wrong_item": ("错货线索：商品型号或规格不一致，不能自动替换商品身份", "manual_identity_check"),
    "price_change": ("价格差异线索：需要价格来源凭证与独立核价，不能改价", "manual_price_check"),
    "identity_unknown": ("身份不明线索：须人工查看现场实物，不能推断SKU", "manual_identity_check"),
}


def _iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("purchasing_evidence_time_unverified")
    return value.astimezone(timezone.utc).isoformat()


def _digest(payload: object) -> str:
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _quantity_candidate(text: str | None) -> tuple[str | None, str | None, list[str]]:
    if not text:
        return None, None, ["source_text_not_exactly_located"]
    found = list(_QUANTITY.finditer(text))
    if len(found) != 1:
        return None, None, ["no_unambiguous_quantity"] if not found else ["multiple_quantity_candidates"]
    return found[0].group(1), found[0].group(2), ["raw_text_only_not_confirmed"]


def extract_candidates(session: Session, actor: AuthenticatedSubject,
                       request: PurchaseEvidenceInput) -> PurchaseEvidenceOutput:
    """Reads employee-consented intent; never treats a model guess as verified."""
    intent = purchase_service.get_intent(session, actor, request.intent_id)
    evidence = intent["evidence"]
    if evidence["consent_confirmed"] is not True or evidence["source_kind"] not in (
            "wechat_paste", "delivery_note_text"):
        raise ValueError("purchase_evidence_consent_required")
    original = evidence["original_text"]
    if not isinstance(original, str) or not 1 <= len(original) <= 10000:
        raise ValueError("purchase_evidence_source_invalid")
    candidates = []
    for line in intent["lines"]:
        source = line["original_description"]
        # An employee's structured line may not be a verbatim substring of
        # the uploaded original. Never fabricate spans or quote synthesized text.
        start = original.find(source)
        has_span = start >= 0 and bool(source)
        excerpt = original[start:start + len(source)] if has_span else None
        quantity, unit, reasons = _quantity_candidate(excerpt)
        human = line["confirmed_by"] is not None and line["confirmed_at"] is not None
        if not human:
            reasons.append("product_and_quantity_require_human_confirmation")
        if not has_span:
            reasons.append("source_span_unverified")
        if line["product_id"] is None:
            reasons.append("product_identity_unknown")
        candidate = {
            "intent_line_id": line["id"],
            "description_candidate": source,
            "source_span": ({"start": start, "end": start + len(source)}
                            if has_span else None),
            "source_excerpt": excerpt,
            "evidence_id": evidence["id"], "evidence_sha256": evidence["content_sha256"],
            "source_kind": evidence["source_kind"],
            "extracted_quantity_candidate": quantity,
            "extracted_unit_candidate": unit,
            "human_confirmed": human,
            "confirmed_by": line["confirmed_by"],
            "confirmed_at": line["confirmed_at"],
            "confirmed_product_id": line["product_id"] if human else None,
            "confirmed_product_version": line["product_version"] if human else None,
            "confirmed_quantity": line["quantity"] if human else None,
            "source_span_verified": has_span,
            "candidate_only": True,
            "confidence_reasons": sorted(set(reasons)),
        }
        candidate["source_digest"] = _digest(candidate)
        candidates.append(candidate)
    candidates.sort(key=lambda c: c["intent_line_id"])
    fingerprint = {
        "organization_id": actor.organization_id, "store_id": actor.store_id,
        "intent_id": intent["id"], "intent_version": intent["version"],
        "evidence_id": evidence["id"], "evidence_sha256": evidence["content_sha256"],
        "candidates": candidates,
    }
    return PurchaseEvidenceOutput(
        organization_id=actor.organization_id, store_id=actor.store_id,
        intent_id=intent["id"], intent_version=intent["version"],
        source_kind=evidence["source_kind"], supplier_claim=evidence["supplier_name"],
        evidence_id=evidence["id"], evidence_sha256=evidence["content_sha256"],
        consent_confirmed=True, candidates=candidates, source_digest=_digest(fingerprint),
    )


def explain_discrepancies(session: Session, actor: AuthenticatedSubject,
                          request: ReceivingExplanationInput) -> ReceivingExplanationOutput:
    """Summarize five persisted employee discrepancy types, never auto-decide.

    get_batch re-authorizes receiving.read after the host's additional
    purchasing.read+dual-identity tool gateway checks.
    """
    batch = receiving_service.get_batch(session, actor, request.batch_id)
    scope = (actor.organization_id, actor.store_id)
    rows = list(session.scalars(select(ReceivingLine).where(
        ReceivingLine.organization_id == scope[0],
        ReceivingLine.store_id == scope[1],
        ReceivingLine.batch_id == request.batch_id,
    ).order_by(ReceivingLine.line_key)))
    discrepancy_rows = list(session.scalars(select(ReceivingDiscrepancy).where(
        ReceivingDiscrepancy.organization_id == scope[0],
        ReceivingDiscrepancy.store_id == scope[1],
        ReceivingDiscrepancy.batch_id == request.batch_id,
    ).order_by(ReceivingDiscrepancy.id)))
    decisions = list(session.scalars(select(ReceivingDecision).where(
        ReceivingDecision.organization_id == scope[0],
        ReceivingDecision.store_id == scope[1],
        ReceivingDecision.batch_id == request.batch_id,
    ).order_by(ReceivingDecision.batch_version)))
    # A decision in another store/batch or on a missing discrepancy cannot
    # become the authoritative outcome of this receiving batch.
    known = {d.id for d in discrepancy_rows}
    last: dict[str, ReceivingDecision] = {}
    for decision in decisions:
        if decision.discrepancy_id not in known:
            raise ValueError("receiving_decision_source_inconsistent")
        last[decision.discrepancy_id] = decision
    lines = []
    for row in rows:
        lines.append({
            "line_id": row.id, "line_key": row.line_key,
            "intent_line_id": row.intent_line_id,
            "product_id": row.product_id,
            "product_version": row.product_version,
            "unknown_product_label": row.unknown_product_label,
            "observed_quantity": row.observed_quantity, "unit": row.unit,
            "observed_by": row.observed_by, "observed_at": _iso(row.observed_at),
            "line_evidence_sha256": row.evidence_sha256,
            "final_for_intent_line": row.final_for_intent_line,
            "quoted_price": row.quoted_price, "delivered_price": row.delivered_price,
            "price_evidence_sha256": row.price_evidence_sha256,
            "quantity_or_price_is_formal_receipt": False,
            "source_digest": _digest({
                "id": row.id, "evidence": row.evidence_sha256,
                "product": row.product_id, "product_version": row.product_version,
                "quantity": row.observed_quantity, "unit": row.unit,
                "final": row.final_for_intent_line,
            }),
        })
    out = []
    for row in discrepancy_rows:
        definition = _ALLOWED.get(row.kind)
        if definition is None:
            raise ValueError("receiving_discrepancy_kind_unmapped")
        adjudication = last.get(row.id)
        out.append({
            "discrepancy_id": row.id, "line_id": row.line_id,
            "kind": row.kind,
            "explanation": definition[0], "next_step": definition[1],
            "source_detail": row.detail,
            "requires_human_review": (adjudication is None or adjudication.decision != "accept_observation"),
            "human_decision": adjudication.decision if adjudication else None,
            "reviewer_id": adjudication.reviewer_id if adjudication else None,
            "responsible_id": adjudication.responsible_id if adjudication else None,
            "decision_batch_version": adjudication.batch_version if adjudication else None,
            "decision_evidence_sha256": adjudication.evidence_sha256 if adjudication else None,
            "decision_at": _iso(adjudication.created_at) if adjudication else None,
            "never_auto_erp_write": True,
            "source_digest": _digest({
                "discrepancy_id": row.id, "line_id": row.line_id,
                "kind": row.kind, "detail": row.detail,
                "decision": adjudication.id if adjudication else None,
            }),
        })
    open_count = sum(x["human_decision"] != "accept_observation" for x in out)
    if open_count != batch["open_discrepancies"]:
        raise ValueError("receiving_review_version_inconsistent")
    source = {
        "organization_id": actor.organization_id, "store_id": actor.store_id,
        "batch_id": request.batch_id, "batch_version": batch["version"],
        "delivery_note_sha256": batch["delivery_note_sha256"],
        "lines": lines, "discrepancies": out,
    }
    return ReceivingExplanationOutput(
        organization_id=actor.organization_id, store_id=actor.store_id,
        batch_id=request.batch_id, batch_version=batch["version"],
        batch_status=batch["status"], intent_id=batch["intent_id"],
        expected_intent_version=batch["expected_intent_version"],
        delivery_note_sha256=batch["delivery_note_sha256"],
        confirmed_by=batch["confirmed_by"],
        independent_human_review=batch["status"] == "reviewed",
        lines=lines, discrepancies=out, open_discrepancy_count=open_count,
        source_digest=_digest(source),
    )
