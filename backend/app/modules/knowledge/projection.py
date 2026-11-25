"""Bounded global knowledge projection: snapshot, staging, publish, ranked reads.

This module is the only owner of the projection lifecycle.  It deliberately
keeps three things apart:

* the **snapshot** reads only source tables in one short read transaction and
  writes a private spool file before that Session is closed;
* **staging** inserts immutable derived rows plus the build record and commits;
* **publish** opens a fresh transaction, locks the singleton generation row,
  re-verifies that the staged build still reflects exactly that generation and
  only then advances the generation pointer.

Reads never evaluate the historical ACL for a principal.  They filter the
published window by the neutral access facts resolved at build time and combine
those facts with *live* authorization (``access_policy.has_restricted`` and the
current ``knowledge.read`` scope check).  A stale generation or a missing
pointer is an explicit ``unavailable`` result; there is no implicit slow
fallback to the legacy materializer.

The original token scorer is preserved byte-for-byte: the same lowercased
``" ".join(filter(None, (title, body, source_ref, scope_ref)))`` haystack is
stored as an exact UTF-8 ``BLOB``/``BYTEA`` and scored with substring matching
inside the database, then the original product/category regex, sort key and
citation truncation are applied unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.modules.knowledge import access_policy
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from backend.app.modules.knowledge.projection_models import (
    BUILD_STATES,
    NORMALIZER_VERSION,
    PROJECTION_SCHEMA_VERSION,
    READ_CLASSES,
    RESOLVER_VERSION,
    WINDOW_LIMIT,
    KnowledgeEntryProjection,
    KnowledgeProjectionBuild,
    KnowledgeSourceGeneration,
)
from backend.app.workers import outbox
from backend.app.workers.outbox import OutboxEvent

EVENT_TYPE = "knowledge.projection.rebuild"

# Generation contract: ``knowledge_source_generation.generation`` is advanced
# only by committed source DML (the 0077 triggers).  A rebuild/publish never
# invents a generation; it may replace the head at the same source generation,
# in which case the previous build is retired and can never be rebound or
# replayed as the current head.  A read is therefore bound to
# ``(generation, build_id)`` and any source commit makes the old build stale.

# Contract (AGT-045 K-P): the only query limit is the tool DTO's 1..200
# *character* string length; direct retrieval has no word/token/byte cap.  An
# arbitrarily long direct query is therefore scored with one JSON parameter,
# never truncated and never rejected for its size.
VISIBLE_READ_CLASSES = ("public", "restricted")
UNAVAILABLE_STATUS_CODE = 503

# Reasons are a closed set only for diagnostics.  Values are never used as
# authorization decisions; every reason maps to the same fail-closed 503.
UNAVAILABLE_REASONS = frozenset({
    "generation_missing", "generation_format", "generation_overflow",
    "projection_missing", "build_missing", "build_not_published",
    "build_state", "build_version", "stale", "entry_count_mismatch",
    "projection_digest_mismatch", "source_window_changed",
    "lease_required", "lease_missing", "lease_not_owned", "lease_expired",
    "lease_event_type", "row_count_mismatch", "projection_row_integrity",
    "snapshot_transaction_busy", "staging_generation_changed",
    "build_event_conflict", "publish_audit_scope_required", "source_window_expired",
    "stale_during_read", "lock_timeout",
})


class KnowledgeProjectionUnavailable(RuntimeError):
    """Fail-closed projection unavailability (HTTP 503 by default)."""

    status_code = UNAVAILABLE_STATUS_CODE

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Compatibility alias used by callers that prefer the shorter name.
ProjectionUnavailable = KnowledgeProjectionUnavailable


# ---------------------------------------------------------------------------
# small immutable value objects


@dataclass(frozen=True, slots=True)
class Snapshot:
    generation: int
    window_limit: int
    input_digest: str
    created_at: datetime
    documents: tuple[dict[str, Any], ...]
    scope_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    window_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProjectionWindow:
    generation: int
    build_id: str
    window_limit: int
    rows: tuple[dict[str, Any], ...]
    # Trusted boundary captured with the rows so the caller can pin the exact
    # build identity and the pre-ACL window's earliest expiry.
    source_generation: int | None = None
    resolver_version: int | None = None
    normalizer_version: int | None = None
    projection_schema_version: int | None = None
    window_expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# time / digest helpers


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _record(row) -> dict[str, Any]:
    return access_policy._record(row)


def _lower_search_bytes(entry: dict[str, Any], revision: dict[str, Any] | None) -> bytes:
    """The exact legacy haystack: no trimming, no punctuation folding."""
    title = revision.get("title") if revision else None
    body = revision.get("body") if revision else None
    source_ref = revision.get("source_ref") if revision else None
    haystack = " ".join(filter(None, (title, body, source_ref, entry.get("scope_ref"))))
    return haystack.lower().encode("utf-8")


def _entry_payload(entry: dict[str, Any], revision: dict[str, Any] | None) -> dict[str, Any]:
    payload = {
        "id": entry.get("id"),
        "version": entry.get("version"),
        "current_revision_id": entry.get("current_revision_id"),
        "organization_id": entry.get("organization_id"),
        "store_id": entry.get("store_id"),
        "status": entry.get("status"),
        "scope_type": entry.get("scope_type"),
        "scope_ref": entry.get("scope_ref"),
        "submitted_at": _iso(entry.get("submitted_at")),
        "approved_by": entry.get("approved_by"),
        "approved_at": _iso(entry.get("approved_at")),
        "expires_at": _iso(entry.get("expires_at")),
        "revision": None,
    }
    if revision is not None:
        payload["revision"] = {
            "id": revision.get("id"),
            "entry_id": revision.get("entry_id"),
            "version": revision.get("version"),
            "organization_id": revision.get("organization_id"),
            "store_id": revision.get("store_id"),
            "title": revision.get("title"),
            "body": revision.get("body"),
            "source_type": revision.get("source_type"),
            "source_ref": revision.get("source_ref"),
            "submitted_by": revision.get("submitted_by"),
            "submitted_at": _iso(revision.get("submitted_at")),
        }
    return payload


def _input_digest(entries: list[KnowledgeEntry], revisions: dict[str, dict[str, Any] | None]) -> str:
    payloads = [_entry_payload(_record(entry), revisions.get(entry.id)) for entry in entries]
    return _sha256(_json_bytes(payloads))


def _canonical_iso(value: Any) -> Any:
    """Digest-stable timestamp form: naive UTC, independent of engine tzinfo."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    return value


def _compact_value(row, key: str):
    if isinstance(row, dict):
        return row[key]
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return getattr(row, key)


