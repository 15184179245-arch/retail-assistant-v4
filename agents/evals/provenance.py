"""AGT-010: signed provenance for online eval results; synthetic data cannot relabel."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

KEY_ENV = "AGT_EVAL_PROVENANCE_KEY"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def results_digest(results: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(results).encode("utf-8")).hexdigest()


def sign(manifest: dict[str, Any], key: str) -> str:
    return hmac.new(key.encode("utf-8"), _canonical(manifest).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def validate_online(results: dict[str, dict[str, Any]],
                    metadata: dict[str, Any], cases_sha256: str) -> list[str]:
    key = os.environ.get(KEY_ENV)
    if not key:
        return ["online_provenance_key_missing"]
    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        return ["online_provenance_missing"]
    required = {"run_id", "collected_at", "model", "results_sha256",
                "eval_version", "prompt_version", "toolset_version",
                "cases_sha256", "signature"}
    if not required <= set(provenance):
        return ["online_provenance_incomplete"]
    for field in ("model", "eval_version", "prompt_version", "toolset_version"):
        if provenance.get(field) != metadata.get(field):
            return [f"online_provenance_{field}_mismatch"]
    if provenance.get("cases_sha256") != cases_sha256:
        return ["online_provenance_cases_mismatch"]
    if provenance.get("results_sha256") != results_digest(results):
        return ["online_provenance_results_mismatch"]
    manifest = {field: provenance[field] for field in
                ("run_id", "collected_at", "model", "eval_version",
                 "prompt_version", "toolset_version", "cases_sha256",
                 "results_sha256")}
    if metadata.get("collector_version") == "agt023-v1":
        extra = ("capture_sha256", "cost_ledger_sha256", "price_version",
                 "reserved_usd", "settled_usd", "calls_consumed")
        if any(field not in provenance for field in extra):
            return ["online_collector_provenance_incomplete"]
        manifest.update({field: provenance[field] for field in extra})
        for field in ("price_version", "reserved_usd", "settled_usd",
                      "calls_consumed"):
            if metadata.get(field) != provenance[field]:
                return ["online_collector_metadata_mismatch"]
        for field in ("capture_sha256", "cost_ledger_sha256"):
            value = provenance[field]
            if not isinstance(value, str) or len(value) != 64 or any(
                    char not in "0123456789abcdef" for char in value):
                return ["online_collector_digest_invalid"]
    expected = sign(manifest, key)
    if not isinstance(provenance.get("signature"), str) or not hmac.compare_digest(
            expected, provenance["signature"]):
        return ["online_provenance_signature_invalid"]
    return []
