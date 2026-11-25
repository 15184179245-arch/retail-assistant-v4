"""ANA-004: deterministic, isolated SALES/RETURN/REVERSAL fact projection.

NOT a production ERP connector or report. No database, HTTP, mutations, floats,
implicit FX, estimated cost or trusted source mappings. Externally authoritative
sales/return semantics remain unverified until ERP-001 read-only sampling.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

FORMULA_VERSION = "ana-004-sales-facts-v1"
SOURCE_CONTRACT_VERSION = "synthetic-projection-v1"
_MONEY = Decimal("0.01")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_ALLOWED = {
    "sale": frozenset(("settled", "draft", "pending", "sent", "receipt_unknown", "unknown")),
    "return": frozenset(("settled", "pending", "receipt_unknown", "unknown")),
    "reversal": frozenset(("confirmed", "pending", "unknown")),
}
_FINAL = {"sale": "settled", "return": "settled", "reversal": "confirmed"}
_BUCKET = {"sale": "gross", "return": "refunds", "reversal_sale": "reversed_sales",
           "reversal_return": "reversed_refunds"}


class SalesFactError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SalesFactError(code)


def _instant(value: object) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 40:
        _fail("analytics_timestamp_invalid")
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("analytics_timestamp_invalid")
    if t.tzinfo is None or t.utcoffset() is None:
        _fail("analytics_timestamp_invalid")
    return t.astimezone(timezone.utc)


def _number(value: object, *, positive: bool = True) -> Decimal:
    if (not isinstance(value, str) or len(value) > 40
            or _DECIMAL.fullmatch(value) is None):
        _fail("analytics_decimal_invalid")
    number = Decimal(value)
    if not number.is_finite() or (positive and number <= 0) or number < 0:
        _fail("analytics_decimal_invalid")
    return number


def _required(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or len(value) > 200 or value != value.strip():
        _fail("analytics_missing_" + field)
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("analytics_noncanonical_source")


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _money(value: Decimal) -> str:
    return format(value.quantize(_MONEY, rounding=ROUND_HALF_UP), ".2f")


def _quantity(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ident(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (row["source_system"], row["source_ref"], row["source_line_ref"], row["kind"])


def _line(row: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (row["sale_id"], row["line_id"], row["product_id"],
            row["spec_id"], row["unit"], row["currency"])


def _check_source(row: object, org: str, store: str, snapshot: datetime) -> dict | None:
    if not isinstance(row, dict):
        _fail("analytics_source_row_invalid")
    if (row.get("organization_id"), row.get("store_id")) != (org, store):
        _fail("analytics_scope_denied")
    for field in ("event_id", "source_system", "source_ref", "source_line_ref",
                  "kind", "status", "recorded_at", "occurred_at"):
        _required(row, field)
    kind = row["kind"]
    if kind not in _ALLOWED or row["status"] not in _ALLOWED[kind]:
        _fail("analytics_status_unmapped")
    if type(row.get("source_version")) is not int or row["source_version"] <= 0:
        _fail("analytics_version_invalid")
    observed, occurred = _instant(row["recorded_at"]), _instant(row["occurred_at"])
    if observed > snapshot:
        return None  # future-arriving revision does not belong to this snapshot
    if occurred > snapshot:
        _fail("analytics_future_business_timestamp")
    if row["status"] == _FINAL[kind]:
        for field in ("sale_id", "line_id", "product_id", "spec_id", "unit", "currency"):
            _required(row, field)
        if _CURRENCY.fullmatch(row["currency"]) is None:
            _fail("analytics_currency_invalid")
        _number(row.get("quantity"))
        _number(row.get("amount"))
        if kind == "return":
            _required(row, "original_sale_event_id")
        if kind == "reversal":
            _required(row, "target_event_id")
    # The source adapter must define every raw revision as a JSON-stable record.
    _canonical(row)
    return dict(row)


def _latest(rows: Sequence[dict], organization_id: str, store_id: str,
            snapshot: datetime) -> dict[str, dict]:
    selected: dict[str, dict] = {}
    for original in rows:
        row = _check_source(original, organization_id, store_id, snapshot)
        if row is None:
            continue
        key = row["event_id"]
        previous = selected.get(key)
        if previous is None:
            selected[key] = row
            continue
        if _ident(previous) != _ident(row):
            _fail("analytics_event_identity_changed")
        if row["source_version"] == previous["source_version"]:
            if _canonical(previous) != _canonical(row):
                _fail("analytics_same_version_conflict")
            continue
        older, newer = sorted((row, previous), key=lambda x: x["source_version"])
        if _instant(newer["recorded_at"]) < _instant(older["recorded_at"]):
            _fail("analytics_version_clock_regressed")
        if newer is row:
            selected[key] = row
    refs = {}
    for row in selected.values():
        key = _ident(row)
        if key in refs and refs[key] != row["event_id"]:
            _fail("analytics_duplicate_source_reference")
        refs[key] = row["event_id"]
    return selected


def _validated_facts(events: dict[str, dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    complete = {k: v for k, v in events.items()
                if v["status"] == _FINAL[v["kind"]]}
    reversed_by: dict[str, dict] = {}
    for row in complete.values():
        if row["kind"] != "reversal":
            continue
        target = complete.get(row["target_event_id"])
        if target is None or target["kind"] == "reversal":
            _fail("analytics_reversal_target_invalid")
        if target["event_id"] in reversed_by:
            _fail("analytics_double_reversal")
        if (_line(target) != _line(row)
                or _number(target["quantity"]) != _number(row["quantity"])
                or _number(target["amount"]) != _number(row["amount"])
                or _instant(row["occurred_at"]) < _instant(target["occurred_at"])):
            _fail("analytics_reversal_not_exact")
        reversed_by[target["event_id"]] = row
    sale_events = {k: v for k, v in complete.items() if v["kind"] == "sale"}
    returns: dict[str, list[dict]] = defaultdict(list)
    for row in complete.values():
        if row["kind"] != "return":
            continue
        original = sale_events.get(row["original_sale_event_id"])
        if original is None or _line(row) != _line(original):
            _fail("analytics_return_source_unmatched")
        if _instant(row["occurred_at"]) < _instant(original["occurred_at"]):
            _fail("analytics_return_predates_sale")
        returns[original["event_id"]].append(row)
    for sale_id, group in returns.items():
        sale = sale_events[sale_id]
        # Refund correction itself must also be reversed to cancel it.
        live = [r for r in group if r["event_id"] not in reversed_by]
        if sale_id in reversed_by and live:
            _fail("analytics_sale_reversal_with_active_refund")
        if (sum((_number(r["quantity"]) for r in live), Decimal(0))
                > _number(sale["quantity"])
                or sum((_number(r["amount"]) for r in live), Decimal(0))
                > _number(sale["amount"])):
            _fail("analytics_refund_exceeds_settled_sale")
    return complete, reversed_by


def project_sales_facts(
    rows: Sequence[dict], *,
    organization_id: str, store_id: str, window_start: str, window_end: str,
    snapshot_at: str, source_watermark_at: str | None,
    complete_history: bool, authorized: bool,
) -> dict:
    """Produce CANDIDATE ONLY synthetic facts; never claim verified ERP revenue.

    `rows` is a single-store trusted adapter's synthetic/unverified projection,
    including original historical sales of any refund in the requested window.
    Permission, source history completeness and watermarks are caller-supplied
    claims, not inferred from a static external API description.
    """
    if not authorized:
        _fail("analytics_scope_denied")
    org = _required({"organization_id": organization_id}, "organization_id")
    store = _required({"store_id": store_id}, "store_id")
    start, end, snapshot = map(_instant, (window_start, window_end, snapshot_at))
    if start >= end or end > snapshot:
        _fail("analytics_window_invalid")
    if type(complete_history) is not bool:
        _fail("analytics_coverage_invalid")
    if source_watermark_at is not None:
        watermark = _instant(source_watermark_at)
        if watermark > snapshot:
            _fail("analytics_watermark_future")
    else:
        watermark = None
    if not isinstance(rows, (tuple, list)) or len(rows) > 100_000:
        _fail("analytics_source_batch_invalid")

    events = _latest(rows, org, store, snapshot)
    complete, reversed_by = _validated_facts(events)

    window_rows = sorted(
        (r for r in events.values()
         if start <= _instant(r["occurred_at"]) < end),
        key=lambda x: (x["occurred_at"], x["event_id"]),
    )
    excluded = [{
        "event_id": r["event_id"], "reason": "not_settled:" + r["status"],
        "source_digest": _digest(r),
    } for r in window_rows if r["event_id"] not in complete]
    uncertainty = any(r["status"] in ("receipt_unknown", "unknown")
                      or r["kind"] in ("return", "reversal")
                      for r in window_rows if r["event_id"] not in complete)
    coverage_reasons = []
    if not complete_history:
        coverage_reasons.append("historical_sales_or_returns_not_verified")
    if watermark is None:
        coverage_reasons.append("source_watermark_missing")
    elif watermark < end:
        coverage_reasons.append("source_watermark_before_window_end")
    if uncertainty:
        coverage_reasons.append("unresolved_settlement_or_refund")
    if not window_rows:
        coverage_reasons.append("no_population")
    elif not any(r["event_id"] in complete for r in window_rows):
        coverage_reasons.append("no_settled_fact_in_window")

    buckets = defaultdict(lambda: defaultdict(Decimal))
    lineage = []
    contributions = []
    for r in window_rows:
        if r["event_id"] not in complete:
            continue
        kind = r["kind"]
        if kind == "reversal":
            target = complete[r["target_event_id"]]
            bucket = "reversed_sales" if target["kind"] == "sale" else "reversed_refunds"
            # The reversal has the same exact grain as the original target.
            signed_kind = "reversal_" + target["kind"]
            references = [target["event_id"]]
        else:
            bucket = "gross" if kind == "sale" else "refunds"
            signed_kind = kind
            references = ([r["original_sale_event_id"]] if kind == "return" else [])
        key = (r["product_id"], r["spec_id"], r["unit"], r["currency"])
        amount = _number(r["amount"])
        quantity = _number(r["quantity"])
        buckets[key][bucket] += amount
        buckets[key][bucket + "_quantity"] += quantity
        lineage.append({
            "event_id": r["event_id"], "source_system": r["source_system"],
            "source_ref": r["source_ref"], "source_line_ref": r["source_line_ref"],
            "source_version": r["source_version"], "source_recorded_at": r["recorded_at"],
            "business_at": r["occurred_at"], "event_kind": signed_kind,
            "source_digest": _digest(r), "linked_event_ids": references,
        })
        contributions.append((r["event_id"], r["source_version"], bucket, key))
    details = []
    for (product, spec, unit, currency), amounts in sorted(buckets.items()):
        def amt(k):
            return amounts.get(k, Decimal(0))
        gross, refunds = amt("gross"), amt("refunds")
        sale_undo, refund_undo = amt("reversed_sales"), amt("reversed_refunds")
        details.append({
            "product_id": product, "spec_id": spec, "unit": unit,
            "currency": currency,
            "gross": _money(gross), "refunds": _money(refunds),
            "reversed_sales": _money(sale_undo),
            "reversed_refunds": _money(refund_undo),
            "net": _money(gross - refunds - sale_undo + refund_undo),
            "gross_quantity": _quantity(amt("gross_quantity")),
            "refund_quantity": _quantity(amt("refunds_quantity")),
            "net_quantity": _quantity(
                amt("gross_quantity") - amt("refunds_quantity")
                - amt("reversed_sales_quantity") + amt("reversed_refunds_quantity")),
        })

    coverage_ok = not coverage_reasons
    source_lineage = sorted(lineage, key=lambda x: x["event_id"])
    source_versions = sorted(
        [{"event_id": v["event_id"], "source_version": v["source_version"],
          "source_digest": _digest(v)} for v in events.values()],
        key=lambda x: x["event_id"],
    )
    formula = {
        "version": FORMULA_VERSION, "source_contract_version": SOURCE_CONTRACT_VERSION,
        "rounding": "Decimal ROUND_HALF_UP final aggregation to 0.01",
        "sign": "sale - settled_return - reversed_sale + reversed_return",
        "grain": "product_id,spec_id,unit,currency",
        "window": "occurred_at UTC [start,end)",
    }
    meta = {
        "organization_id": org, "store_id": store,
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "snapshot_at": snapshot.isoformat(),
        "source_watermark_at": watermark.isoformat() if watermark else None,
        "formula_version": FORMULA_VERSION, "formula_digest": _digest(formula),
        "source_contract_version": SOURCE_CONTRACT_VERSION,
        "source_digest": _digest(source_versions),
        "source_event_count": len(events), "included_event_count": len(source_lineage),
        "excluded_event_count": len(excluded),
        "source_versions": source_versions,
    }
    return {
        "state": "candidate_only", "publishable": False,
        "source_verified": False, "reason": None if coverage_ok else coverage_reasons,
        "amounts_complete_in_synthetic_projection": coverage_ok,
        "facts": details if coverage_ok else None,
        "gross_margin": None, "gross_margin_reason": "cost_source_unverified",
        "excluded": excluded, "lineage": source_lineage,
        "provenance": meta, "recalculation_digest": _digest({
            "metadata": meta, "lineage": source_lineage, "facts": details,
            "excluded": excluded, "coverage_reasons": coverage_reasons,
        }),
    }