def _row_tuple(row) -> list[Any]:
    # Only compact citation metadata participates in the per-row digest.  The
    # hot read path must not pull the full UTF-8 haystack out of the DB just to
    # re-hash it; scoring bytes are covered by the trusted published-projection
    # boundary (publish verifies the full build digest once).
    return [
        _compact_value(row, "entry_id"),
        _compact_value(row, "organization_id"),
        _compact_value(row, "store_id"),
        _compact_value(row, "entry_version"),
        _compact_value(row, "current_revision_id"),
        _compact_value(row, "revision_version"),
        _canonical_iso(_compact_value(row, "submitted_at")),
        _compact_value(row, "status"),
        _canonical_iso(_compact_value(row, "expires_at")),
        _compact_value(row, "scope_type"),
        _compact_value(row, "scope_ref"),
        _compact_value(row, "approved_by"),
        _canonical_iso(_compact_value(row, "approved_at")),
        _compact_value(row, "read_class"),
        _compact_value(row, "policy_version"),
        _compact_value(row, "access_epoch"),
        _compact_value(row, "scope_reference_class"),
        _compact_value(row, "title"),
        _compact_value(row, "body_excerpt"),
        _compact_value(row, "source_type"),
        _compact_value(row, "source_ref"),
    ]


def _row_digest(row) -> str:
    """Digest of one row's compact citation metadata, verified before serving."""
    return _sha256(_json_bytes(_row_tuple(row)))


def _full_row_tuple(row: KnowledgeEntryProjection) -> list[Any]:
    # The build digest still covers every stored field (including the search
    # haystack); it is verified once at publish/staging, not per read hit.
    return _row_tuple(row) + [
        _sha256(row.lower_search_bytes),
        row.proof,
        row.source_digest,
    ]


def _projection_digest(rows: list[KnowledgeEntryProjection]) -> str:
    ordered = sorted(rows, key=lambda row: row.entry_id)
    return _sha256(_json_bytes([_full_row_tuple(row) for row in ordered]))


def _chunks(values, size: int = 400):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


# ---------------------------------------------------------------------------
# source window


def _window_order(session: Session) -> tuple:
    """Reproduce the legacy window order, including its tie behaviour.

    The legacy statement is only ``ORDER BY submitted_at DESC``; on SQLite that
    leaves equal timestamps in physical rowid (insertion) order, while the
    planner may otherwise return rows in primary-key order.  The projection
    therefore orders ties by rowid on SQLite.  PostgreSQL has no rowid, so the
    deterministic proxy used there is ``created_at, id`` (the legacy planner
    order is unspecified on both engines; this only pins the projection).
    """
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        return (
            KnowledgeEntry.submitted_at.desc(),
            sa.text("knowledge_entries.rowid ASC"),
        )
    return (
        KnowledgeEntry.submitted_at.desc(),
        KnowledgeEntry.created_at.asc(),
        KnowledgeEntry.id.asc(),
    )


def window_entries(session: Session, threshold: datetime, window_limit: int = WINDOW_LIMIT) -> list[KnowledgeEntry]:
    """The original approved/unexpired window, capped per (organization, store).

    The cap is applied per identity scope *before* authorization, exactly like
    the legacy materializer's per-scope ``LIMIT 10000``.
    """
    limit = _validate_window_limit(window_limit)
    order = _window_order(session)
    ranked = (
        select(
            KnowledgeEntry.id.label("entry_id"),
            func.row_number().over(
                partition_by=(KnowledgeEntry.organization_id, KnowledgeEntry.store_id),
                order_by=order,
            ).label("position"),
        )
        .where(
            KnowledgeEntry.status == "approved",
            sa.or_(KnowledgeEntry.expires_at.is_(None), KnowledgeEntry.expires_at > threshold),
        )
        .subquery()
    )
    window_ids = select(ranked.c.entry_id).where(ranked.c.position <= limit)
    statement = (
        select(KnowledgeEntry)
        .where(KnowledgeEntry.id.in_(window_ids))
        .order_by(*order)
    )
    return list(session.scalars(statement))


def _window_expiry(entries: list[KnowledgeEntry]) -> datetime | None:
    """Earliest natural expiry of any row inside the pre-ACL window.

    An unexpiring row makes the window time-independent (``None``).  This bound
    is the only silent, DML-free way a legitimate window can shrink, so the read
    path rejects the build once the bound passes instead of pretending the
    smaller remainder is the original window.
    """
    bound: datetime | None = None
    for entry in entries:
        value = entry.expires_at
        if value is None:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if bound is None or value < bound:
            bound = value
    # ``None`` only when every row in the window never expires.
    return bound


def _head_signature(session: Session) -> tuple[int, str | None]:
    """Cheap (generation, build_id) fingerprint used for the final read recheck."""
    row = session.execute(
        select(KnowledgeSourceGeneration.generation, KnowledgeSourceGeneration.build_id)
        .where(KnowledgeSourceGeneration.id == 1)
    ).first()
    if row is None:
        raise KnowledgeProjectionUnavailable("generation_missing")
    return int(row[0]), row[1]


def _validate_window_limit(window_limit: int) -> int:
    if not isinstance(window_limit, int) or isinstance(window_limit, bool):
        raise ValueError("window_limit must be an int")
    if not 1 <= window_limit <= WINDOW_LIMIT:
        raise ValueError("window_limit must be between 1 and 10000")
    return window_limit


def _current_revisions(session: Session, entries: list[KnowledgeEntry]) -> dict[str, dict[str, Any] | None]:
    by_entry: dict[str, dict[str, dict[str, Any]]] = {}
    for chunk in _chunks([entry.id for entry in entries]):
        for revision in session.scalars(
            select(KnowledgeRevision).where(KnowledgeRevision.entry_id.in_(chunk))
        ):
            by_entry.setdefault(revision.entry_id, {})[revision.id] = _record(revision)
    return {
        entry.id: by_entry.get(entry.id, {}).get(entry.current_revision_id)
        for entry in entries
    }


# ---------------------------------------------------------------------------
# snapshot (read-only, then spool, then the caller closes the Session)


