"""ANA-006: deterministic, unverified advice hypotheses and manual action receipts.

No persistence, model, network, new permissions, ERP/Task write or production
endpoint. A trusted future service MUST authenticate/authorize each call and
durably commit receipts before treating an action as accepted.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from backend.app.modules.analytics.sales_facts import project_sales_facts

ADVICE_VERSION = "ana-006-advice-v1"
METRIC_VERSION = "1.0.0"
_ALLOWED_TYPES = frozenset({
    "refund_review", "sales_mix_review", "unresolved_receipt_review",
})
_TRANSITIONS = {
    None: frozenset(("accepted", "rejected")),
    "accepted": frozenset(("in_progress",)),
    "in_progress": frozenset(("reviewed",)),
    "reviewed": frozenset(("closed",)),
    "closed": frozenset(),
    "rejected": frozenset(),
}
_ACTIONS = {
    "refund_review": "人工核对退货凭证、原销售单与退款状态；不得发起退款",
    "sales_mix_review": "人工核对同品不同规格的来源与品类陈列假设；不得自动促销调价",
    "unresolved_receipt_review": "人工核对来源水位、遗漏回执、未知退款与覆盖日期；不得补造金额",
}


class OperationsAdviceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise OperationsAdviceError(code)


def _canonical(data: object) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        _fail("advice_source_not_canonical")


def _hash(data: object) -> str:
    return sha256(_canonical(data).encode("utf-8")).hexdigest()


def _required(value: object, name: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        _fail("advice_" + name + "_invalid")
    return value


def _when(value: object, name: str) -> str:
    value = _required(value, name, maximum=40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("advice_" + name + "_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("advice_" + name + "_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def _advice(
    *,
    category: str, grain: dict, title: str, basis: dict,
    evidence: Mapping[str, Any], source_ids: list[str],
    confidence: str,
) -> dict:
    if category not in _ALLOWED_TYPES or confidence not in ("low", "insufficient"):
        _fail("advice_type_invalid")
    source_ids = sorted(set(source_ids))
    metadata = evidence["provenance"]
    identity = {
        "formula_version": metadata["formula_version"],
        "metric_version": METRIC_VERSION,
        "source_digest": metadata["source_digest"],
        "window_start": metadata["window_start"],
        "window_end": metadata["window_end"],
        "snapshot_at": metadata["snapshot_at"],
        "scope": (metadata["organization_id"], metadata["store_id"]),
        "category": category, "grain": grain,
    }
    result = {
        "advice_id": "advice-" + _hash(identity),
        "advice_version": ADVICE_VERSION,
        "advice_type": category,
        "state": "hypothesis", "publishable": False, "source_verified": False,
        "organization_id": metadata["organization_id"],
        "store_id": metadata["store_id"],
        "metric_id": "sales.paid_reconciled_candidate",
        "metric_version": METRIC_VERSION,
        "metric_formula_version": metadata["formula_version"],
        "metric_formula_digest": metadata["formula_digest"],
        "source_contract_version": metadata["source_contract_version"],
        "source_digest": metadata["source_digest"],
        "source_recalculation_digest": evidence["recalculation_digest"],
        "source_ids": source_ids,
        "source_versions": [
            dict(x) for x in metadata["source_versions"]
            if x["event_id"] in source_ids
        ],
        "window_start": metadata["window_start"],
        "window_end": metadata["window_end"],
        "snapshot_at": metadata["snapshot_at"],
        "source_watermark_at": metadata["source_watermark_at"],
        "grain": grain,
        "title": title,
        "basis": basis,
        "confidence": {
            "level": confidence, "reason": (
                "synthetic_only_source_unverified" if confidence == "low"
                else "source_incomplete_or_unresolved"
            ),
            "source_event_count": metadata["source_event_count"],
            "source_verified": False,
        },
        "expected_impact": None,
        "expected_impact_reason": "no_verified_business_baseline",
        "cost_estimate": None, "cost_reason": "no_verified_cost_source",
        "risk": "Source semantics unverified; manual review only",
        "proposed_owner_id": None,
        "review_due_at": None,
        "action_template": _ACTIONS[category],
        "action_execution": "MANUAL_ONLY_NO_BUSINESS_WRITE",
    }
    result["advice_digest"] = _hash(result)
    return result


def generate_operations_advice(
    rows: Sequence[dict], *,
    organization_id: str, store_id: str,
    window_start: str, window_end: str, snapshot_at: str,
    source_watermark_at: str | None,
    complete_history: bool, authorized: bool,
    analytics_read_authorized: bool,
) -> dict:
    """No unverified monetary assertions and no automatic action.

    A trusted caller must have independently verified the employee's session,
    org/store and ANALYTICS READ permission. Boolean flags are a synthetic-only
    interface, NEVER an HTTP permission model.
    """
    if authorized is not True or analytics_read_authorized is not True:
        _fail("advice_permission_denied")
    projection = project_sales_facts(
        rows, organization_id=organization_id, store_id=store_id,
        window_start=window_start, window_end=window_end,
        snapshot_at=snapshot_at, source_watermark_at=source_watermark_at,
        complete_history=complete_history, authorized=True,
    )
    if (projection["publishable"] is not False
            or projection["source_verified"] is not False
            or projection["state"] != "candidate_only"):
        _fail("advice_source_must_be_candidate")
    meta = projection["provenance"]
    required_meta = ("organization_id", "store_id", "formula_version",
                     "formula_digest", "source_digest", "source_contract_version",
                     "source_versions", "window_start", "window_end", "snapshot_at")
    if any(not meta.get(k) and meta.get(k) != [] for k in required_meta):
        _fail("advice_provenance_missing")
    if (meta["organization_id"], meta["store_id"]) != (organization_id, store_id):
        _fail("advice_scope_mismatch")
    source_ids = [x["event_id"] for x in meta["source_versions"]]
    output = []
    if projection["facts"] is None:
        # A gap/unknown must yield a reconciliation TASK HYPOTHESIS, never
        # invented 'sales were 0' or fabricated confidence/impact.
        output.append(_advice(
            category="unresolved_receipt_review", grain={"kind": "source_window"},
            title="核对本期销售/退款回执及来源覆盖",
            basis={"incomplete_reasons": list(projection["reason"]),
                   "numeric_sales_claim": None,
                   "coverage_verified": False},
            evidence=projection, source_ids=source_ids, confidence="insufficient",
        ))
    else:
        # This is synthetic candidate data. Do not calculate profitability,
        # forecast net cash flow or recommend quantities/price changes.
        mix: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for item in projection["facts"]:
            money = Decimal(item["refunds"]) - Decimal(item["reversed_refunds"])
            if money > 0:
                output.append(_advice(
                    category="refund_review",
                    grain={k: item[k] for k in ("product_id", "spec_id", "unit", "currency")},
                    title="人工核对该规格已归集退款凭证",
                    basis={"candidate_refund_amount": format(money, ".2f"),
                           "currency": item["currency"],
                           "cannot_infer_net_profit": True,
                           "denominator_verified": False},
                    evidence=projection, source_ids=source_ids, confidence="low",
                ))
            mix[item["product_id"], item["unit"], item["currency"]].add(item["spec_id"])
        for (product_id, unit, currency), specs in sorted(mix.items()):
            if len(specs) < 2:
                continue
            output.append(_advice(
                category="sales_mix_review",
                grain={"product_id": product_id, "unit": unit, "currency": currency},
                title="复核同一商品多规格的合成销售构成",
                basis={"spec_ids": sorted(specs), "conversion_rate": None,
                       "stockout_claim": None, "cost_or_margin_claim": None},
                evidence=projection, source_ids=source_ids, confidence="low",
            ))
    output.sort(key=lambda x: (x["advice_type"], x["advice_id"]))
    return {
        "advice_contract_version": ADVICE_VERSION,
        "source_recalculation_digest": projection["recalculation_digest"],
        "source_formula_version": meta["formula_version"],
        "source_formula_digest": meta["formula_digest"],
        "source_digest": meta["source_digest"],
        "organization_id": organization_id, "store_id": store_id,
        "source_verified": False, "publishable": False,
        "category": "synthetic_hypotheses_not_business_facts",
        "candidates": output,
    }


def _check_advice(advice: Mapping[str, Any]) -> None:
    if not isinstance(advice, dict):
        _fail("advice_payload_invalid")
    digest = advice.get("advice_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        _fail("advice_digest_invalid")
    unsigned = dict(advice)
    del unsigned["advice_digest"]
    if _hash(unsigned) != digest:
        _fail("advice_digest_invalid")
    if (advice.get("state") != "hypothesis"
            or advice.get("advice_type") not in _ALLOWED_TYPES
            or advice.get("publishable") is not False
            or advice.get("source_verified") is not False):
        _fail("advice_status_invalid")
    for field in ("advice_id", "organization_id", "store_id",
                  "source_recalculation_digest", "metric_formula_digest"):
        _required(advice.get(field), field)


def replay_manual_actions(advice: dict, events: Sequence[dict], *,
                          organization_id: str, store_id: str,
                          authorized: bool) -> dict:
    """Validate and replay immutable externally persisted, append-only receipts.

    Does not save them. This is not business execution, not a durable queue and
    not sufficient on its own for a production authorization/atomic-CAS gate.
    """
    if authorized is not True:
        _fail("advice_permission_denied")
    _check_advice(advice)
    if (advice["organization_id"], advice["store_id"]) != (
            organization_id, store_id):
        _fail("advice_scope_mismatch")
    if not isinstance(events, (tuple, list)) or len(events) > 10000:
        _fail("advice_events_invalid")
    state: str | None = None
    version = 0
    seen: dict[str, str] = {}
    history = []
    owner_id: str | None = None
    review_due_at: str | None = None
    for item in events:
        if not isinstance(item, dict):
            _fail("advice_receipt_invalid")
        if set(item) != {
                "advice_id", "advice_digest", "organization_id", "store_id",
                "idempotency_key", "version", "state", "reviewer_id",
                "owner_id", "review_due_at", "recorded_at", "reason",
                "business_execution", "receipt_digest",
        } or item.get("business_execution") is not None:
            _fail("advice_receipt_extra_action_forbidden")
        signature = item.get("receipt_digest")
        unsigned = dict(item)
        unsigned.pop("receipt_digest", None)
        if signature != _hash(unsigned):
            _fail("advice_receipt_tampered")
        if (item.get("organization_id"), item.get("store_id"),
                item.get("advice_id"), item.get("advice_digest")) != (
                organization_id, store_id,
                advice["advice_id"], advice["advice_digest"]):
            _fail("advice_receipt_scope_changed")
        idem = _required(item.get("idempotency_key"), "idempotency_key")
        if idem in seen:
            if seen[idem] != signature:
                _fail("advice_idempotency_conflict")
            continue
        if type(item.get("version")) is not int or item["version"] != version + 1:
            _fail("advice_receipt_version_conflict")
        next_state = item.get("state")
        if next_state not in _TRANSITIONS.get(state, ()):
            _fail("advice_illegal_transition")
        _required(item.get("reviewer_id"), "reviewer_id")
        _required(item.get("reason"), "reason", maximum=2000)
        proposed_owner = _required(item.get("owner_id"), "owner_id")
        due = _when(item.get("review_due_at"), "review_due_at")
        _when(item.get("recorded_at"), "recorded_at")
        if owner_id is not None and (owner_id, review_due_at) != (proposed_owner, due):
            _fail("advice_owner_or_due_changed")
        owner_id, review_due_at = proposed_owner, due
        version += 1
        state = next_state
        seen[idem] = signature
        history.append(item)
    return {
        "advice_id": advice["advice_id"], "advice_digest": advice["advice_digest"],
        "organization_id": organization_id, "store_id": store_id,
        "version": version, "state": state or "candidate",
        "owner_id": owner_id, "review_due_at": review_due_at,
        "history": history, "manual_action_only": True, "business_execution": None,
    }


def make_manual_action_receipt(
    advice: dict, existing_receipts: Sequence[dict], *,
    organization_id: str, store_id: str,
    authorized: bool, reviewer_id: str, owner_id: str,
    review_due_at: str, recorded_at: str, reason: str,
    idempotency_key: str, expected_version: int, transition: str,
) -> dict:
    """Return a proposal to persist through an EXISTING authorized service.

    The result alone DOES NOT cause an action, Task, approval, ERP write or
    external operation. Only future owner services may durably store receipts.
    """
    state = replay_manual_actions(
        advice, existing_receipts, organization_id=organization_id,
        store_id=store_id, authorized=authorized,
    )
    idem = _required(idempotency_key, "idempotency_key")
    _required(reviewer_id, "reviewer_id")
    _required(owner_id, "owner_id")
    _required(reason, "reason", maximum=2000)
    due = _when(review_due_at, "review_due_at")
    stamped = _when(recorded_at, "recorded_at")
    # Set the review deadline at the initial decision. Later receipts keep
    # that deadline but may truthfully record that the action occurred late.
    if state["version"] == 0 and datetime.fromisoformat(due) < datetime.fromisoformat(stamped):
        _fail("advice_due_before_decision")
    if type(expected_version) is not int or expected_version < 0:
        _fail("advice_expected_version_invalid")
    if transition not in ("accepted", "rejected", "in_progress", "reviewed", "closed"):
        _fail("advice_illegal_transition")
    next_version = expected_version + 1
    record = {
        "advice_id": advice["advice_id"], "advice_digest": advice["advice_digest"],
        "organization_id": organization_id, "store_id": store_id,
        "idempotency_key": idem, "version": next_version,
        "state": transition, "reviewer_id": reviewer_id, "owner_id": owner_id,
        "review_due_at": due, "recorded_at": stamped, "reason": reason,
        "business_execution": None,
    }
    record["receipt_digest"] = _hash(record)
    for existing in existing_receipts:
        if existing.get("idempotency_key") == idem:
            if existing.get("receipt_digest") == record["receipt_digest"]:
                return dict(existing)
            _fail("advice_idempotency_conflict")
    if expected_version != state["version"]:
        _fail("advice_receipt_version_conflict")
    if transition not in _TRANSITIONS.get(
            None if state["state"] == "candidate" else state["state"], ()):
        _fail("advice_illegal_transition")
    if state["owner_id"] is not None and (
            state["owner_id"], state["review_due_at"]) != (owner_id, due):
        _fail("advice_owner_or_due_changed")
    return record
