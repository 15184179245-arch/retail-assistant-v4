"""Versioned SQLite sidecar store for the parallel derived catalog layer.

Invariants:
- `schema_version=1` must be explicitly created onto a NEW file that is distinct
  from any authoritative catalog database.
- `read_current_terms(...)` is strictly read-only (`mode=ro` + `PRAGMA query_only=ON`),
  never creates missing files, performs batched SQL (`WHERE product_id IN (...)`
  chunked by 400, never 1 query per product), and returns only completed or
  deterministic_only records whose scope, `snapshot_id`, `payload_sha256`, and
  span invariants match. Both rule-based (`origin="rule"`) and model-extracted
  (`origin="model"`) attributes can coexist across versions without overwriting
  each other's watermarks or losing provenance.
- Per-batch pre-call persistence (`reserve_batch_call`) and per-batch atomic
  commit (`finalize_batch_call`) prevent loss of already-paid results on
  cancellation/crash and block automatic replay of `in_flight` or `unknown`
  calls (`resume_blocked_idempotency_keys`).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterable, Sequence

from pydantic import ValidationError

from backend.app.modules.search.derived_catalog.models import (
    ALGORITHM_VERSION,
    NORMALIZATION_RULE_VERSION,
    PROMPT_VERSION,
    CandidateRelationRecord,
    DerivedAttributeRecord,
    ProductExtractionReceipt,
    SourceLookupItem,
)

SCHEMA_VERSION = 1
SQLITE_BATCH_CHUNK = 400
_SCIENTIFIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?[eE][+-]?\d+$")
_LEADING_ZERO_DIGITS_RE = re.compile(r"^0\d+$")
_DIMENSION_SEP_RE = re.compile(r"(?<=\d)\s*([×*ｘX])\s*(?=\d)")


class DerivedCatalogStoreError(ValueError):
    """Raised when sidecar target, schema, or scope invariants are violated."""


def _fold_fullwidth_ascii(text: str) -> str:
    """Fold ONLY fullwidth ASCII visible characters (U+FF01..U+FF5E) and ideographic space (U+3000).

    Never alters superscripts (`²`, `³`, `¹`), subscripts, circled digits (`①`),
    fractions (`½`), Roman numerals, or any non-ASCII symbols that NFKC would
    falsely turn into identifier digits.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def safe_normalize_value(value: str) -> tuple[str | None, str | None]:
    """Registered conservative comparison normalization (`derived-norm-v1`).

    Rules:
    - Never use general Unicode NFKC that transforms superscripts (`M²` -> `M2`),
      circled numbers (`①` -> `1`), or fractions (`½` -> `1/2`) into new digits.
    - Never expand scientific notation (e.g. `1.23E+12` stays untouched).
    - Never strip leading zeros (e.g. `00123` keeps `00`).
    - Never guess units or dimensions.
    - Strip outer whitespace and matching outer quotes (`"` or `'`) only if the
      resulting string is non-empty.
    - Fold only fullwidth ASCII (`！`..`～`) to halfwidth ASCII and normalize
      dimension separators (`×`, `*`, `ｘ`, `X`) between digits to `x`.
    """
    if not isinstance(value, str) or not value:
        return None, None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
        inner = trimmed[1:-1].strip()
        if inner:
            trimmed = inner
    if not trimmed:
        return None, None
    # Scientific notation must never be expanded or altered into numbers.
    if _SCIENTIFIC_RE.fullmatch(trimmed):
        return (trimmed, NORMALIZATION_RULE_VERSION) if trimmed != value else (None, None)
    # Leading-zero pure digit codes must keep their exact leading zeros.
    if _LEADING_ZERO_DIGITS_RE.fullmatch(trimmed):
        return (trimmed, NORMALIZATION_RULE_VERSION) if trimmed != value else (None, None)

    folded = _fold_fullwidth_ascii(trimmed)
    if _SCIENTIFIC_RE.fullmatch(folded) or _LEADING_ZERO_DIGITS_RE.fullmatch(folded):
        return (folded, NORMALIZATION_RULE_VERSION) if folded != value else (None, None)

    norm = _DIMENSION_SEP_RE.sub("x", folded)
    if not norm or norm == value:
        return None, None
    return norm, NORMALIZATION_RULE_VERSION


def extraction_version_key(
    *,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    return f"{model}|{prompt_version}|{algorithm_version}"


def _ensure_v1_ext_tables(conn: sqlite3.Connection) -> None:
    """Ensure batch_calls and runtime_leases tables exist on v1 sidecars (backward-compatible)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS batch_calls (
            call_id TEXT PRIMARY KEY,
            parent_call_id TEXT,
            organization_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            extraction_version TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            batch_input_digest TEXT NOT NULL,
            product_ids_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('in_flight', 'completed', 'failed')),
            usage_status TEXT NOT NULL CHECK (usage_status IN ('in_flight', 'known', 'unknown', 'not_called', 'failed')),
            reserved_output_tokens INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            error_code TEXT,
            rejected_proposals INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_batch_calls_scope_ver
        ON batch_calls (organization_id, store_id, extraction_version, status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_leases (
            organization_id TEXT NOT NULL,
            store_id TEXT NOT NULL,
            extraction_version TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (organization_id, store_id, extraction_version)
        )
        """
    )