def _begin_snapshot_read(session: Session) -> None:
    """Start the explicit read transaction the frozen design requires.

    SQLAlchemy/pysqlite defer ``BEGIN`` until the first DML, so a plain sequence
    of SELECTs would not share a snapshot.  The source snapshot therefore begins
    explicitly: SQLite gets a read transaction, PostgreSQL a read-only
    REPEATABLE READ transaction.  A caller that already wrote on this Session
    cannot take a clean snapshot and gets a fail-closed reason.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else None
    try:
        if dialect == "postgresql":
            connection = session.connection()
            connection.exec_driver_sql("BEGIN")
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
        else:
            session.connection().exec_driver_sql("BEGIN")
    except sa.exc.OperationalError as exc:
        raise KnowledgeProjectionUnavailable("snapshot_transaction_busy") from exc


def snapshot_scope(
    session: Session,
    *,
    now: datetime | None = None,
    window_limit: int = WINDOW_LIMIT,
) -> Snapshot:
    """Read the exact source window and resolve neutral access facts for it.

    No write, flush or identity decision happens here.  The returned Snapshot is
    plain data; the caller spools it and closes this Session before staging.
    """
    timestamp = _now(now)
    limit = _validate_window_limit(window_limit)
    _begin_snapshot_read(session)
    generation_row = session.get(KnowledgeSourceGeneration, 1)
    if generation_row is None:
        raise KnowledgeProjectionUnavailable("generation_missing")
    if generation_row.format_version != PROJECTION_SCHEMA_VERSION:
        raise KnowledgeProjectionUnavailable("generation_format")
    generation = generation_row.generation
    entries = window_entries(session, timestamp, limit)
    window_expires_at = _window_expiry(entries)
    documents: list[dict[str, Any]] = []
    scope_counts: dict[tuple[str, str], int] = {}
    for entry in entries:
        entry_dict = _record(entry)
        revisions, policies, facts, conflict = access_policy.inspect_entry(session, entry)
        revision_records = [_record(row) for row in revisions]
        policy_records = [access_policy._record(row) for row in policies]
        revision = next(
            (row for row in revision_records if row.get("id") == entry.current_revision_id),
            None,
        )
        state = access_policy.projection_entry_state(
            entry_dict, revision, revision_records, policy_records, facts, conflict,
        )
        lower_bytes = _lower_search_bytes(entry_dict, revision)
        source_payload = {
            "entry": _entry_payload(entry_dict, revision),
            "read_class": state["read_class"],
            "cause": state["cause"],
            "policy_version": state["policy_version"],
            "access_epoch": state["access_epoch"],
            "scope_reference_class": state["scope_reference_class"],
            "lower_sha256": _sha256(lower_bytes),
        }
        proof = {
            "resolver_version": RESOLVER_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "cause": state["cause"],
            "entry_sha256": _sha256(_json_bytes(_entry_payload(entry_dict, None))),
            "revision_sha256": (
                _sha256(_json_bytes(_entry_payload({"id": entry.id}, revision)["revision"]))
                if revision is not None else None
            ),
            "ancestor_hold_ids": list(state["ancestor_hold_ids"])[:20],
            "ancestor_fact_complete": bool(state["ancestor_fact_complete"]),
        }
        document = {
            "entry_id": entry.id,
            "organization_id": entry.organization_id,
            "store_id": entry.store_id,
            "entry_version": entry.version,
            "current_revision_id": entry.current_revision_id,
            "revision_version": revision.get("version") if revision is not None else None,
            "submitted_at": _iso(entry.submitted_at),
            "status": entry.status,
            "expires_at": _iso(entry.expires_at),
            "scope_type": entry.scope_type,
            "scope_ref": entry.scope_ref,
            "approved_by": entry.approved_by,
            "approved_at": _iso(entry.approved_at),
            "title": (revision.get("title") if revision is not None else "")[:200],
            "body_excerpt": (revision.get("body") if revision is not None else "")[:400],
            "source_type": revision.get("source_type") if revision is not None else "",
            "source_ref": revision.get("source_ref") if revision is not None else None,
            "read_class": state["read_class"],
            "policy_version": state["policy_version"],
            "access_epoch": state["access_epoch"],
            "scope_reference_class": state["scope_reference_class"],
            "proof": proof,
            "lower_search_hex": lower_bytes.hex(),
            "source_digest": _sha256(_json_bytes(source_payload)),
        }
        documents.append(document)
        scope = (entry.organization_id, entry.store_id)
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
    # The digest is built from the same plain payloads the publish verifier will
    # rebuild from live source rows, including the full revision body.
    revisions = _current_revisions(session, entries)
    input_digest = _input_digest(entries, revisions)
    return Snapshot(
        generation=generation,
        window_limit=limit,
        input_digest=input_digest,
        created_at=timestamp,
        documents=tuple(documents),
        scope_counts=scope_counts,
        window_expires_at=window_expires_at,
    )


# ---------------------------------------------------------------------------
# private spool


def write_spool(snapshot: Snapshot) -> str:
    """Write the snapshot to a private (0600) temp file and return its path."""
    descriptor, path = tempfile.mkstemp(prefix="kp_snapshot_", suffix=".jsonl")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "generation": snapshot.generation,
                "window_limit": snapshot.window_limit,
                "input_digest": snapshot.input_digest,
                "created_at": _iso(snapshot.created_at),
                "window_expires_at": _iso(snapshot.window_expires_at),
                "scope_counts": {f"{key[0]}\x1f{key[1]}": value
                                 for key, value in snapshot.scope_counts.items()},
            }, ensure_ascii=False) + "\n")
            for document in snapshot.documents:
                handle.write(json.dumps(document, ensure_ascii=False) + "\n")
        return path
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def read_spool(path: str) -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    """Return (metadata, lazy document iterator); never loads the whole spool."""
    handle = open(path, "r", encoding="utf-8")

    def documents() -> Iterator[dict[str, Any]]:
        with handle:
            first = handle.readline()
            if not first:
                return
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    handle.seek(0)
    metadata = json.loads(handle.readline())
    handle.seek(0)
    return metadata, documents()


# ---------------------------------------------------------------------------
# staging


def stage_snapshot(
    session: Session,
    *,
    build_id: str,
    spool_path: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Insert/replace one staging build from the spool.  Does not commit."""
    timestamp = _now(now)
    metadata, documents = read_spool(spool_path)
    window_expires_at = (
        _parse_time(metadata["window_expires_at"])
        if metadata.get("window_expires_at") else None
    )
    existing = session.get(KnowledgeProjectionBuild, build_id)
    if existing is not None and existing.state == "published":
        generation_row = session.get(KnowledgeSourceGeneration, 1)
        if (generation_row is None
                or generation_row.build_id != build_id
                or generation_row.generation != existing.source_generation):
            raise KnowledgeProjectionUnavailable("stale")
        return {
            "state": "published", "build_id": build_id, "replayed": True,
            "generation": existing.source_generation,
        }
    if existing is not None and existing.state not in ("staging",):
        raise KnowledgeProjectionUnavailable("build_state")
    if existing is not None and existing.source_generation != int(metadata["generation"]):
        # Re-staging would silently rebind this build/event to a newer source
        # snapshot.  Fail closed; a later event rebuilds against the new
        # generation instead.
        raise KnowledgeProjectionUnavailable("staging_generation_changed")
    if existing is not None:
        session.execute(
            sa.delete(KnowledgeEntryProjection).where(
                KnowledgeEntryProjection.build_id == build_id
            )
        )
        existing.source_generation = int(metadata["generation"])
        existing.resolver_version = RESOLVER_VERSION
        existing.normalizer_version = NORMALIZER_VERSION
        existing.projection_schema_version = PROJECTION_SCHEMA_VERSION
        existing.window_limit = int(metadata["window_limit"])
        existing.input_digest = str(metadata["input_digest"])
        existing.built_at = timestamp
        existing.window_expires_at = window_expires_at
        existing.entry_count = 0
        existing.projection_digest = _sha256(_json_bytes([]))
        existing.state = "staging"
        build = existing
    else:
        build = KnowledgeProjectionBuild(
            build_id=build_id,
            source_generation=int(metadata["generation"]),
            state="staging",
            resolver_version=RESOLVER_VERSION,
            normalizer_version=NORMALIZER_VERSION,
            projection_schema_version=PROJECTION_SCHEMA_VERSION,
            window_limit=int(metadata["window_limit"]),
            entry_count=0,
            input_digest=str(metadata["input_digest"]),
            projection_digest=_sha256(_json_bytes([])),
            built_at=timestamp,
            window_expires_at=window_expires_at,
        )
        session.add(build)
    session.flush()
    rows: list[KnowledgeEntryProjection] = []
    count = 0
    for document in documents:
        row = KnowledgeEntryProjection(
            build_id=build_id,
            entry_id=document["entry_id"],
            organization_id=document["organization_id"],
            store_id=document["store_id"],
            entry_version=int(document["entry_version"]),
            current_revision_id=document["current_revision_id"],
            revision_version=(
                int(document["revision_version"])
                if document.get("revision_version") is not None else None
            ),
            submitted_at=_parse_time(document["submitted_at"]),
            status=document["status"],
            expires_at=(
                _parse_time(document["expires_at"])
                if document.get("expires_at") is not None else None
            ),
            scope_type=document["scope_type"],
            scope_ref=document.get("scope_ref"),
            approved_by=document.get("approved_by"),
            approved_at=(
                _parse_time(document["approved_at"])
                if document.get("approved_at") is not None else None
            ),
            read_class=document["read_class"],
            policy_version=int(document["policy_version"]),
            access_epoch=int(document["access_epoch"]),
            scope_reference_class=document["scope_reference_class"],
            title=document["title"],
            body_excerpt=document["body_excerpt"],
            source_type=document["source_type"],
            source_ref=document.get("source_ref"),
            lower_search_bytes=bytes.fromhex(document["lower_search_hex"]),
            proof=document["proof"],
            source_digest=document["source_digest"],
            row_digest="pending",
        )
        row.row_digest = _row_digest(row)
        session.add(row)
        rows.append(row)
        count += 1
        if count % 500 == 0:
            session.flush()
    session.flush()
    build.entry_count = count
    build.projection_digest = _projection_digest(rows)
    session.flush()
    return {
        "state": "staging",
        "build_id": build_id,
        "source_generation": build.source_generation,
        "entry_count": count,
        "projection_digest": build.projection_digest,
    }


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# publish (fresh transaction, generation pointer CAS)


