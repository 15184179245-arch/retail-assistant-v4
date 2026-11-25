"""Versioned global knowledge projection tables (AGT-045 K-P slice).

The projection is derived state keyed by one monotonic source generation.  A
source generation is bumped by database triggers on every insert/update/delete
of an ordinary knowledge source table, so a build is publishable only while it
still reflects the generation it snapshotted.  Identity permission is never
materialized here: only the neutral, target-local access facts produced by
``access_policy.resolve_records`` are stored, and reads re-resolve the caller
against the current authorization tables.

This module only defines ORM metadata; the authoritative DDL (triggers,
singleton guards, dialect-specific constraints) lives in
``backend/migrations/versions/0077_knowledge_projection_generation.py``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db.base import Base
from backend.app.modules.agents.models import UTCDateTime

READ_CLASSES = ("public", "restricted", "conflict", "hold", "unverified", "missing")
BUILD_STATES = ("staging", "published", "retired")
SCOPE_REFERENCE_CLASSES = ("public", "restricted")
PROJECTION_SCHEMA_VERSION = 1
RESOLVER_VERSION = 1
NORMALIZER_VERSION = 1
WINDOW_LIMIT = 10_000


class KnowledgeSourceGeneration(Base):
    """Singleton authority row for the currently published projection.

    ``generation`` starts at 1 and is only advanced by the ``0077`` triggers and
    by a successful publish.  ``generation = build.source_generation + 1`` is
    the publish invariant; a direct ``UPDATE``/``REPLACE`` that resets it,
    overflows it, or deletes the row is rejected inside the database.
    """

    __tablename__ = "knowledge_source_generation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=PROJECTION_SCHEMA_VERSION)
    build_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_projection_builds.build_id", ondelete="RESTRICT"),
        nullable=True,
    )
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    built_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_knowledge_source_generation_singleton"),
        CheckConstraint("generation > 0", name="ck_knowledge_source_generation_positive"),
        CheckConstraint("generation <= 9223372036854775807", name="ck_knowledge_source_generation_max"),
        CheckConstraint("format_version = 1", name="ck_knowledge_source_generation_format"),
        CheckConstraint("entry_count >= 0", name="ck_knowledge_source_generation_entry_count"),
    )


class KnowledgeProjectionBuild(Base):
    """One immutable projection generation attempt; only ``published`` is head."""

    __tablename__ = "knowledge_projection_builds"

    build_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="staging")
    resolver_version: Mapped[int] = mapped_column(Integer, nullable=False, default=RESOLVER_VERSION)
    normalizer_version: Mapped[int] = mapped_column(Integer, nullable=False, default=NORMALIZER_VERSION)
    projection_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=PROJECTION_SCHEMA_VERSION
    )
    window_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=WINDOW_LIMIT)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    built_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    window_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    publisher_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_knowledge_projection_builds_event"),
        CheckConstraint("source_generation >= 0", name="ck_knowledge_projection_builds_source_generation"),
        CheckConstraint(
            "state IN ('staging','published','retired')",
            name="ck_knowledge_projection_builds_state",
        ),
        CheckConstraint(
            "resolver_version = 1 AND normalizer_version = 1 AND projection_schema_version = 1",
            name="ck_knowledge_projection_builds_versions",
        ),
        CheckConstraint("window_limit > 0 AND window_limit <= 10000", name="ck_knowledge_projection_builds_window"),
        CheckConstraint("entry_count >= 0", name="ck_knowledge_projection_builds_entry_count"),
        CheckConstraint(
            "(state <> 'published') OR (published_at IS NOT NULL AND publisher_id IS NOT NULL)",
            name="ck_knowledge_projection_builds_published_fields",
        ),
        Index("ix_knowledge_projection_builds_state", "state"),
    )


class KnowledgeEntryProjection(Base):
    """Neutral per-entry projection row; scope is a closed visibility domain.

    ``read_class`` and ``scope_reference_class`` are the only predicates a read
    may use to decide content visibility.  Both are computed from target-local
    knowledge facts, never from a cached principal decision.
    """

    __tablename__ = "knowledge_entry_projections"

    build_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_projection_builds.build_id", ondelete="CASCADE"),
        primary_key=True,
    )
    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    store_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revision_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    read_class: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Match authoritative 0077 DDL; do not narrow the stored epoch.
    access_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scope_reference_class: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_excerpt: Mapped[str] = mapped_column(String(400), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(500), nullable=True)
    lower_search_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    proof: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Digest over every stored field; the read path re-verifies each served row
    # so a tampered projection row fails closed instead of being returned.
    row_digest: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "read_class IN ('public','restricted','conflict','hold','unverified','missing')",
            name="ck_knowledge_entry_projections_read_class",
        ),
        CheckConstraint(
            "scope_reference_class IN ('public','restricted')",
            name="ck_knowledge_entry_projections_scope_class",
        ),
        CheckConstraint(
            "entry_version > 0 AND (revision_version IS NULL OR revision_version > 0)",
            name="ck_knowledge_entry_projections_versions",
        ),
        CheckConstraint("policy_version = 1 AND access_epoch > 0", name="ck_knowledge_entry_projections_epoch"),
        Index(
            "ix_knowledge_entry_projections_scope",
            "build_id",
            "organization_id",
            "store_id",
        ),
    )