def create_sidecar(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    source_db_path: str | Path | None = None,
    source_catalog_sha256: str | None = None,
    model: str = "deepseek-flash",
    prompt_version: str = PROMPT_VERSION,
    algorithm_version: str = ALGORITHM_VERSION,
) -> dict[str, object]:
    """Explicitly create a new `schema_version=1` sidecar SQLite database.

    Refuses existing files and refuses any path matching `source_db_path`.
    """
    target = Path(derived_db_path).resolve()
    if source_db_path is not None:
        source_resolved = Path(source_db_path).resolve()
        if source_resolved == target:
            raise DerivedCatalogStoreError("derived_sidecar_source_target_denied")
    if target.exists():
        raise DerivedCatalogStoreError("derived_sidecar_new_target_required")
    if not organization_id or not store_id:
        raise DerivedCatalogStoreError("derived_sidecar_scope_required")

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)

    conn = sqlite3.connect(target, timeout=10.0)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE extraction_receipts (
                organization_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                extraction_version TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('completed', 'checked_no_attributes', 'deterministic_only', 'failed', 'in_flight')
                ),
                usage_status TEXT NOT NULL CHECK (
                    usage_status IN ('known', 'unknown', 'not_called', 'failed', 'in_flight')
                ),
                input_tokens INTEGER,
                output_tokens INTEGER,
                error_code TEXT,
                call_id TEXT,
                parent_call_id TEXT,
                rejected_proposals INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (
                    organization_id,
                    store_id,
                    product_id,
                    snapshot_id,
                    payload_sha256,
                    extraction_version
                )
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX ix_extraction_receipts_lookup
            ON extraction_receipts (
                organization_id,
                store_id,
                product_id,
                snapshot_id,
                payload_sha256,
                status
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE derived_attributes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                extraction_version TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN (
                        'legacy_code',
                        'manufacturer_part_number',
                        'specification',
                        'model',
                        'brand',
                        'unclassified'
                    )
                ),
                value TEXT NOT NULL,
                source_field TEXT NOT NULL,
                start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
                end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
                normalized_value TEXT,
                normalization_version TEXT,
                certainty TEXT NOT NULL CHECK (certainty IN ('observed_field', 'candidate', 'ambiguous')),
                brand_scope TEXT,
                barcode_hint TEXT,
                model TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                algorithm_version TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                extraction_status TEXT NOT NULL CHECK (
                    extraction_status IN ('completed', 'checked_no_attributes', 'deterministic_only', 'failed', 'in_flight')
                ),
                usage_status TEXT NOT NULL CHECK (
                    usage_status IN ('known', 'unknown', 'not_called', 'failed', 'in_flight')
                ),
                input_tokens INTEGER,
                output_tokens INTEGER,
                error_code TEXT,
                UNIQUE (
                    organization_id,
                    store_id,
                    product_id,
                    snapshot_id,
                    payload_sha256,
                    extraction_version,
                    source_field,
                    start_offset,
                    end_offset,
                    kind,
                    certainty
                )
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX ix_derived_attributes_lookup
            ON derived_attributes (
                organization_id,
                store_id,
                product_id,
                snapshot_id,
                payload_sha256,
                extraction_version
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE candidate_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                extraction_version TEXT NOT NULL,
                relation_kind TEXT NOT NULL,
                shared_value TEXT NOT NULL,
                brand_scope TEXT,
                certainty TEXT NOT NULL CHECK (certainty IN ('candidate', 'ambiguous')),
                product_ids_json TEXT NOT NULL,
                snapshot_ids_json TEXT NOT NULL,
                evidence_fields_json TEXT NOT NULL,
                note TEXT NOT NULL
            )
            """
        )
        _ensure_v1_ext_tables(conn)
        ver = extraction_version_key(
            model=model,
            prompt_version=prompt_version,
            algorithm_version=algorithm_version,
        )
        meta: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "organization_id": organization_id,
            "store_id": store_id,
            "active_extraction_version": ver,
            "enabled_extraction_versions": [ver],
            "model": model,
            "prompt_version": prompt_version,
            "algorithm_version": algorithm_version,
            "source_catalog_sha256": source_catalog_sha256 or "",
            "watermark": "initialized",
            "completed_products": 0,
            "checked_no_attributes_products": 0,
            "deterministic_only_products": 0,
            "failed_products": 0,
            "unknown_frozen_products": 0,
        }
        conn.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()],
        )
        conn.commit()
        target.chmod(0o600)
        return meta
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_metadata(conn: sqlite3.Connection) -> dict[str, object]:
    rows = conn.execute("SELECT key, value FROM metadata").fetchall()
    if not rows:
        raise DerivedCatalogStoreError("derived_sidecar_metadata_missing")
    return {k: json.loads(v) for k, v in rows}


def _receipt_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(extraction_receipts)").fetchall()}


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def acquire_extraction_lease(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    extraction_version: str,
    owner_id: str,
    ttl_seconds: float = 60.0,
) -> None:
    """Acquire an exclusive CAS runtime lease in the sidecar to prevent concurrent duplicate runs."""
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        raise DerivedCatalogStoreError("derived_sidecar_missing")
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")
        _ensure_v1_ext_tables(conn)
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        exp_iso = datetime.fromtimestamp(now_dt.timestamp() + max(1.0, ttl_seconds), tz=timezone.utc).isoformat()
        existing = conn.execute(
            """
            SELECT owner_id, expires_at FROM runtime_leases
            WHERE organization_id = ? AND store_id = ? AND extraction_version = ?
            """,
            (organization_id, store_id, extraction_version),
        ).fetchone()
        if existing is not None:
            cur_owner, cur_exp = existing
            if str(cur_exp) > now_iso:
                raise DerivedCatalogStoreError("derived_extraction_lease_held")
        conn.execute(
            """
            INSERT OR REPLACE INTO runtime_leases (
                organization_id, store_id, extraction_version, owner_id, acquired_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (organization_id, store_id, extraction_version, owner_id, now_iso, exp_iso),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def release_extraction_lease(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    extraction_version: str,
    owner_id: str,
) -> None:
    """Release an active CAS runtime lease if still owned by `owner_id`."""
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        return
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _has_table(conn, "runtime_leases"):
            conn.execute(
                """
                DELETE FROM runtime_leases
                WHERE organization_id = ? AND store_id = ? AND extraction_version = ? AND owner_id = ?
                """,
                (organization_id, store_id, extraction_version, owner_id),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    finally:
        conn.close()


def freeze_abandoned_in_flight(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    extraction_version: str,
) -> int:
    """Convert any leftover `in_flight` receipts/calls from a prior crash/cancellation into `failed`/`unknown` (`needs_verification`).

    This guarantees that an interrupted in-flight request whose billing/result is
    unknown is never automatically re-dispatched on restart.
    """
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        return 0
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")
        _ensure_v1_ext_tables(conn)
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """
            UPDATE extraction_receipts
            SET status = 'failed',
                usage_status = 'unknown',
                error_code = COALESCE(error_code, 'abandoned_in_flight_unknown'),
                recorded_at = ?
            WHERE organization_id = ?
              AND store_id = ?
              AND extraction_version = ?
              AND (status = 'in_flight' OR usage_status = 'in_flight')
            """,
            (now_iso, organization_id, store_id, extraction_version),
        )
        frozen_count = int(cur.rowcount or 0)
        conn.execute(
            """
            UPDATE batch_calls
            SET status = 'failed',
                usage_status = 'unknown',
                error_code = COALESCE(error_code, 'abandoned_in_flight_unknown'),
                completed_at = ?
            WHERE organization_id = ?
              AND store_id = ?
              AND extraction_version = ?
              AND (status = 'in_flight' OR usage_status = 'in_flight')
            """,
            (now_iso, organization_id, store_id, extraction_version),
        )
        if frozen_count > 0:
            _recompute_watermarks(conn, organization_id=organization_id, store_id=store_id, active_ver=extraction_version)
        conn.execute("COMMIT")
        return frozen_count
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def completed_idempotency_keys(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    extraction_version: str,
) -> set[tuple[str, str, str]]:
    """Return `(product_id, snapshot_id, payload_sha256)` already completed for `extraction_version`."""
    state = inspect_recovery_state(
        derived_db_path,
        organization_id=organization_id,
        store_id=store_id,
        extraction_version=extraction_version,
    )
    return state["completed_keys"]  # type: ignore[return-value]


def inspect_recovery_state(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    extraction_version: str,
) -> dict[str, object]:
    """Read-only inspection of per-product and per-call recovery state for `extraction_version`.

    Separates:
    - `completed_keys`: `status IN ('completed', 'checked_no_attributes', 'deterministic_only')`
    - `unknown_frozen_keys`: `status = 'failed' AND usage_status IN ('unknown', 'in_flight')` or `status = 'in_flight'`
      (must NEVER be automatically replayed because a paid request may have been dispatched)
    - `retryable_failed_keys`: `status = 'failed' AND usage_status IN ('not_called', 'known', 'failed')`
    - Cumulative call & token usage across prior runs on this sidecar.
    """
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        raise DerivedCatalogStoreError("derived_sidecar_missing")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")

        cols = _receipt_columns(conn)
        call_col = "call_id" if "call_id" in cols else "NULL"
        rows = conn.execute(
            f"""
            SELECT product_id, snapshot_id, payload_sha256, status, usage_status, error_code, {call_col}
            FROM extraction_receipts
            WHERE organization_id = ?
              AND store_id = ?
              AND extraction_version = ?
            """,
            (organization_id, store_id, extraction_version),
        ).fetchall()

        completed_keys: set[tuple[str, str, str]] = set()
        unknown_frozen_keys: set[tuple[str, str, str]] = set()
        retryable_failed_keys: dict[tuple[str, str, str], dict[str, str | None]] = {}

        for pid, sid, psha, status, u_status, err_code, call_id in rows:
            key = (pid, sid, psha)
            if status in ("completed", "checked_no_attributes", "deterministic_only"):
                completed_keys.add(key)
            elif status == "in_flight" or u_status in ("unknown", "in_flight"):
                unknown_frozen_keys.add(key)
            else:
                retryable_failed_keys[key] = {
                    "status": status,
                    "usage_status": u_status,
                    "error_code": err_code,
                    "last_call_id": call_id,
                }

        prior_calls = 0
        prior_in_tokens = 0
        prior_out_tokens = 0
        prior_unknown_calls = 0
        if _has_table(conn, "batch_calls"):
            c_rows = conn.execute(
                """
                SELECT status, usage_status, input_tokens, output_tokens
                FROM batch_calls
                WHERE organization_id = ?
                  AND store_id = ?
                  AND extraction_version = ?
                """,
                (organization_id, store_id, extraction_version),
            ).fetchall()
            for c_status, c_usage, in_t, out_t in c_rows:
                if c_usage != "not_called":
                    prior_calls += 1
                if c_usage in ("unknown", "in_flight") or c_status == "in_flight":
                    prior_unknown_calls += 1
                prior_in_tokens += int(in_t or 0)
                prior_out_tokens += int(out_t or 0)

        return {
            "completed_keys": completed_keys,
            "unknown_frozen_keys": unknown_frozen_keys,
            "retryable_failed_keys": retryable_failed_keys,
            "prior_calls": prior_calls,
            "prior_input_tokens": prior_in_tokens,
            "prior_output_tokens": prior_out_tokens,
            "prior_unknown_calls": prior_unknown_calls,
        }
    except DerivedCatalogStoreError:
        raise
    except Exception:
        raise DerivedCatalogStoreError("derived_sidecar_invalid") from None
    finally:
        conn.close()


def _assert_current_lease(conn, organization_id, store_id, extraction_version, owner_id):
    row = conn.execute(
        "SELECT owner_id,expires_at FROM runtime_leases WHERE organization_id=? AND store_id=? AND extraction_version=?",
        (organization_id,store_id,extraction_version),
    ).fetchone()
    if row is None or row[0] != owner_id or row[1] <= datetime.now(timezone.utc).isoformat():
        raise DerivedCatalogStoreError("derived_extraction_lease_lost")


def reserve_batch_call(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    extraction_version: str,
    model: str,
    prompt_version: str,
    algorithm_version: str,
    call_id: str,
    parent_call_id: str | None = None,
    batch_items: Sequence[dict[str, str]],
    reserved_output_tokens: int,
    recorded_at: datetime,
    owner_id: str | None = None,
    cumulative_max_requests: int | None = None,
    cumulative_max_total_tokens: int | None = None,
) -> bool:
    """Atomically claim a batch of products and persist `in_flight` call & receipts BEFORE calling the provider.

    Returns `True` if all items in `batch_items` were claimed, or `False` if any
    item was already completed, `in_flight`, or `unknown` frozen by another
    worker/process.
    """
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        raise DerivedCatalogStoreError("derived_sidecar_missing")
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")
        _ensure_v1_ext_tables(conn)

        if owner_id is not None:
            _assert_current_lease(conn,organization_id,store_id,extraction_version,owner_id)
        # Reserve the enforced input cap as well as the output cap. Unknown
        # calls retain their reservation; known outcomes release unused tokens.
        calls,charged = conn.execute(
            """SELECT count(*),COALESCE(sum(CASE
              WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND usage_status='known'
                THEN input_tokens+output_tokens
              ELSE 32768+reserved_output_tokens END),0)
            FROM batch_calls WHERE organization_id=? AND store_id=?
              AND extraction_version=? AND usage_status!='not_called'""",
            (organization_id,store_id,extraction_version),
        ).fetchone()
        # Pre-journal v1/manual receipts have no batch ledger. Treat each
        # receipt as a conservative call bound; do not reset old paid usage.
        cols=_receipt_columns(conn)
        orphan="(r.call_id IS NULL OR NOT EXISTS (SELECT 1 FROM batch_calls b WHERE b.call_id=r.call_id))" if 'call_id' in cols else '1=1'
        legacy_calls,legacy_tokens=conn.execute(
            "SELECT count(*),COALESCE(sum(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND usage_status='known' "
            "THEN input_tokens+output_tokens ELSE 36864 END),0) FROM extraction_receipts r "
            "WHERE organization_id=? AND store_id=? AND extraction_version=? AND usage_status!='not_called' AND "+orphan,
            (organization_id,store_id,extraction_version),
        ).fetchone()
        calls+=legacy_calls;charged+=legacy_tokens
        if cumulative_max_requests is not None and calls >= cumulative_max_requests:
            raise DerivedCatalogStoreError("derived_request_budget_exhausted")
        if cumulative_max_total_tokens is not None and charged+32768+reserved_output_tokens > cumulative_max_total_tokens:
            raise DerivedCatalogStoreError("derived_token_budget_exhausted")

        # Check that none of the batch items are already completed, in_flight, or unknown-frozen
        for item in batch_items:
            row = conn.execute(
                """
                SELECT status, usage_status
                FROM extraction_receipts
                WHERE organization_id = ?
                  AND store_id = ?
                  AND product_id = ?
                  AND snapshot_id = ?
                  AND payload_sha256 = ?
                  AND extraction_version = ?
                """,
                (
                    organization_id,
                    store_id,
                    item["product_id"],
                    item["snapshot_id"],
                    item["payload_sha256"],
                    extraction_version,
                ),
            ).fetchone()
            if row is not None:
                st, ust = row
                if st in ("completed", "checked_no_attributes", "deterministic_only", "in_flight") or ust in ("unknown", "in_flight"):
                    conn.execute("ROLLBACK")
                    return False

        from hashlib import sha256
        batch_digest = sha256(
            json.dumps([i["input_digest"] for i in batch_items], separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        ts_iso = recorded_at.isoformat()

        conn.execute(
            """
            INSERT INTO batch_calls (
                call_id,
                parent_call_id,
                organization_id,
                store_id,
                extraction_version,
                model,
                prompt_version,
                algorithm_version,
                batch_input_digest,
                product_ids_json,
                status,
                usage_status,
                reserved_output_tokens,
                input_tokens,
                output_tokens,
                error_code,
                rejected_proposals,
                started_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', 'in_flight', ?, NULL, NULL, NULL, 0, ?, NULL)
            """,
            (
                call_id,
                parent_call_id,
                organization_id,
                store_id,
                extraction_version,
                model,
                prompt_version,
                algorithm_version,
                batch_digest,
                json.dumps([i["product_id"] for i in batch_items], ensure_ascii=False),
                reserved_output_tokens,
                ts_iso,
            ),
        )

        cols = _receipt_columns(conn)
        has_call_cols = "call_id" in cols and "parent_call_id" in cols
        for item in batch_items:
            conn.execute(
                """
                DELETE FROM extraction_receipts
                WHERE organization_id = ?
                  AND store_id = ?
                  AND product_id = ?
                  AND snapshot_id = ?
                  AND payload_sha256 = ?
                  AND extraction_version = ?
                """,
                (
                    organization_id,
                    store_id,
                    item["product_id"],
                    item["snapshot_id"],
                    item["payload_sha256"],
                    extraction_version,
                ),
            )
            if has_call_cols:
                conn.execute(
                    """
                    INSERT INTO extraction_receipts (
                        organization_id, store_id, product_id, snapshot_id, payload_sha256,
                        extraction_version, model, prompt_version, algorithm_version, input_digest,
                        status, usage_status, input_tokens, output_tokens, error_code,
                        call_id, parent_call_id, rejected_proposals, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', 'in_flight', NULL, NULL, NULL, ?, ?, 0, ?)
                    """,
                    (
                        organization_id,
                        store_id,
                        item["product_id"],
                        item["snapshot_id"],
                        item["payload_sha256"],
                        extraction_version,
                        model,
                        prompt_version,
                        algorithm_version,
                        item["input_digest"],
                        call_id,
                        parent_call_id,
                        ts_iso,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO extraction_receipts (
                        organization_id, store_id, product_id, snapshot_id, payload_sha256,
                        extraction_version, model, prompt_version, algorithm_version, input_digest,
                        status, usage_status, input_tokens, output_tokens, error_code,
                        rejected_proposals, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_flight', 'in_flight', NULL, NULL, NULL, 0, ?)
                    """,
                    (
                        organization_id,
                        store_id,
                        item["product_id"],
                        item["snapshot_id"],
                        item["payload_sha256"],
                        extraction_version,
                        model,
                        prompt_version,
                        algorithm_version,
                        item["input_digest"],
                        ts_iso,
                    ),
                )
        conn.execute("COMMIT")
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def finalize_batch_call(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    call_id: str,
    call_status: str,
    usage_status: str,
    input_tokens: int | None,
    output_tokens: int | None,
    error_code: str | None,
    rejected_proposals: int,
    receipts: Sequence[ProductExtractionReceipt],
    set_active_version: str | None = None,
    owner_id: str | None = None,
) -> dict[str, int]:
    """Atomically commit a single batch call's outcome and product receipts immediately upon completion/failure."""
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        raise DerivedCatalogStoreError("derived_sidecar_missing")
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")
        _ensure_v1_ext_tables(conn)
        call = conn.execute(
            "SELECT extraction_version,product_ids_json FROM batch_calls WHERE call_id=? AND organization_id=? AND store_id=?",
            (call_id,organization_id,store_id),
        ).fetchone()
        if call is None:raise DerivedCatalogStoreError("derived_call_missing")
        if owner_id is not None:
            _assert_current_lease(conn,organization_id,store_id,call[0],owner_id)
        allowed=set(json.loads(call[1]))
        if {r.product_id for r in receipts}!=allowed or any(r.call_id!=call_id or r.extraction_version!=call[0] for r in receipts):
            raise DerivedCatalogStoreError("derived_call_receipt_mismatch")
        now_iso = datetime.now(timezone.utc).isoformat()
        updated = conn.execute(
            """
            UPDATE batch_calls
            SET status = ?,
                usage_status = ?,
                input_tokens = ?,
                output_tokens = ?,
                error_code = ?,
                rejected_proposals = ?,
                completed_at = ?
            WHERE call_id = ? AND organization_id = ? AND store_id = ?
              AND status = 'in_flight' AND usage_status = 'in_flight'
            """,
            (
                call_status,
                usage_status,
                input_tokens,
                output_tokens,
                error_code,
                rejected_proposals,
                now_iso,
                call_id,organization_id,store_id,
            ),
        )
        if updated.rowcount!=1:raise DerivedCatalogStoreError("derived_call_not_in_flight")
        res = _write_receipts_and_relations_in_tx(
            conn,
            meta=meta,
            organization_id=organization_id,
            store_id=store_id,
            receipts=receipts,
            relations=(),
            set_active_version=set_active_version,
            allow_replace_in_flight=True,
        )
        conn.execute("COMMIT")
        path.chmod(0o600)
        return res
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _recompute_watermarks(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    store_id: str,
    active_ver: str,
) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT status, usage_status, COUNT(*)
        FROM extraction_receipts
        WHERE organization_id = ? AND store_id = ? AND extraction_version = ?
        GROUP BY status, usage_status
        """,
        (organization_id, store_id, active_ver),
    ).fetchall()
    completed_n = 0
    checked_no_attr_n = 0
    det_n = 0
    failed_n = 0
    unknown_frozen_n = 0
    for st, ust, cnt in rows:
        n = int(cnt)
        if st == "completed":
            completed_n += n
        elif st == "checked_no_attributes":
            checked_no_attr_n += n
        elif st == "deterministic_only":
            det_n += n
        elif st == "in_flight" or ust in ("unknown", "in_flight"):
            failed_n += n
            unknown_frozen_n += n
        elif st == "failed":
            failed_n += n

    if failed_n > 0 and (completed_n + checked_no_attr_n + det_n) > 0:
        watermark = "partial_with_failures"
    elif failed_n > 0:
        watermark = "failed"
    elif det_n > 0 and (completed_n + checked_no_attr_n) == 0:
        watermark = "partial_deterministic"
    elif (completed_n + checked_no_attr_n) > 0:
        watermark = "completed"
    else:
        watermark = "initialized"

    for k, v in (
        ("completed_products", completed_n),
        ("checked_no_attributes_products", checked_no_attr_n),
        ("deterministic_only_products", det_n),
        ("failed_products", failed_n),
        ("unknown_frozen_products", unknown_frozen_n),
        ("watermark", watermark),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (k, json.dumps(v, ensure_ascii=False)),
        )
    return {
        "completed_products": completed_n,
        "checked_no_attributes_products": checked_no_attr_n,
        "deterministic_only_products": det_n,
        "failed_products": failed_n,
        "unknown_frozen_products": unknown_frozen_n,
    }


def _write_receipts_and_relations_in_tx(
    conn: sqlite3.Connection,
    *,
    meta: dict[str, object],
    organization_id: str,
    store_id: str,
    receipts: Iterable[ProductExtractionReceipt],
    relations: Iterable[CandidateRelationRecord] = (),
    set_active_version: str | None = None,
    allow_replace_in_flight: bool = False,
) -> dict[str, int]:
    inserted_receipts = 0
    inserted_attributes = 0
    cols = _receipt_columns(conn)
    has_call_cols = "call_id" in cols and "parent_call_id" in cols
    versions_touched: set[str] = set()

    for receipt in receipts:
        if receipt.organization_id != organization_id or receipt.store_id != store_id:
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")
        versions_touched.add(receipt.extraction_version)
        existing = conn.execute(
            """
            SELECT status, usage_status FROM extraction_receipts
            WHERE organization_id = ?
              AND store_id = ?
              AND product_id = ?
              AND snapshot_id = ?
              AND payload_sha256 = ?
              AND extraction_version = ?
            """,
            (
                receipt.organization_id,
                receipt.store_id,
                receipt.product_id,
                receipt.snapshot_id,
                receipt.payload_sha256,
                receipt.extraction_version,
            ),
        ).fetchone()
        if existing is not None:
            ex_status, ex_usage = existing
            if ex_status in ("completed", "checked_no_attributes", "deterministic_only"):
                if ex_status in ("completed", "checked_no_attributes") or receipt.status == "deterministic_only":
                    continue
            if not allow_replace_in_flight and (ex_status == "in_flight" or ex_usage in ("unknown", "in_flight")):
                # Never overwrite an unknown-frozen or active in-flight receipt from outside its own call finalizer
                continue

            conn.execute(
                """
                DELETE FROM derived_attributes
                WHERE organization_id = ?
                  AND store_id = ?
                  AND product_id = ?
                  AND snapshot_id = ?
                  AND payload_sha256 = ?
                  AND extraction_version = ?
                """,
                (
                    receipt.organization_id,
                    receipt.store_id,
                    receipt.product_id,
                    receipt.snapshot_id,
                    receipt.payload_sha256,
                    receipt.extraction_version,
                ),
            )
            conn.execute(
                """
                DELETE FROM extraction_receipts
                WHERE organization_id = ?
                  AND store_id = ?
                  AND product_id = ?
                  AND snapshot_id = ?
                  AND payload_sha256 = ?
                  AND extraction_version = ?
                """,
                (
                    receipt.organization_id,
                    receipt.store_id,
                    receipt.product_id,
                    receipt.snapshot_id,
                    receipt.payload_sha256,
                    receipt.extraction_version,
                ),
            )

        if has_call_cols:
            conn.execute(
                """
                INSERT INTO extraction_receipts (
                    organization_id,
                    store_id,
                    product_id,
                    snapshot_id,
                    payload_sha256,
                    extraction_version,
                    model,
                    prompt_version,
                    algorithm_version,
                    input_digest,
                    status,
                    usage_status,
                    input_tokens,
                    output_tokens,
                    error_code,
                    call_id,
                    parent_call_id,
                    rejected_proposals,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.organization_id,
                    receipt.store_id,
                    receipt.product_id,
                    receipt.snapshot_id,
                    receipt.payload_sha256,
                    receipt.extraction_version,
                    receipt.model,
                    receipt.prompt_version,
                    receipt.algorithm_version,
                    receipt.input_digest,
                    receipt.status,
                    receipt.usage_status,
                    receipt.input_tokens,
                    receipt.output_tokens,
                    receipt.error_code,
                    receipt.call_id,
                    receipt.parent_call_id,
                    receipt.rejected_proposals,
                    receipt.recorded_at.isoformat(),
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO extraction_receipts (
                    organization_id,
                    store_id,
                    product_id,
                    snapshot_id,
                    payload_sha256,
                    extraction_version,
                    model,
                    prompt_version,
                    algorithm_version,
                    input_digest,
                    status,
                    usage_status,
                    input_tokens,
                    output_tokens,
                    error_code,
                    rejected_proposals,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.organization_id,
                    receipt.store_id,
                    receipt.product_id,
                    receipt.snapshot_id,
                    receipt.payload_sha256,
                    receipt.extraction_version,
                    receipt.model,
                    receipt.prompt_version,
                    receipt.algorithm_version,
                    receipt.input_digest,
                    receipt.status,
                    receipt.usage_status,
                    receipt.input_tokens,
                    receipt.output_tokens,
                    receipt.error_code,
                    receipt.rejected_proposals,
                    receipt.recorded_at.isoformat(),
                ),
            )
        inserted_receipts += 1

        if receipt.status in ("completed", "checked_no_attributes", "deterministic_only"):
            for attr in receipt.attributes:
                if (
                    attr.organization_id != organization_id
                    or attr.store_id != store_id
                    or attr.product_id != receipt.product_id
                    or attr.snapshot_id != receipt.snapshot_id
                    or attr.payload_sha256 != receipt.payload_sha256
                ):
                    raise DerivedCatalogStoreError("derived_attribute_identity_mismatch")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO derived_attributes (
                        organization_id,
                        store_id,
                        product_id,
                        snapshot_id,
                        payload_sha256,
                        extraction_version,
                        kind,
                        value,
                        source_field,
                        start_offset,
                        end_offset,
                        normalized_value,
                        normalization_version,
                        certainty,
                        brand_scope,
                        barcode_hint,
                        model,
                        prompt_version,
                        algorithm_version,
                        input_digest,
                        extraction_status,
                        usage_status,
                        input_tokens,
                        output_tokens,
                        error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attr.organization_id,
                        attr.store_id,
                        attr.product_id,
                        attr.snapshot_id,
                        attr.payload_sha256,
                        receipt.extraction_version,
                        attr.kind,
                        attr.value,
                        attr.source_field,
                        attr.start,
                        attr.end,
                        attr.normalized_value,
                        attr.normalization_version,
                        attr.certainty,
                        attr.brand_scope,
                        attr.barcode_hint,
                        attr.model,
                        attr.prompt_version,
                        attr.algorithm_version,
                        attr.input_digest,
                        attr.extraction_status,
                        attr.usage_status,
                        attr.input_tokens,
                        attr.output_tokens,
                        attr.error_code,
                    ),
                )
                inserted_attributes += 1

    active_ver = set_active_version or str(meta.get("active_extraction_version") or "")
    if set_active_version is not None:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('active_extraction_version', ?)",
            (json.dumps(set_active_version, ensure_ascii=False),),
        )

    # Maintain enabled_extraction_versions list so multi-version rule + model results coexist
    enabled_raw = meta.get("enabled_extraction_versions")
    enabled_vers: list[str] = list(enabled_raw) if isinstance(enabled_raw, list) else []
    if active_ver and active_ver not in enabled_vers:
        enabled_vers.append(active_ver)
    for v in sorted(versions_touched):
        if v and v not in enabled_vers:
            enabled_vers.append(v)
    if enabled_vers:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('enabled_extraction_versions', ?)",
            (json.dumps(enabled_vers, ensure_ascii=False),),
        )

    relation_list = list(relations)
    if relation_list:
        conn.execute(
            "DELETE FROM candidate_relations WHERE organization_id = ? AND store_id = ? AND extraction_version = ?",
            (organization_id, store_id, active_ver),
        )
        for rel in relation_list:
            if rel.organization_id != organization_id or rel.store_id != store_id:
                raise DerivedCatalogStoreError("derived_relation_scope_mismatch")
            conn.execute(
                """
                INSERT INTO candidate_relations (
                    organization_id,
                    store_id,
                    extraction_version,
                    relation_kind,
                    shared_value,
                    brand_scope,
                    certainty,
                    product_ids_json,
                    snapshot_ids_json,
                    evidence_fields_json,
                    note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rel.organization_id,
                    rel.store_id,
                    active_ver,
                    rel.relation_kind,
                    rel.shared_value,
                    rel.brand_scope,
                    rel.certainty,
                    json.dumps(rel.product_ids, ensure_ascii=False),
                    json.dumps(rel.snapshot_ids, ensure_ascii=False),
                    json.dumps(rel.evidence_fields, ensure_ascii=False),
                    rel.note,
                ),
            )

    wm = _recompute_watermarks(conn, organization_id=organization_id, store_id=store_id, active_ver=active_ver)
    return {
        "inserted_receipts": inserted_receipts,
        "inserted_attributes": inserted_attributes,
        **wm,
    }


def record_extractions(
    derived_db_path: str | Path,
    *,
    organization_id: str,
    store_id: str,
    receipts: Iterable[ProductExtractionReceipt],
    relations: Iterable[CandidateRelationRecord] = (),
    set_active_version: str | None = None,
) -> dict[str, int]:
    """Persist product extraction receipts and verified attributes into an existing sidecar."""
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        raise DerivedCatalogStoreError("derived_sidecar_missing")

    conn = sqlite3.connect(path, timeout=10.0)
    try:
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")
        _ensure_v1_ext_tables(conn)
        res = _write_receipts_and_relations_in_tx(
            conn,
            meta=meta,
            organization_id=organization_id,
            store_id=store_id,
            receipts=receipts,
            relations=relations,
            set_active_version=set_active_version,
            allow_replace_in_flight=False,
        )
        conn.commit()
        path.chmod(0o600)
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def merge_sidecars(
    target_db_path: str | Path,
    *,
    source_sidecar_paths: Sequence[str | Path],
    organization_id: str,
    store_id: str,
    source_db_path: str | Path | None = None,
    source_catalog_sha256: str | None = None,
    active_extraction_version: str | None = None,
) -> dict[str, object]:
    """Explicitly merge multiple sidecars (e.g., rule sidecar + model sidecars) into a new sidecar.

    Preserves exact provenance (`extraction_version`, `model`, `prompt_version`,
    `algorithm_version`, `status`, `usage_status`, `input_tokens`, `output_tokens`,
    `call_id`) without relabeling one model's records as another's or letting
    deterministic rule records overwrite model pending/completed watermarks.
    """
    if not source_sidecar_paths:
        raise ValueError("source_sidecar_paths_required")

    create_sidecar(
        target_db_path,
        organization_id=organization_id,
        store_id=store_id,
        source_db_path=source_db_path,
        source_catalog_sha256=source_catalog_sha256,
    )
    target = Path(target_db_path).resolve()
    target_conn = sqlite3.connect(target, timeout=10.0)
    try:
        enabled_versions: list[str] = []
        copied_receipts = 0
        copied_attributes = 0
        copied_calls = 0

        for src_p in source_sidecar_paths:
            src_path = Path(src_p).resolve()
            if not src_path.is_file():
                raise DerivedCatalogStoreError("derived_sidecar_missing")
            src_conn = sqlite3.connect(src_path.as_uri() + "?mode=ro", uri=True)
            src_conn.execute("PRAGMA query_only=ON")
            try:
                s_meta = _load_metadata(src_conn)
                if (
                    s_meta.get("schema_version") != SCHEMA_VERSION
                    or s_meta.get("organization_id") != organization_id
                    or s_meta.get("store_id") != store_id
                ):
                    raise DerivedCatalogStoreError("derived_sidecar_scope_mismatch")

                s_cols = _receipt_columns(src_conn)
                has_call = "call_id" in s_cols and "parent_call_id" in s_cols
                r_sql = (
                    """
                    SELECT organization_id, store_id, product_id, snapshot_id, payload_sha256,
                           extraction_version, model, prompt_version, algorithm_version, input_digest,
                           status, usage_status, input_tokens, output_tokens, error_code,
                           call_id, parent_call_id, rejected_proposals, recorded_at
                    FROM extraction_receipts
                    WHERE organization_id = ? AND store_id = ?
                    """
                    if has_call
                    else """
                    SELECT organization_id, store_id, product_id, snapshot_id, payload_sha256,
                           extraction_version, model, prompt_version, algorithm_version, input_digest,
                           status, usage_status, input_tokens, output_tokens, error_code,
                           NULL AS call_id, NULL AS parent_call_id, rejected_proposals, recorded_at
                    FROM extraction_receipts
                    WHERE organization_id = ? AND store_id = ?
                    """
                )
                for r_row in src_conn.execute(r_sql, (organization_id, store_id)).fetchall():
                    ext_ver = r_row[5]
                    if ext_ver not in enabled_versions:
                        enabled_versions.append(ext_ver)
                    cur = target_conn.execute(
                        """
                        INSERT OR IGNORE INTO extraction_receipts (
                            organization_id, store_id, product_id, snapshot_id, payload_sha256,
                            extraction_version, model, prompt_version, algorithm_version, input_digest,
                            status, usage_status, input_tokens, output_tokens, error_code,
                            call_id, parent_call_id, rejected_proposals, recorded_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        r_row,
                    )
                    copied_receipts += int(cur.rowcount or 0)

                for a_row in src_conn.execute(
                    """
                    SELECT organization_id, store_id, product_id, snapshot_id, payload_sha256,
                           extraction_version, kind, value, source_field, start_offset, end_offset,
                           normalized_value, normalization_version, certainty, brand_scope, barcode_hint,
                           model, prompt_version, algorithm_version, input_digest,
                           extraction_status, usage_status, input_tokens, output_tokens, error_code
                    FROM derived_attributes
                    WHERE organization_id = ? AND store_id = ?
                    """,
                    (organization_id, store_id),
                ).fetchall():
                    cur = target_conn.execute(
                        """
                        INSERT OR IGNORE INTO derived_attributes (
                            organization_id, store_id, product_id, snapshot_id, payload_sha256,
                            extraction_version, kind, value, source_field, start_offset, end_offset,
                            normalized_value, normalization_version, certainty, brand_scope, barcode_hint,
                            model, prompt_version, algorithm_version, input_digest,
                            extraction_status, usage_status, input_tokens, output_tokens, error_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        a_row,
                    )
                    copied_attributes += int(cur.rowcount or 0)

                if _has_table(src_conn, "batch_calls"):
                    for c_row in src_conn.execute(
                        """
                        SELECT call_id, parent_call_id, organization_id, store_id, extraction_version,
                               model, prompt_version, algorithm_version, batch_input_digest, product_ids_json,
                               status, usage_status, reserved_output_tokens, input_tokens, output_tokens,
                               error_code, rejected_proposals, started_at, completed_at
                        FROM batch_calls
                        WHERE organization_id = ? AND store_id = ?
                        """,
                        (organization_id, store_id),
                    ).fetchall():
                        cur = target_conn.execute(
                            """
                            INSERT OR IGNORE INTO batch_calls (
                                call_id, parent_call_id, organization_id, store_id, extraction_version,
                                model, prompt_version, algorithm_version, batch_input_digest, product_ids_json,
                                status, usage_status, reserved_output_tokens, input_tokens, output_tokens,
                                error_code, rejected_proposals, started_at, completed_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            c_row,
                        )
                        copied_calls += int(cur.rowcount or 0)

                for rel_row in src_conn.execute(
                    """
                    SELECT organization_id, store_id, extraction_version, relation_kind,
                           shared_value, brand_scope, certainty, product_ids_json,
                           snapshot_ids_json, evidence_fields_json, note
                    FROM candidate_relations
                    WHERE organization_id = ? AND store_id = ?
                    """,
                    (organization_id, store_id),
                ).fetchall():
                    target_conn.execute(
                        """
                        INSERT INTO candidate_relations (
                            organization_id, store_id, extraction_version, relation_kind,
                            shared_value, brand_scope, certainty, product_ids_json,
                            snapshot_ids_json, evidence_fields_json, note
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rel_row,
                    )
            finally:
                src_conn.close()

        effective_active = active_extraction_version or (enabled_versions[-1] if enabled_versions else "")
        target_conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('active_extraction_version', ?)",
            (json.dumps(effective_active, ensure_ascii=False),),
        )
        target_conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('enabled_extraction_versions', ?)",
            (json.dumps(enabled_versions, ensure_ascii=False),),
        )
        wm = _recompute_watermarks(
            target_conn,
            organization_id=organization_id,
            store_id=store_id,
            active_ver=effective_active,
        )
        target_conn.commit()
        target.chmod(0o600)
        return {
            "target_sidecar": str(target),
            "enabled_extraction_versions": enabled_versions,
            "active_extraction_version": effective_active,
            "copied_receipts": copied_receipts,
            "copied_attributes": copied_attributes,
            "copied_calls": copied_calls,
            **wm,
        }
    except Exception:
        target_conn.rollback()
        raise
    finally:
        target_conn.close()


def read_current_terms(
    derived_db_path,
    *,
    organization_id: str,
    store_id: str,
    sources: list[dict],
) -> dict[str, list[dict]]:
    """Batch-read current derived attributes verified against exact source snapshots.

    Contract guarantees:
    - Each item in `sources` must have exactly `product_id`, `snapshot_id`,
      and `payload_sha256` (validated via `SourceLookupItem` with `extra="forbid"`).
    - Strictly read-only: opens `derived_db_path` with `mode=ro` and
      `PRAGMA query_only=ON`. Never creates a missing file or writes to disk.
    - Missing sidecar, corrupted sidecar, schema mismatch, or scope mismatch
      returns `{}` (fail-closed empty result without side effects) so caller
      search paths never crash or leak cross-scope/stale data.
    - Uses batched SQL (`WHERE product_id IN (...)` in chunks of 400), never
      one SQL query per product.
    - Returns verified attributes from all enabled extraction versions (both
      rule-derived `origin="rule"` and model-derived `origin="model"`) whose
      receipt has `status IN ('completed', 'checked_no_attributes', 'deterministic_only')`
      and matches `(organization_id, store_id, product_id, snapshot_id, payload_sha256)`.
      Failed/in-flight outcomes are never returned. A completed platform result
      with unknown token usage is distinct from an unknown outcome and may be returned.
    """
    if (
        not isinstance(organization_id, str)
        or not organization_id.strip()
        or not isinstance(store_id, str)
        or not store_id.strip()
        or not isinstance(sources, list)
    ):
        raise ValueError("invalid_read_current_terms_arguments")

    if not sources:
        return {}

    validated_sources: dict[str, SourceLookupItem] = {}
    for raw_item in sources:
        if not isinstance(raw_item, dict):
            raise ValueError("invalid_source_lookup_item")
        try:
            item = SourceLookupItem.model_validate(raw_item)
        except ValidationError as exc:
            raise ValueError("invalid_source_lookup_item") from exc
        if item.product_id in validated_sources and validated_sources[item.product_id] != item:
            raise ValueError("conflicting_duplicate_source_product_id")
        validated_sources[item.product_id] = item

    if derived_db_path is None or not isinstance(derived_db_path, (str, Path)):
        return {}
    path = Path(derived_db_path).resolve()
    if not path.is_file():
        return {}

    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    try:
        meta = _load_metadata(conn)
        if (
            meta.get("schema_version") != SCHEMA_VERSION
            or meta.get("organization_id") != organization_id
            or meta.get("store_id") != store_id
        ):
            return {}
        active_ver = meta.get("active_extraction_version")
        enabled_raw = meta.get("enabled_extraction_versions")
        enabled_vers: list[str] = []
        if isinstance(enabled_raw, list):
            enabled_vers = [v for v in enabled_raw if isinstance(v, str) and v]
        if isinstance(active_ver, str) and active_ver and active_ver not in enabled_vers:
            enabled_vers.append(active_ver)
        if not enabled_vers:
            return {}

        product_ids = list(validated_sources.keys())
        result: dict[str, list[dict]] = {}
        ver_placeholders = ",".join("?" for _ in enabled_vers)

        for offset in range(0, len(product_ids), SQLITE_BATCH_CHUNK):
            chunk_ids = product_ids[offset : offset + SQLITE_BATCH_CHUNK]
            id_placeholders = ",".join("?" for _ in chunk_ids)
            sql = f"""
                SELECT
                    a.product_id,
                    a.organization_id,
                    a.store_id,
                    a.snapshot_id,
                    a.payload_sha256,
                    a.extraction_version,
                    a.kind,
                    a.value,
                    a.source_field,
                    a.start_offset,
                    a.end_offset,
                    a.normalized_value,
                    a.normalization_version,
                    a.certainty,
                    a.brand_scope,
                    a.barcode_hint,
                    a.model,
                    a.prompt_version,
                    a.algorithm_version,
                    a.input_digest,
                    a.extraction_status,
                    a.usage_status,
                    a.input_tokens,
                    a.output_tokens,
                    a.error_code
                FROM derived_attributes a
                JOIN extraction_receipts r
                  ON r.organization_id = a.organization_id
                 AND r.store_id = a.store_id
                 AND r.product_id = a.product_id
                 AND r.snapshot_id = a.snapshot_id
                 AND r.payload_sha256 = a.payload_sha256
                 AND r.extraction_version = a.extraction_version
                WHERE a.organization_id = ?
                  AND a.store_id = ?
                  AND a.extraction_version IN ({ver_placeholders})
                  AND r.status IN ('completed', 'checked_no_attributes', 'deterministic_only')
                  AND a.extraction_status IN ('completed', 'checked_no_attributes', 'deterministic_only')
                  AND a.product_id IN ({id_placeholders})
                ORDER BY
                    a.product_id,
                    CASE WHEN a.extraction_status = 'completed' THEN 0 ELSE 1 END,
                    a.id
            """
            params = [organization_id, store_id, *enabled_vers, *chunk_ids]
            for row in conn.execute(sql, params).fetchall():
                (
                    pid,
                    org_id,
                    st_id,
                    snap_id,
                    psha,
                    ext_ver,
                    kind,
                    val,
                    sfield,
                    start_off,
                    end_off,
                    norm_val,
                    norm_ver,
                    certainty,
                    brand_scope,
                    barcode_hint,
                    model_name,
                    prompt_ver,
                    alg_ver,
                    in_digest,
                    ext_status,
                    use_status,
                    in_tok,
                    out_tok,
                    err_code,
                ) = row
                expected = validated_sources.get(pid)
                if expected is None:
                    continue
                if (
                    org_id != organization_id
                    or st_id != store_id
                    or snap_id != expected.snapshot_id
                    or psha != expected.payload_sha256
                ):
                    continue
                origin_val = "rule" if ext_status == "deterministic_only" or model_name == "local-deterministic" else "model"
                try:
                    record = DerivedAttributeRecord(
                        product_id=pid,
                        organization_id=org_id,
                        store_id=st_id,
                        snapshot_id=snap_id,
                        payload_sha256=psha,
                        kind=kind,
                        value=val,
                        source_field=sfield,
                        start=start_off,
                        end=end_off,
                        normalized_value=norm_val,
                        normalization_version=norm_ver,
                        certainty=certainty,
                        brand_scope=brand_scope,
                        barcode_hint=barcode_hint,
                        origin=origin_val,  # type: ignore[arg-type]
                        extraction_version=ext_ver,
                        model=model_name,
                        prompt_version=prompt_ver,
                        algorithm_version=alg_ver,
                        input_digest=in_digest,
                        extraction_status=ext_status,
                        usage_status=use_status,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        error_code=err_code,
                    )
                except ValidationError:
                    continue
                result.setdefault(pid, []).append(record.model_dump())

        return result
    except (sqlite3.Error, DerivedCatalogStoreError, json.JSONDecodeError, ValueError):
        return {}
    finally:
        conn.close()