def _begin_write(session: Session) -> None:
    if session.in_transaction():
        session.rollback()
    if session.get_bind().dialect.name == "sqlite":
        # One writer at a time; the lock is taken before reading the pointer.
        session.execute(text("BEGIN IMMEDIATE"))


def publish_build(
    session: Session,
    *,
    build_id: str,
    actor_id: str | None = None,
    worker_id: str | None = None,
    event_id: str | None = None,
    organization_id: str | None = None,
    store_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically verify and publish a staged build.  Does not commit."""
    timestamp = _now(now)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("actor_id is required for an attributed projection publish")
    # The design requires the projection audit in the publish transaction; a
    # publish with no organization/store cannot write that row, so it fails
    # closed instead of silently skipping attribution.
    if not organization_id or not store_id:
        raise KnowledgeProjectionUnavailable("publish_audit_scope_required")
    _begin_write(session)
    generation = session.get(KnowledgeSourceGeneration, 1, with_for_update=True)
    if generation is None:
        raise KnowledgeProjectionUnavailable("generation_missing")
    if generation.format_version != PROJECTION_SCHEMA_VERSION:
        raise KnowledgeProjectionUnavailable("generation_format")
    build = session.get(KnowledgeProjectionBuild, build_id, with_for_update=True)
    if build is None:
        raise KnowledgeProjectionUnavailable("build_missing")
    if build.state == "published":
        if generation.build_id == build_id:
            return _published_result(build, generation, replayed=True)
        raise KnowledgeProjectionUnavailable("build_state")
    if build.state != "staging":
        raise KnowledgeProjectionUnavailable("build_state")
    if (build.resolver_version, build.normalizer_version, build.projection_schema_version) != (
        RESOLVER_VERSION, NORMALIZER_VERSION, PROJECTION_SCHEMA_VERSION,
    ):
        raise KnowledgeProjectionUnavailable("build_version")
    if event_id is not None:
        if build.event_id is not None and build.event_id != event_id:
            raise KnowledgeProjectionUnavailable("build_event_conflict")
        build.event_id = event_id
    _check_lease(
        session, event_id=event_id, worker_id=worker_id, now=timestamp,
    )
    if generation.generation != build.source_generation:
        raise KnowledgeProjectionUnavailable("source_window_changed")
    # Rebuild the window at the snapshot threshold, not at publish wall-clock:
    # a row expiring while the build waits for its transaction would otherwise
    # make every healthy slow build unpublishable.
    snapshot_threshold = build.built_at
    if snapshot_threshold.tzinfo is None:
        snapshot_threshold = snapshot_threshold.replace(tzinfo=timezone.utc)
    entries = window_entries(session, snapshot_threshold, build.window_limit)
    revisions = _current_revisions(session, entries)
    if _input_digest(entries, revisions) != build.input_digest:
        raise KnowledgeProjectionUnavailable("source_window_changed")
    recomputed_expiry = _window_expiry(entries)
    stored_expiry = build.window_expires_at
    if stored_expiry is not None and stored_expiry.tzinfo is None:
        stored_expiry = stored_expiry.replace(tzinfo=timezone.utc)
    if recomputed_expiry != stored_expiry:
        raise KnowledgeProjectionUnavailable("source_window_changed")
    rows = list(session.scalars(
        select(KnowledgeEntryProjection).where(KnowledgeEntryProjection.build_id == build_id)
    ))
    if len(rows) != build.entry_count or len(rows) != len(entries):
        raise KnowledgeProjectionUnavailable("entry_count_mismatch")
    if _projection_digest(rows) != build.projection_digest:
        raise KnowledgeProjectionUnavailable("projection_digest_mismatch")
    previous_build_id = generation.build_id
    build.state = "published"
    build.published_at = timestamp
    build.publisher_id = actor_id
    if previous_build_id is not None and previous_build_id != build_id:
        previous = session.get(KnowledgeProjectionBuild, previous_build_id, with_for_update=True)
        if previous is not None and previous.state == "published":
            previous.state = "retired"
    # Frozen design: generation == the source generation the build
    # snapshotted; publishing records the build but never advances it.
    generation.build_id = build_id
    generation.entry_count = build.entry_count
    generation.built_at = build.built_at
    generation.updated_at = timestamp
    session.flush()
    from backend.app.modules.audit import service as audit_service
    audit_service.record_event(
        session,
        actor_id=actor_id,
        organization_id=organization_id,
        store_id=store_id,
        action="knowledge.projection_rebuilt",
        target_type="knowledge_projection",
        target_id=build_id,
        idempotency_key=f"knowledge.projection.published:{build_id}",
        details={
            "generation": generation.generation,
            "entry_count": build.entry_count,
            "input_digest": build.input_digest,
            "projection_digest": build.projection_digest,
        },
        occurred_at=timestamp,
    )
    return {
        "state": "published",
        "build_id": build_id,
        "generation": generation.generation,
        "entry_count": build.entry_count,
        "input_digest": build.input_digest,
        "projection_digest": build.projection_digest,
        "published_at": _iso(timestamp),
        "replayed": False,
    }


def _published_result(build: KnowledgeProjectionBuild, generation: KnowledgeSourceGeneration | None,
                      *, replayed: bool) -> dict[str, Any]:
    if generation is None:
        raise KnowledgeProjectionUnavailable("generation_missing")
    if (generation.build_id != build.build_id
            or generation.generation != build.source_generation):
        raise KnowledgeProjectionUnavailable("stale")
    return {
        "state": "published",
        "build_id": build.build_id,
        "generation": generation.generation,
        "entry_count": build.entry_count,
        "input_digest": build.input_digest,
        "projection_digest": build.projection_digest,
        "published_at": _iso(build.published_at),
        "replayed": replayed,
    }


def _check_lease(session: Session, *, event_id: str | None, worker_id: str | None,
                 now: datetime) -> None:
    if event_id is None:
        return
    if worker_id is None:
        raise KnowledgeProjectionUnavailable("lease_not_owned")
    event = session.get(OutboxEvent, event_id, with_for_update=True)
    if event is None:
        raise KnowledgeProjectionUnavailable("lease_missing")
    if event.status != "leased":
        raise KnowledgeProjectionUnavailable("lease_missing")
    if event.event_type != EVENT_TYPE:
        raise KnowledgeProjectionUnavailable("lease_event_type")
    lease_until = event.lease_until
    if lease_until is not None and lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until is None or lease_until <= now:
        raise KnowledgeProjectionUnavailable("lease_expired")
    if event.lease_owner != worker_id:
        raise KnowledgeProjectionUnavailable("lease_not_owned")


# ---------------------------------------------------------------------------
# orchestration


def rebuild_now(
    session_factory,
    *,
    actor_id: str,
    worker_id: str | None = None,
    event_id: str | None = None,
    organization_id: str | None = None,
    store_id: str | None = None,
    now: datetime | None = None,
    window_limit: int = WINDOW_LIMIT,
    build_id: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Snapshot → spool → close → stage → publish, with replay on retry."""
    timestamp = _now(now)
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("actor_id is required")
    if event_id is not None:
        with session_factory() as replay_session:
            existing = replay_session.scalar(
                select(KnowledgeProjectionBuild).where(KnowledgeProjectionBuild.event_id == event_id)
            )
            if existing is not None and existing.state == "published":
                generation = replay_session.get(KnowledgeSourceGeneration, 1)
                try:
                    return _published_result(existing, generation, replayed=True)
                except KnowledgeProjectionUnavailable as exc:
                    if exc.reason != "stale":
                        raise
                    return {
                        "state": "stale",
                        "build_id": existing.build_id,
                        "generation": generation.generation if generation is not None else None,
                        "expected_generation": expected_generation,
                    }
            if existing is not None:
                build_id = existing.build_id
    build_id = build_id or str(uuid4())
    snapshot_session = session_factory()
    spool_path: str | None = None
    try:
        snapshot = snapshot_scope(snapshot_session, now=timestamp, window_limit=window_limit)
        if expected_generation is not None and snapshot.generation != expected_generation:
            return {
                "state": "stale",
                "build_id": build_id,
                "generation": snapshot.generation,
                "expected_generation": expected_generation,
            }
        spool_path = write_spool(snapshot)
    finally:
        snapshot_session.close()
    try:
        with session_factory() as staging_session:
            staged = stage_snapshot(
                staging_session, build_id=build_id, spool_path=spool_path, now=timestamp,
            )
            if staged.get("replayed"):
                return staged
            staging_session.commit()
        with session_factory() as publish_session:
            published = publish_build(
                publish_session,
                build_id=build_id,
                actor_id=actor_id,
                worker_id=worker_id,
                event_id=event_id,
                organization_id=organization_id,
                store_id=store_id,
                # A real rebuild can outlive its lease: when no clock is
                # injected, publish re-reads the wall clock so an expired
                # lease cannot publish. Explicit test clocks stay deterministic.
                now=None if now is None else timestamp,
            )
            publish_session.commit()
            return published
    finally:
        if spool_path is not None:
            try:
                os.unlink(spool_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# reads: only published state, neutral access facts + live authorization


def _require_read(session: Session, actor, timestamp: datetime) -> None:
    from backend.app.modules.identity.command_guard import refresh_authority
    from backend.app.modules.knowledge import service as knowledge_service
    # Mirrors materialize_usable_entries: refresh the authority snapshot, then
    # require the permission again so a long-lived session cannot read with a
    # stale grant.
    refresh_authority(session)
    knowledge_service._require(session, actor, knowledge_service.READ_PERMISSION, timestamp)


def published_head(session: Session, *, now: datetime | None = None) -> tuple[KnowledgeSourceGeneration, KnowledgeProjectionBuild]:
    generation = session.get(KnowledgeSourceGeneration, 1)
    if generation is None:
        raise KnowledgeProjectionUnavailable("generation_missing")
    if generation.format_version != PROJECTION_SCHEMA_VERSION:
        raise KnowledgeProjectionUnavailable("generation_format")
    if not generation.build_id:
        raise KnowledgeProjectionUnavailable("projection_missing")
    build = session.get(KnowledgeProjectionBuild, generation.build_id)
    if build is None:
        raise KnowledgeProjectionUnavailable("build_missing")
    if build.state != "published":
        raise KnowledgeProjectionUnavailable("build_not_published")
    if (build.resolver_version, build.normalizer_version, build.projection_schema_version) != (
        RESOLVER_VERSION, NORMALIZER_VERSION, PROJECTION_SCHEMA_VERSION,
    ):
        raise KnowledgeProjectionUnavailable("build_version")
    if build.source_generation != generation.generation:
        raise KnowledgeProjectionUnavailable("stale")
    if build.entry_count != generation.entry_count:
        raise KnowledgeProjectionUnavailable("entry_count_mismatch")
    # count(<column>), not count(*): the commit-guard read-only filter rejects
    # SQLAlchemy's literal `*` column node, and the pin must stay a plain read.
    row_count = session.scalar(
        select(func.count(KnowledgeEntryProjection.entry_id))
        .where(KnowledgeEntryProjection.build_id == build.build_id)
    )
    if row_count != build.entry_count:
        raise KnowledgeProjectionUnavailable("row_count_mismatch")
    if build.window_expires_at is not None:
        bound = build.window_expires_at
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=timezone.utc)
        # The pre-ACL window keeps rows with ``expires_at > now``.  At the exact
        # expiry second the legacy window has already rolled, so ``>`` here
        # would silently serve a shrunken window as a false no-match.
        if _now(now) >= bound:
            raise KnowledgeProjectionUnavailable("source_window_expired")
    return generation, build


def _visible_row(row: dict[str, Any], *, restricted: bool) -> bool:
    return access_policy.projection_row_visible(row, restricted=restricted)


def _token_relation(session: Session):
    """Expanded exact-token relation for one JSON-encoded query parameter.

    The whole query crosses the boundary once, so the number of words is not
    bounded by the driver's parameter count.  SQLite expands the JSON array
    with ``json_each``; PostgreSQL with ``jsonb_array_elements_text``.

    Tokens cross as lowercase hex strings, not text: a query may legally hold
    a NUL byte, and both JSONB and PostgreSQL text reject ``\u0000`` (22P05).
    Hex is lossless in both directions and keeps the legacy Python substring
    semantics byte-for-byte (decode on the DB side, then exact binary search).
    """
    dialect = session.get_bind().dialect.name
    parameter = sa.bindparam("token_hex_json", type_=sa.String())
    if dialect == "postgresql":
        from sqlalchemy.dialects import postgresql as pg_types
        rows = sa.func.jsonb_array_elements_text(
            sa.cast(parameter, pg_types.JSONB)
        ).table_valued("value").render_derived()
        return rows, sa.text(
            "position(decode(value, 'hex') in "
            "knowledge_entry_projections.lower_search_bytes) > 0")
    rows = sa.func.json_each(parameter).table_valued("value")
    return rows, func.instr(
        KnowledgeEntryProjection.lower_search_bytes,
        func.unhex(rows.c.value)) > 0


_COMPACT_COLUMNS = (
    KnowledgeEntryProjection.entry_id,
    KnowledgeEntryProjection.organization_id,
    KnowledgeEntryProjection.store_id,
    KnowledgeEntryProjection.entry_version,
    KnowledgeEntryProjection.current_revision_id,
    KnowledgeEntryProjection.revision_version,
    KnowledgeEntryProjection.submitted_at,
    KnowledgeEntryProjection.status,
    KnowledgeEntryProjection.expires_at,
    KnowledgeEntryProjection.scope_type,
    KnowledgeEntryProjection.scope_ref,
    KnowledgeEntryProjection.approved_by,
    KnowledgeEntryProjection.approved_at,
    KnowledgeEntryProjection.read_class,
    KnowledgeEntryProjection.policy_version,
    KnowledgeEntryProjection.access_epoch,
    KnowledgeEntryProjection.scope_reference_class,
    KnowledgeEntryProjection.title,
    KnowledgeEntryProjection.body_excerpt,
    KnowledgeEntryProjection.source_type,
    KnowledgeEntryProjection.source_ref,
    KnowledgeEntryProjection.row_digest,
)


def _score_statement(session: Session, *, build_id: str, organization_id: str, store_id: str,
                     threshold: datetime, tokens: list[bytes] | None):
    # Never select ``lower_search_bytes`` or whole ORM rows on the read path:
    # a common token can match the full 10k window and the bytes never need to
    # cross the DB boundary.  The frozen trusted-DB boundary guarantees that a
    # published row is the exact row verified at publish time.
    column = KnowledgeEntryProjection.lower_search_bytes
    criteria = [
        KnowledgeEntryProjection.build_id == build_id,
        KnowledgeEntryProjection.organization_id == organization_id,
        KnowledgeEntryProjection.store_id == store_id,
        KnowledgeEntryProjection.status == "approved",
        sa.or_(
            KnowledgeEntryProjection.expires_at.is_(None),
            KnowledgeEntryProjection.expires_at > threshold,
        ),
        KnowledgeEntryProjection.read_class.in_(VISIBLE_READ_CLASSES),
    ]
    if tokens is None:
        return select(*_COMPACT_COLUMNS).where(*criteria)
    token_rows, position = _token_relation(session)
    score = (
        select(func.count())
        .select_from(token_rows)
        .where(position)
        .scalar_subquery()
    )
    statement = (
        select(*_COMPACT_COLUMNS, score.label("score"))
        .where(*criteria, score > 0)
        .order_by(KnowledgeEntryProjection.submitted_at.desc())
    )
    return statement.params(
        token_hex_json=json.dumps([token.hex() for token in tokens]))


def _budget_reason(deadline: float | None, cancel_event) -> str | None:
    if cancel_event is not None and cancel_event.is_set():
        return "cancelled"
    if deadline is not None and time.monotonic() >= deadline:
        return "deadline_exceeded"
    return None


def _install_db_budget(session, deadline: float | None, cancel_event):
    """Bound the database call itself, not just the Python rows loop.

    SQLite installs a progress handler so a long scan/lock wait is interrupted
    and the statement rolls back.  PG uses ``SET LOCAL statement_timeout`` so
    the budget cannot leak to later statements on the pooled connection.
    """
    if deadline is None and cancel_event is None:
        return lambda: None
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        raw = session.connection().connection.driver_connection

        def progress():
            return 1 if _budget_reason(deadline, cancel_event) else 0

        raw.set_progress_handler(progress, 1000)

        def restore():
            raw.set_progress_handler(None, 1000)

        return restore
    if dialect == "postgresql" and deadline is not None:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        session.execute(text(f"SET LOCAL statement_timeout = {remaining_ms}"))
    return lambda: None


def pin_prepared(actor, prepared, *, deadline=None, cancel_event=None):
    """Return a commit-guard check that revalidates the projection at commit."""
    def check(session, instant):
        _check_window_budget(deadline, cancel_event)
        timestamp = _now(instant)
        _require_read(session, actor, timestamp)
        expires = prepared.window_expires_at
        if expires is not None:
            expiry = datetime.fromisoformat(expires) if isinstance(expires, str) else expires
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if timestamp >= expiry:
                raise RuntimeError("knowledge_projection_window_expired")
        generation, build = published_head(session, now=timestamp)
        expected = (
            prepared.generation, prepared.build_id, prepared.source_generation,
            prepared.window_limit, prepared.resolver_version,
            prepared.normalizer_version, prepared.projection_schema_version,
        )
        actual = (
            generation.generation, build.build_id, build.source_generation,
            build.window_limit, build.resolver_version,
            build.normalizer_version, build.projection_schema_version,
        )
        if actual != expected:
            raise RuntimeError("knowledge_projection_head_changed")
        if prepared.needs_restricted and not access_policy.has_restricted(session, actor, timestamp):
            raise RuntimeError("knowledge_projection_acl_changed")
        if _head_signature(session) != (generation.generation, build.build_id):
            raise RuntimeError("knowledge_projection_head_changed")
        _check_window_budget(deadline, cancel_event)
    return check


def _check_window_budget(deadline: float | None, cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise KnowledgeProjectionUnavailable("cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise KnowledgeProjectionUnavailable("deadline_exceeded")


def load_window(session: Session, actor, *, now: datetime | None = None,
                query: str | None = None, deadline: float | None = None,
                cancel_event=None) -> ProjectionWindow:
    """Load the published scope window, with optional in-database token score."""
    _check_window_budget(deadline, cancel_event)
    timestamp = _now(now)
    _require_read(session, actor, timestamp)
    generation, build = published_head(session, now=timestamp)
    tokens = None
    if query is not None:
        text_value = (query or "").strip()
        if not text_value:
            return ProjectionWindow(
                generation=generation.generation, build_id=build.build_id,
                window_limit=build.window_limit, rows=(),
                source_generation=build.source_generation,
                resolver_version=build.resolver_version,
                normalizer_version=build.normalizer_version,
                projection_schema_version=build.projection_schema_version,
                window_expires_at=build.window_expires_at,
            )
        from backend.app.modules.knowledge.retrieval import _tokens
        tokens = sorted(token.encode("utf-8") for token in _tokens(text_value))
        if not tokens:
            return ProjectionWindow(
                generation=generation.generation, build_id=build.build_id,
                window_limit=build.window_limit, rows=(),
                source_generation=build.source_generation,
                resolver_version=build.resolver_version,
                normalizer_version=build.normalizer_version,
                projection_schema_version=build.projection_schema_version,
                window_expires_at=build.window_expires_at,
            )
    statement = _score_statement(
        session,
        build_id=build.build_id,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        threshold=timestamp,
        tokens=tokens,
    )
    restricted_value: list[bool] = []

    def restricted() -> bool:
        if not restricted_value:
            restricted_value.append(access_policy.has_restricted(session, actor, timestamp))
        return restricted_value[0]

    rows: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    needs_restricted = False
    _check_window_budget(deadline, cancel_event)
    restore_budget = _install_db_budget(session, deadline, cancel_event)
    try:
        result = session.execute(statement)
        for index, record in enumerate(result):
            if index % 256 == 0:
                _check_window_budget(deadline, cancel_event)
            values = record._mapping
            if _row_digest(values) != values["row_digest"]:
                raise KnowledgeProjectionUnavailable("projection_row_integrity")
            score = int(values["score"]) if tokens is not None else 0
            row = {
                "entry_id": values["entry_id"],
                "revision_id": values["current_revision_id"],
                "entry_version": values["entry_version"],
                "revision_version": values["revision_version"],
                "title": values["title"],
                "excerpt": values["body_excerpt"],
                "source_type": values["source_type"],
                "source_ref": values["source_ref"],
                "scope_type": values["scope_type"],
                "scope_ref": values["scope_ref"],
                "submitted_at": values["submitted_at"],
                "approved_by": values["approved_by"],
                "approved_at": values["approved_at"],
                "expires_at": values["expires_at"],
                "read_class": values["read_class"],
                "scope_reference_class": values["scope_reference_class"],
                "score": score,
            }
            if row["read_class"] == "restricted" or row["scope_reference_class"] != "public":
                needs_restricted = True
            collected.append(row)
    except sa.exc.OperationalError as error:
        reason = _budget_reason(deadline, cancel_event)
        if reason is not None:
            raise KnowledgeProjectionUnavailable(reason) from error
        raise
    finally:
        restore_budget()
    if needs_restricted and not restricted():
        collected = [row for row in collected if _visible_row(row, restricted=False)]
    rows = collected
    _check_window_budget(deadline, cancel_event)
    # R0: re-read the cheap generation fingerprint after scoring so a source
    # write that commits mid-read (PG READ COMMITTED) cannot make us serve the
    # previous generation's rows.
    if _head_signature(session) != (generation.generation, build.build_id):
        raise KnowledgeProjectionUnavailable("stale_during_read")
    return ProjectionWindow(
        generation=generation.generation, build_id=build.build_id,
        window_limit=build.window_limit, rows=tuple(rows),
        source_generation=build.source_generation,
        resolver_version=build.resolver_version,
        normalizer_version=build.normalizer_version,
        projection_schema_version=build.projection_schema_version,
        window_expires_at=build.window_expires_at,
    )


def _scope_matches(text_value: str, row: dict[str, Any]) -> bool:
    if row["scope_type"] not in ("product", "category"):
        return True
    reference = (row.get("scope_ref") or "").strip()
    if not reference:
        return False
    pattern = r"(?<![A-Za-z0-9_-])" + re.escape(reference) + r"(?![A-Za-z0-9_-])"
    return re.search(pattern, text_value, flags=re.IGNORECASE) is not None


def _citation_iso(value: datetime | None) -> str | None:
    # The legacy citation formatter serialises the stored value verbatim, so a
    # naive SQLite timestamp stays naive and a PG timestamptz keeps its offset.
    return None if value is None else value.isoformat()


def _citation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": row["entry_id"],
        "revision_id": row["revision_id"],
        "version": row["revision_version"],
        "title": row["title"],
        "excerpt": row["excerpt"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "approved_by": row["approved_by"],
        "approved_at": _citation_iso(row["approved_at"]),
        "expires_at": _citation_iso(row["expires_at"]),
        "scope_type": row["scope_type"],
        "scope_ref": row["scope_ref"],
    }


def search_projection(session: Session, actor, *, query: str, limit: int = 5,
                      now: datetime | None = None) -> dict[str, Any]:
    """Projection-backed search with the original scorer's observable semantics."""
    text_value = (query or "").strip()
    if not text_value:
        return {
            "query": query or "", "citations": [], "insufficient": True,
            "reason": "empty_query", "answer": None,
        }
    window = load_window(session, actor, now=now, query=query)
    rows = [row for row in window.rows if _scope_matches(text_value, row)]
    rows.sort(key=lambda row: (-row["score"], row["submitted_at"]))
    cap = max(1, min(int(limit), 20))
    citations = [_citation(row) for row in rows[:cap]]
    return {
        "query": text_value,
        "citations": citations,
        "insufficient": not citations,
        "reason": None if citations else "insufficient_evidence",
        "answer": None,
    }


# ---------------------------------------------------------------------------
# outbox integration and diagnostics


def enqueue_rebuild(
    session: Session,
    *,
    organization_id: str,
    store_id: str,
    actor_id: str,
    request_id: str | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> OutboxEvent:
    """Enqueue one explicit rebuild bound to the current source generation.

    The idempotency key names the scope, the target generation and the request
    id, so a retried request deduplicates while a rebuild for a newer generation
    is a distinct event.  A stale payload is reported as a stale receipt instead
    of being silently retargeted.
    """
    timestamp = _now(now)
    generation = session.get(KnowledgeSourceGeneration, 1)
    if generation is None:
        raise KnowledgeProjectionUnavailable("generation_missing")
    key = idempotency_key or (
        "knowledge.projection.rebuild:"
        f"{organization_id}:{store_id}:{generation.generation}:{uuid4()}"
    )
    # The payload is deterministic for a given key so outbox.enqueue can return
    # the existing event on a retried request (requested_at lives on the event,
    # not in the identity-bearing payload).
    request = request_id or key
    return outbox.enqueue(
        session,
        organization_id=organization_id,
        store_id=store_id,
        event_type=EVENT_TYPE,
        idempotency_key=key,
        payload={
            "actor_id": actor_id,
            "request_id": request,
            "expected_generation": generation.generation,
            "projection_schema_version": PROJECTION_SCHEMA_VERSION,
        },
        now=timestamp,
    )


class KnowledgeProjectionHandler:
    """Outbox handler; the runner has already committed the lease."""

    def __init__(self, session_factory, *, worker_id: str | None = None,
                 window_limit: int = WINDOW_LIMIT, now: datetime | None = None):
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._window_limit = window_limit
        # Tests inject the same clock the runner used to lease the event; real
        # callers leave it unset and the publish compares against wall time.
        self._now = now

    def __call__(self, event) -> str:
        actor_id = ""
        expected_generation = None
        if isinstance(event.payload, dict):
            candidate = event.payload.get("actor_id")
            if isinstance(candidate, str) and candidate.strip():
                actor_id = candidate
            expected = event.payload.get("expected_generation")
            if isinstance(expected, int) and not isinstance(expected, bool):
                expected_generation = expected
        actor_id = actor_id or f"outbox:{event.id}"
        result = rebuild_now(
            self._session_factory,
            actor_id=actor_id,
            worker_id=self._worker_id,
            event_id=event.id,
            organization_id=event.organization_id,
            store_id=event.store_id,
            window_limit=self._window_limit,
            now=self._now,
            expected_generation=expected_generation,
        )
        if result.get("state") == "stale":
            # A trusted stale receipt: the explicit later event rebuilds the
            # newer generation, this payload is never silently retargeted.
            return (
                "knowledge_projection:stale:"
                f"{result['build_id']}:{result.get('generation')}"
            )
        if result.get("state") != "published":
            raise RuntimeError(f"knowledge_projection_not_published:{result.get('state')}")
        return f"knowledge_projection:{result['build_id']}:{result['generation']}"


def projection_status(session: Session) -> dict[str, Any]:
    """Doctor/read model for operators; never raises for absent state."""
    generation = session.get(KnowledgeSourceGeneration, 1)
    report: dict[str, Any] = {
        "available": False,
        "generation": generation.generation if generation is not None else None,
        "format_version": generation.format_version if generation is not None else None,
        "build_id": generation.build_id if generation is not None else None,
        "reason": None,
    }
    if generation is None:
        report["reason"] = "generation_missing"
        return report
    if generation.format_version != PROJECTION_SCHEMA_VERSION:
        report["reason"] = "generation_format"
        return report
    if not generation.build_id:
        report["reason"] = "projection_missing"
        return report
    build = session.get(KnowledgeProjectionBuild, generation.build_id)
    if build is None:
        report["reason"] = "build_missing"
        return report
    report.update({
        "build_state": build.state,
        "build_source_generation": build.source_generation,
        "build_entry_count": build.entry_count,
        "window_limit": build.window_limit,
        "built_at": _iso(build.built_at),
        "published_at": _iso(build.published_at),
    })
    if build.state != "published":
        report["reason"] = "build_not_published"
        return report
    if build.source_generation != generation.generation:
        report["reason"] = "stale"
        return report
    rows = list(session.scalars(
        select(KnowledgeEntryProjection).where(KnowledgeEntryProjection.build_id == build.build_id)
    ))
    report["row_count"] = len(rows)
    report["entry_count_matches"] = len(rows) == build.entry_count == generation.entry_count
    report["projection_digest_matches"] = _projection_digest(rows) == build.projection_digest
    # Reproduce the snapshot threshold, not "now": natural expiry must not
    # make a healthy projection look inconsistent to the doctor.
    entries = window_entries(session, build.built_at, build.window_limit)
    revisions = _current_revisions(session, entries)
    report["window_count_now"] = len(entries)
    report["input_digest_matches"] = _input_digest(entries, revisions) == build.input_digest
    report["available"] = bool(
        report["entry_count_matches"]
        and report["projection_digest_matches"]
        and report["input_digest_matches"]
    )
    if not report["available"]:
        report["reason"] = "projection_inconsistent"
    return report


__all__ = [
    "EVENT_TYPE",
    "KnowledgeProjectionHandler",
    "KnowledgeProjectionUnavailable",
    "ProjectionUnavailable",
    "ProjectionWindow",
    "Snapshot",
    "enqueue_rebuild",
    "load_window",
    "projection_status",
    "published_head",
    "publish_build",
    "rebuild_now",
    "search_projection",
    "snapshot_scope",
    "stage_snapshot",
    "window_entries",
    "write_spool",
]
