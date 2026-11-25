"""ANA-007: offline, reproducible, pre-registered growth pilot evaluation.

This module is NOT an execution engine, verified POS connector, causal estimator,
authorization middleware, or production report. All input remains synthetic or
unverified until separately authenticated, audited and approved by a real
service. No model, network, SQL, Tasks, ERP or business mutations occur.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import re
from typing import Any

from backend.app.modules.analytics.sales_facts import (
    FORMULA_VERSION as SALES_FORMULA_VERSION, project_sales_facts,
)

PILOT_CONTRACT_VERSION = "ana-007-offline-v1"
METRIC_VERSION = "1.0.0"
READ_ONLY_ACTIONS = frozenset((
    "manual_display_review", "manual_customer_information_review",
))
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_SIGNED_DECIMAL_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:[.][0-9]{1,6})?$")
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")
_COHORTS = ("treatment", "control")
_PERIODS = ("pre", "post")
_BUCKETS = tuple(f"{c}_{p}" for c in _COHORTS for p in _PERIODS)
_MONEY = Decimal("0.01")


class GrowthEvaluationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise GrowthEvaluationError(code)


def _required(value: object, name: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200
            or value != value.strip()):
        _fail("growth_invalid_" + name)
    return value


def _decimal(value: object, *, allow_zero: bool = True,
             allow_negative: bool = False) -> Decimal:
    syntax = _SIGNED_DECIMAL_RE if allow_negative else _DECIMAL_RE
    if not isinstance(value, str) or len(value) > 40 or syntax.fullmatch(value) is None:
        _fail("growth_decimal_invalid")
    val = Decimal(value)
    if not val.is_finite() or (val < 0 and not allow_negative) or (
            not allow_zero and val == 0):
        _fail("growth_decimal_invalid")
    return val


def _time(value: object) -> datetime:
    value = _required(value, "time")
    if len(value) > 40:
        _fail("growth_invalid_time")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("growth_invalid_time")
    if dt.tzinfo is None or dt.utcoffset() is None:
        _fail("growth_invalid_time")
    return dt.astimezone(timezone.utc)


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        _fail("growth_noncanonical_data")


def _hash(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _money(value: Decimal) -> str:
    return format(value.quantize(_MONEY, rounding=ROUND_HALF_UP), ".2f")


def _period(plan: dict, name: str) -> tuple[datetime, datetime]:
    raw = plan.get(name)
    if not isinstance(raw, dict) or set(raw) != {"start", "end"}:
        _fail("growth_invalid_window")
    start, end = _time(raw["start"]), _time(raw["end"])
    if start >= end:
        _fail("growth_invalid_window")
    return start, end


def _grains(rows: object) -> set[tuple[str, str, str]]:
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        _fail("growth_missing_control_or_treatment")
    seen: set[tuple[str, str, str]] = set()
    for item in rows:
        if not isinstance(item, dict) or set(item) != {"product_id", "spec_id", "unit"}:
            _fail("growth_invalid_grain")
        grain = tuple(_required(item[key], key) for key in
                      ("product_id", "spec_id", "unit"))
        if grain in seen:
            _fail("growth_duplicate_grain")
        seen.add(grain)
    return seen


def freeze_growth_plan(design: dict) -> dict:
    """Immutable JSON-compatible plan envelope, NOT operational authorization."""
    if not isinstance(design, dict):
        _fail("growth_invalid_design")
    required = {
        "pilot_id", "organization_id", "store_id", "owner_id", "advice_id",
        "advice_digest", "action_kind", "treatment", "control", "currency",
        "pre_window", "post_window", "registered_at", "minimum_samples",
        "min_descriptive_did", "guardrail_limits", "confound_checklist",
    }
    if set(design) != required:
        _fail("growth_design_fields_mismatch")
    for key in ("pilot_id", "organization_id", "store_id", "owner_id", "advice_id"):
        _required(design[key], key)
    digest = design["advice_digest"]
    if not isinstance(digest, str) or re.fullmatch("[a-f0-9]{64}", digest) is None:
        _fail("growth_invalid_advice_digest")
    if design["action_kind"] not in READ_ONLY_ACTIONS:
        _fail("growth_action_not_allowed")
    if not isinstance(design["currency"], str) or (
            _CURRENCY_RE.fullmatch(design["currency"]) is None):
        _fail("growth_invalid_currency")
    treatment, control = _grains(design["treatment"]), _grains(design["control"])
    if treatment.intersection(control):
        _fail("growth_overlapping_cohorts")
    pre_start, pre_end = _period(design, "pre_window")
    post_start, post_end = _period(design, "post_window")
    if (pre_end > post_start or pre_end - pre_start != post_end - post_start
            or (post_end - pre_start).days > 90):
        _fail("growth_noncomparable_windows")
    if _time(design["registered_at"]) >= post_start:
        _fail("growth_plan_post_registered")
    if type(design["minimum_samples"]) is not int or not 2 <= design["minimum_samples"] <= 100000:
        _fail("growth_invalid_sample_floor")
    _decimal(design["min_descriptive_did"])
    limits = design["guardrail_limits"]
    required_limits = {
        "min_treatment_post_gross_margin", "max_treatment_refund_rate_increase_bps",
        "max_treatment_post_stockouts", "max_treatment_post_inventory_occupied",
        "max_treatment_post_labor_minutes",
    }
    if not isinstance(limits, dict) or set(limits) != required_limits:
        _fail("growth_guardrail_missing")
    for key, value in limits.items():
        _decimal(value, allow_negative=(key == "min_treatment_post_gross_margin"))
    checklist = design["confound_checklist"]
    if (not isinstance(checklist, list)
            or set(checklist) != {"holiday", "promotion", "pricing",
                                  "competitor", "supply_disruption"}):
        _fail("growth_confound_checklist_missing")
    frozen = json.loads(_canonical(design))  # disallow mutable foreign objects
    return {"contract_version": PILOT_CONTRACT_VERSION,
            "design": frozen, "plan_digest": _hash({
                "contract_version": PILOT_CONTRACT_VERSION, "design": frozen,
            }), "execution_enabled": False, "real_pilot_approved": False}


def _check_plan(plan: dict) -> dict:
    if not isinstance(plan, dict) or set(plan) != {
            "contract_version", "design", "plan_digest",
            "execution_enabled", "real_pilot_approved"}:
        _fail("growth_plan_tampered")
    if (plan["contract_version"] != PILOT_CONTRACT_VERSION
            or plan["execution_enabled"] is not False
            or plan["real_pilot_approved"] is not False):
        _fail("growth_plan_tampered")
    validated = freeze_growth_plan(plan["design"])
    if validated != plan:
        _fail("growth_plan_tampered")
    return validated["design"]


def _audit_receipts(plan: dict, receipts: object) -> tuple[list[str], list[dict]]:
    if not isinstance(receipts, list) or len(receipts) > 1000:
        _fail("growth_invalid_receipts")
    design = plan["design"]
    post_start, post_end = _period(design, "post_window")
    evidence = []
    seen: dict[str, str] = {}
    reasons: list[str] = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            _fail("growth_invalid_receipt")
        required = {"receipt_id", "pilot_id", "plan_digest", "organization_id",
                    "store_id", "owner_id", "approved_by", "executed_by",
                    "action_kind", "occurred_at", "capability_gate_refs",
                    "manual_confirmation"}
        if set(receipt) != required:
            _fail("growth_receipt_fields_invalid")
        for key in ("receipt_id", "owner_id", "approved_by", "executed_by"):
            _required(receipt[key], key)
        if ((receipt["pilot_id"], receipt["plan_digest"],
             receipt["organization_id"], receipt["store_id"],
             receipt["owner_id"], receipt["action_kind"]) != (
             design["pilot_id"], plan["plan_digest"],
             design["organization_id"], design["store_id"],
             design["owner_id"], design["action_kind"])):
            _fail("growth_receipt_scope_or_action_mismatch")
        timestamp = _time(receipt["occurred_at"])
        if not post_start <= timestamp < post_end:
            _fail("growth_intervention_outside_window")
        if receipt["manual_confirmation"] is not True:
            _fail("growth_unconfirmed_action")
        if (not isinstance(receipt["capability_gate_refs"], list)
                or not receipt["capability_gate_refs"]
                or not all(isinstance(x, str) and x and len(x) <= 200
                           for x in receipt["capability_gate_refs"])):
            _fail("growth_missing_capability_gate")
        digest = _hash(receipt)
        if receipt["receipt_id"] in seen:
            if seen[receipt["receipt_id"]] != digest:
                _fail("growth_duplicate_intervention_conflict")
            continue
        seen[receipt["receipt_id"]] = digest
        evidence.append({"receipt_id": receipt["receipt_id"],
                         "digest": digest, "occurred_at": timestamp.isoformat(),
                         "capability_gate_refs": receipt["capability_gate_refs"]})
    if not evidence:
        reasons.append("missing_manual_intervention_receipt")
    return reasons, sorted(evidence, key=lambda x: x["receipt_id"])


def _operating_guardrails(observations: object, plan: dict
                          ) -> tuple[list[str], dict]:
    if not isinstance(observations, list) or len(observations) != 4:
        return ["missing_guardrail_observations"], {}
    indexed: dict[str, dict] = {}
    for record in observations:
        if not isinstance(record, dict):
            _fail("growth_guardrail_record_invalid")
        required = {"bucket", "organization_id", "store_id", "source_id",
                    "source_version", "source_digest", "source_verified",
                    "gross_margin", "stockout_events", "inventory_occupied",
                    "labor_minutes"}
        if set(record) != required:
            _fail("growth_guardrail_record_invalid")
        bucket = record["bucket"]
        if bucket not in _BUCKETS or bucket in indexed:
            _fail("growth_guardrail_duplicate_bucket")
        if (record["organization_id"], record["store_id"]) != (
                plan["design"]["organization_id"], plan["design"]["store_id"]):
            _fail("growth_scope_denied")
        _required(record["source_id"], "guardrail_source_id")
        if type(record["source_version"]) is not int or record["source_version"] <= 0:
            _fail("growth_guardrail_version_invalid")
        if (not isinstance(record["source_digest"], str)
                or re.fullmatch("[a-f0-9]{64}", record["source_digest"]) is None):
            _fail("growth_guardrail_provenance_invalid")
        if type(record["source_verified"]) is not bool:
            _fail("growth_guardrail_verified_invalid")
        for key in ("gross_margin", "stockout_events", "inventory_occupied",
                    "labor_minutes"):
            if record[key] is not None:
                _decimal(record[key], allow_negative=(key == "gross_margin"))
        indexed[bucket] = record
    if set(indexed) != set(_BUCKETS):
        return ["missing_guardrail_observations"], {}
    tr_pre, tr_post = indexed["treatment_pre"], indexed["treatment_post"]
    limits = plan["design"]["guardrail_limits"]
    reasons = []
    for key in ("gross_margin", "stockout_events", "inventory_occupied",
                "labor_minutes"):
        if any(indexed[b][key] is None for b in _BUCKETS):
            reasons.append("missing_" + key + "_guardrail")
    if not reasons:
        if _decimal(tr_post["gross_margin"], allow_negative=True) < _decimal(
                limits["min_treatment_post_gross_margin"], allow_negative=True):
            reasons.append("gross_margin_guardrail_failed")
        for src, limit, label in (
            ("stockout_events", "max_treatment_post_stockouts", "stockout"),
            ("inventory_occupied", "max_treatment_post_inventory_occupied", "inventory"),
            ("labor_minutes", "max_treatment_post_labor_minutes", "labor"),
        ):
            if _decimal(tr_post[src]) > _decimal(limits[limit]):
                reasons.append(label + "_guardrail_failed")
    return reasons, {
        "source_refs": [{k: indexed[b][k] for k in (
            "bucket", "source_id", "source_version", "source_digest",
            "source_verified")} for b in _BUCKETS],
        "observed_by_bucket": {b: {k: indexed[b][k] for k in (
            "gross_margin", "stockout_events", "inventory_occupied",
            "labor_minutes")} for b in _BUCKETS},
        "treatment_pre": {k: tr_pre[k] for k in (
            "gross_margin", "stockout_events", "inventory_occupied",
            "labor_minutes")},
        "treatment_post": {k: tr_post[k] for k in (
            "gross_margin", "stockout_events", "inventory_occupied",
            "labor_minutes")},
        "all_source_verification_flags_are_self_reported": True,
    }


def evaluate_growth_pilot(
    plan: dict, *, pre_events: list[dict], post_events: list[dict],
    intervention_receipts: list[dict], guardrail_observations: list[dict],
    confound_observations: list[dict], source_attestations: dict,
    authorized: bool,
) -> dict:
    """Two-period, matched-group descriptive DID; causality is NEVER asserted.

    An externally confirmed 'authorized' flag is NOT a credential; this method
    is offline and cannot open a real pilot or report verified revenue.
    """
    if authorized is not True:
        _fail("growth_scope_denied")
    design = _check_plan(plan)
    pre_start, pre_end = _period(design, "pre_window")
    post_start, post_end = _period(design, "post_window")
    org, store = design["organization_id"], design["store_id"]
    reasons, receipt_lineage = _audit_receipts(plan, intervention_receipts)
    guardrail_reasons, guards = _operating_guardrails(guardrail_observations, plan)
    reasons += guardrail_reasons
    if not isinstance(confound_observations, list):
        _fail("growth_confounds_invalid")
    confound_map = {}
    for confound in confound_observations:
        if (not isinstance(confound, dict) or
                set(confound) != {"kind", "observed", "source_id", "note"}):
            _fail("growth_confounds_invalid")
        kind = confound["kind"]
        if kind not in design["confound_checklist"] or kind in confound_map:
            _fail("growth_confounds_invalid")
        if type(confound["observed"]) is not bool:
            _fail("growth_confounds_invalid")
        _required(confound["source_id"], "confound_source")
        _required(confound["note"], "confound_note")
        confound_map[kind] = confound
    if set(confound_map) != set(design["confound_checklist"]):
        reasons.append("confound_checklist_incomplete")
    elif any(item["observed"] for item in confound_map.values()):
        reasons.append("known_confounding_interference")

    # The evaluator MUST NOT invent a trusted source watermark from a time
    # window. Coverage/watermarks are external, caller-supplied evidence and
    # still unverified here; they never establish real production readiness.
    if (not isinstance(source_attestations, dict)
            or set(source_attestations) != set(_PERIODS)):
        _fail("growth_source_attestations_required")
    validated_sources = {}
    for period, end in (("pre", pre_end), ("post", post_end)):
        receipt = source_attestations[period]
        if not isinstance(receipt, dict) or set(receipt) != {
                "source_id", "source_watermark_at", "snapshot_at",
                "complete_history", "source_verified"}:
            _fail("growth_source_attestations_required")
        _required(receipt["source_id"], "source_id")
        if type(receipt["complete_history"]) is not bool or (
                type(receipt["source_verified"]) is not bool):
            _fail("growth_source_attestation_invalid")
        snap = _time(receipt["snapshot_at"])
        if snap < end:
            _fail("growth_snapshot_before_period_end")
        watermark = (None if receipt["source_watermark_at"] is None
                     else _time(receipt["source_watermark_at"]))
        if watermark is not None and watermark > snap:
            _fail("growth_watermark_after_snapshot")
        validated_sources[period] = {
            "source_id": receipt["source_id"], "snapshot_at": snap,
            "watermark": watermark,
            "complete_history": receipt["complete_history"],
            "source_verified": receipt["source_verified"],
        }
    if validated_sources["pre"]["snapshot_at"] != validated_sources["post"]["snapshot_at"]:
        _fail("growth_asof_snapshot_mismatch")
    projections = {}
    for period, rows, (start, end) in (
        ("pre", pre_events, (pre_start, pre_end)),
        ("post", post_events, (post_start, post_end)),
    ):
        # 2 input projections share a fixed snapshot after the observation
        # period. The source must prove full-history and watermarks externally.
        projections[period] = project_sales_facts(
            rows, organization_id=org, store_id=store,
            window_start=start.isoformat(), window_end=end.isoformat(),
            snapshot_at=validated_sources[period]["snapshot_at"].isoformat(),
            source_watermark_at=(
                validated_sources[period]["watermark"].isoformat()
                if validated_sources[period]["watermark"] is not None else None),
            complete_history=validated_sources[period]["complete_history"],
            authorized=True,
        )
        if projections[period]["facts"] is None:
            reasons.append(period + "_source_incomplete")
    if not isinstance(pre_events, list) or not isinstance(post_events, list):
        _fail("growth_source_batch_invalid")

    groups = {cohort: _grains(design[cohort]) for cohort in _COHORTS}
    currency = design["currency"]
    amount = {bucket: Decimal(0) for bucket in _BUCKETS}
    gross = {bucket: Decimal(0) for bucket in _BUCKETS}
    refund = {bucket: Decimal(0) for bucket in _BUCKETS}
    sample_ids = {bucket: set() for bucket in _BUCKETS}
    lineage = {}
    seen_events: set[str] = set()
    for period, raw in (("pre", pre_events), ("post", post_events)):
        projection = projections[period]
        source_versions = {
            x["event_id"]: x["source_version"]
            for x in projection["provenance"]["source_versions"]
        }
        # ANA-004 itself deduplicates by stable event_id and verifies same
        # source reference, version clock, refund/reversal chain and watermark.
        events = {x["event_id"]: x for x in projection["lineage"]}
        for event_id, meta in events.items():
            if event_id in seen_events:
                _fail("growth_repeated_event_across_periods")
            seen_events.add(event_id)
            candidates = [x for x in raw
                          if x.get("event_id") == event_id
                          and x.get("source_version") == source_versions[event_id]]
            if len(candidates) == 0:
                _fail("growth_event_unmatched")
            record = candidates[0]
            grain = tuple(record.get(x) for x in ("product_id", "spec_id", "unit"))
            owners = [cohort for cohort in _COHORTS if grain in groups[cohort]]
            if len(owners) != 1 or record.get("currency") != currency:
                _fail("growth_grain_or_currency_unmatched")
            bucket = owners[0] + "_" + period
            if meta["event_kind"] == "sale":
                sample_ids[bucket].add(event_id)
        if projection["facts"] is not None:
            for entry in projection["facts"]:
                grain = tuple(entry[x] for x in ("product_id", "spec_id", "unit"))
                owners = [cohort for cohort in _COHORTS if grain in groups[cohort]]
                if len(owners) != 1 or entry["currency"] != currency:
                    _fail("growth_grain_or_currency_unmatched")
                bucket = owners[0] + "_" + period
                amount[bucket] += Decimal(entry["net"])
                gross[bucket] += Decimal(entry["gross"])
                refund[bucket] += (
                    Decimal(entry["refunds"]) - Decimal(entry["reversed_refunds"]))
        lineage[period] = {
            "source_digest": projection["provenance"]["source_digest"],
            "recalculation_digest": projection["recalculation_digest"],
            "formula_digest": projection["provenance"]["formula_digest"],
            "formula_version": projection["provenance"]["formula_version"],
            "source_versions": projection["provenance"]["source_versions"],
            "snapshot_at": projection["provenance"]["snapshot_at"],
            "source_watermark_at": projection["provenance"]["source_watermark_at"],
            "source_attestation_id": validated_sources[period]["source_id"],
            "source_verification_claim_only": validated_sources[period]["source_verified"],
            "lineage": projection["lineage"],
        }
    # Recompute the refund guardrail from the same line-level ANA-004 source,
    # not from an untrusted self-declared percentage. If a period has only
    # refunds/reversals and no settled gross denominator, do not invent 0%.
    refund_bps: dict[str, Decimal | None] = {}
    for bucket in ("treatment_pre", "treatment_post"):
        refund_bps[bucket] = (
            refund[bucket] / gross[bucket] * Decimal(10000)
            if gross[bucket] > 0 and refund[bucket] >= 0 else None
        )
    if any(projections[p]["facts"] is None for p in _PERIODS):
        # A partial ledger is not a valid zero-refund or net-sales reading.
        refund_bps = {"treatment_pre": None, "treatment_post": None}
    if any(v is None for v in refund_bps.values()):
        reasons.append("refund_rate_denominator_unverified")
    elif (refund_bps["treatment_post"] - refund_bps["treatment_pre"]
          > _decimal(design["guardrail_limits"][
              "max_treatment_refund_rate_increase_bps"])):
        reasons.append("refund_rate_guardrail_failed")
    sample_floor = design["minimum_samples"]
    for bucket in _BUCKETS:
        if len(sample_ids[bucket]) < sample_floor:
            reasons.append("insufficient_sample_" + bucket)

    delta_t = amount["treatment_post"] - amount["treatment_pre"]
    delta_c = amount["control_post"] - amount["control_pre"]
    did = delta_t - delta_c
    if all(projections[p]["facts"] is not None for p in _PERIODS):
        if did < _decimal(design["min_descriptive_did"]):
            reasons.append("descriptive_uplift_below_preregistered_floor")

    source_complete = all(projections[p]["facts"] is not None for p in _PERIODS)
    def _sales_stats(period: str, cohort: str) -> dict:
        key = cohort + "_" + period
        return {"net_sales": (_money(amount[key]) if
                              projections[period]["facts"] is not None else None),
                "settled_sale_count": len(sample_ids[key])}
    report = {
        "contract_version": PILOT_CONTRACT_VERSION, "plan_digest": plan["plan_digest"],
        "pilot_id": design["pilot_id"], "organization_id": org, "store_id": store,
        "metric_id": "sales.paid_reconciled_candidate",
        "metric_version": METRIC_VERSION,
        "sales_formula_version": SALES_FORMULA_VERSION,
        "currency": currency,
        "cohorts": {cohort: {
            "pre": _sales_stats("pre", cohort),
            "post": _sales_stats("post", cohort),
        } for cohort in _COHORTS},
        "descriptive_treatment_change": _money(delta_t) if source_complete else None,
        "descriptive_control_change": _money(delta_c) if source_complete else None,
        "descriptive_difference_in_differences": _money(did) if source_complete else None,
        "uncertainty": {
            "description": "descriptive only; no randomization proof, parallel "
                           "trends proof, variance or causal confidence interval",
            "causal_effect": None, "confidence_interval": None,
            "causality_not_established": True,
            "observed_confounders": sorted(k for k, v in confound_map.items()
                                          if v["observed"]),
        },
        "guardrails": {
            **guards,
            "refund_rate_treatment_pre_bps": (
                format(refund_bps["treatment_pre"], ".4f")
                if refund_bps["treatment_pre"] is not None else None),
            "refund_rate_treatment_post_bps": (
                format(refund_bps["treatment_post"], ".4f")
                if refund_bps["treatment_post"] is not None else None),
            "refund_rate_formula": (
                "(settled_returns - reversed_returns) / settled_gross * 10000"),
        },
        "capability_gates": "not_verified_by_offline_evaluator",
        "manual_receipts": receipt_lineage,
        "source_lineage": lineage,
        "gate_reasons": sorted(set(reasons)),
        "synthetic_gate_result": "stop_or_review" if reasons else "descriptive_gates_met",
        "real_pilot_verified": False,
        "r032_achieved": False,
        "publishable_as_real_business_result": False,
        "next_action": "manual_reconcile_or_new_preregistered_hypothesis",
    }
    report["recalculation_digest"] = _hash(report)
    return report
