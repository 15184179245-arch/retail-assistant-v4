"""Submission, review, correction and withdrawal rules for KNW-001."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from backend.app.modules.knowledge import access_policy

SUBMIT_PERMISSION = "knowledge.submit"
REVIEW_PERMISSION = "knowledge.review"
READ_PERMISSION = "knowledge.read"
SOURCE_TYPES = {"manual", "file", "link", "import"}
ENTITY_TYPES = {"knowledge_entry"}


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(400, "timezone_required")
    return value.astimezone(timezone.utc)


def _has_permission(session: Session, actor: AuthenticatedSubject, key: str,
                    now: datetime) -> bool:
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=key, now=now)
        return True
    except HTTPException as exc:
        if exc.status_code == 403 and exc.detail == "permission_denied":
            return False
        raise


def _require(session: Session, actor: AuthenticatedSubject, key: str,
             now: datetime) -> None:
    if not _has_permission(session, actor, key, now):
        raise HTTPException(403, "knowledge_permission_denied")


def _cas(session: Session, entry: KnowledgeEntry, expected_version: int,
         values: dict) -> KnowledgeEntry:
    # Compare the observed revision/state as well as the caller's version.
    # Never flush ORM assignments before this conditional write.
    changed = session.execute(update(KnowledgeEntry).where(
        KnowledgeEntry.id == entry.id,
        KnowledgeEntry.organization_id == entry.organization_id,
        KnowledgeEntry.store_id == entry.store_id,
        KnowledgeEntry.version == expected_version,
        KnowledgeEntry.current_revision_id == entry.current_revision_id,
        KnowledgeEntry.status == entry.status,
    ).values(**values, version=expected_version + 1)
      .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise HTTPException(409, "knowledge_version_conflict")
    session.refresh(entry)
    return entry


def _entry(session: Session, actor: AuthenticatedSubject, entry_id: str) -> KnowledgeEntry:
    entry = session.scalar(select(KnowledgeEntry).where(
        KnowledgeEntry.id == entry_id,
        KnowledgeEntry.organization_id == actor.organization_id,
        KnowledgeEntry.store_id == actor.store_id,
    ).execution_options(populate_existing=True))
    if entry is None:
        # Other-store and unknown entries are indistinguishable to the caller.
        raise HTTPException(404, "knowledge_not_found")
    return entry


def _current_revision(session: Session, entry: KnowledgeEntry) -> KnowledgeRevision | None:
    if entry.current_revision_id is None:
        return None
    return session.get(KnowledgeRevision, entry.current_revision_id, populate_existing=True)


def _audit(session: Session, actor: AuthenticatedSubject, entry: KnowledgeEntry,
           action: str, key: str, now: datetime) -> None:
    record_event(session, actor_id=actor.subject_id,
                 organization_id=actor.organization_id,
                 store_id=actor.store_id, action=action,
                 target_type="knowledge_entry", target_id=entry.id,
                 idempotency_key=f"knowledge:{entry.id}:{action}:{key}",
                 details={"version": entry.version, "status": entry.status},
                 occurred_at=now)


def submit_knowledge(session: Session, actor: AuthenticatedSubject, *,
                     title: str, body: str, source_type: str,
                     source_ref: str | None = None, scope_type: str = "store",
                     scope_ref: str | None = None,
                     expires_at: datetime | None = None,
                     now: datetime | None = None, _origin: tuple | None = None) -> KnowledgeEntry:
    timestamp = _now(now)
    _require(session, actor, SUBMIT_PERMISSION, timestamp)
    if source_type not in SOURCE_TYPES:
        raise HTTPException(400, "invalid_source_type")
    if scope_type not in {"store", "product", "category", "global"}:
        raise HTTPException(400, "invalid_scope_type")
    if scope_type != "store" and not (scope_ref or "").strip():
        raise HTTPException(400, "scope_ref_required")
    if expires_at is not None:
        expires_at = _now(expires_at)
        if expires_at <= timestamp:
            raise HTTPException(400, "expiry_must_be_future")

    entry = KnowledgeEntry(
        id=str(uuid4()), organization_id=actor.organization_id,
        store_id=actor.store_id, scope_type=scope_type,
        scope_ref=(scope_ref or None), status="pending_review", version=1,
        submitted_by=actor.subject_id, submitted_at=timestamp,
        expires_at=expires_at, created_at=timestamp, updated_at=timestamp,
    )
    revision = KnowledgeRevision(
        id=str(uuid4()), entry_id=entry.id,
        organization_id=actor.organization_id, store_id=actor.store_id,
        version=1, title=title.strip(), body=body.strip(),
        source_type=source_type, source_ref=(source_ref or None),
        submitted_by=actor.subject_id, submitted_at=timestamp,
    )
    entry.current_revision_id = revision.id
    session.add_all([entry, revision])
    session.flush()
    _audit(session, actor, entry, "knowledge.submitted", revision.id, timestamp)
    origin = _origin or ("ordinary", None, None)
    access_policy.establish_new(session, entry, revision, origin_kind=origin[0],
                                memory_id=origin[1], memory_version=origin[2])
    return entry


def revise_knowledge(session: Session, actor: AuthenticatedSubject, entry_id: str, *,
                     expected_version: int, title: str, body: str, source_type: str,
                     source_ref: str | None = None, scope_type: str = "store",
                     scope_ref: str | None = None,
                     expires_at: datetime | None = None,
                     now: datetime | None = None) -> KnowledgeEntry:
    timestamp = _now(now)
    _require(session, actor, SUBMIT_PERMISSION, timestamp)
    entry = _entry(session, actor, entry_id)
    if entry.version != expected_version:
        raise HTTPException(409, "knowledge_version_conflict")
    if entry.status in {"withdrawn", "expired"}:
        raise HTTPException(409, "knowledge_revision_closed")
    if source_type not in SOURCE_TYPES:
        raise HTTPException(400, "invalid_source_type")
    if scope_type not in {"store", "product", "category", "global"}:
        raise HTTPException(400, "invalid_scope_type")
    if scope_type != "store" and not (scope_ref or "").strip():
        raise HTTPException(400, "scope_ref_required")
    if expires_at is not None:
        expires_at = _now(expires_at)
        if expires_at <= timestamp:
            raise HTTPException(400, "expiry_must_be_future")
    current_revision = _current_revision(session, entry)
    if current_revision is None:
        raise HTTPException(409, "knowledge_revision_missing")
    revision = KnowledgeRevision(
        id=str(uuid4()), entry_id=entry.id,
        organization_id=entry.organization_id, store_id=entry.store_id,
        version=current_revision.version + 1, title=title.strip(), body=body.strip(),
        source_type=source_type, source_ref=(source_ref or None),
        submitted_by=actor.subject_id, submitted_at=timestamp,
    )
    _cas(session, entry, expected_version, {
        "status": "pending_review", "current_revision_id": revision.id,
        "scope_type": scope_type, "scope_ref": scope_ref or None,
        "expires_at": expires_at, "updated_at": timestamp,
        "approved_by": None, "approved_at": None, "decision_reason": None,
    })
    session.add(revision)
    session.flush()
    _audit(session, actor, entry, "knowledge.revised", revision.id, timestamp)
    access_policy.establish_new(session, entry, revision)
    return entry


def review_knowledge(session: Session, actor: AuthenticatedSubject, entry_id: str, *,
                     expected_version: int, revision_id: str,
                     decision: str, reason: str | None = None,
                     now: datetime | None = None) -> KnowledgeEntry:
    timestamp = _now(now)
    _require(session, actor, REVIEW_PERMISSION, timestamp)
    entry = _entry(session, actor, entry_id)
    if entry.version != expected_version or entry.current_revision_id != revision_id:
        raise HTTPException(409, "knowledge_version_conflict")
    if decision == "verify_source":
        access_policy.verify_source(session, actor, entry, now=timestamp)
        return entry
    if entry.status != "pending_review":
        raise HTTPException(409, "knowledge_not_pending_review")
    current_revision = _current_revision(session, entry)
    if current_revision is None:
        raise HTTPException(409, "knowledge_revision_missing")
    if actor.subject_id in {entry.submitted_by, current_revision.submitted_by}:
        raise HTTPException(403, "knowledge_review_separation_required")
    if decision not in {"approve", "reject"}:
        raise HTTPException(400, "invalid_decision")
    if decision == "approve":
        if not access_policy.can_read(session, actor, entry, current_revision, now=timestamp):
            raise HTTPException(403, "knowledge_content_unavailable")
        # The historical-capable policy refreshes entry. Explicit source-memory
        # correction/withdrawal can change its status without incrementing its
        # version; never let the refreshed withdrawn state become the CAS's
        # new allowed starting state for an approval.
        if entry.version != expected_version or entry.current_revision_id != revision_id:
            raise HTTPException(409, "knowledge_version_conflict")
        if entry.status != "pending_review":
            raise HTTPException(409, "knowledge_not_pending_review")
        if actor.subject_id in {entry.submitted_by, current_revision.submitted_by}:
            raise HTTPException(403, "knowledge_review_separation_required")
    if decision == "approve" and entry.expires_at is not None and entry.expires_at <= timestamp:
        raise HTTPException(409, "knowledge_expired")
    _cas(session, entry, expected_version, {
        "status": "approved" if decision == "approve" else "rejected",
        "approved_by": actor.subject_id if decision == "approve" else None,
        "approved_at": timestamp if decision == "approve" else None,
        "decision_reason": reason or None, "updated_at": timestamp,
    })
    _audit(session, actor, entry, "knowledge.approved" if decision == "approve" else "knowledge.rejected",
           f"review:{entry.current_revision_id or entry.id}:{entry.version}", timestamp)
    return entry


def withdraw_knowledge(session: Session, actor: AuthenticatedSubject, entry_id: str, *,
                       expected_version: int, reason: str, now: datetime | None = None) -> KnowledgeEntry:
    timestamp = _now(now)
    entry = _entry(session, actor, entry_id)
    allowed = (_has_permission(session, actor, SUBMIT_PERMISSION, timestamp)
               or _has_permission(session, actor, REVIEW_PERMISSION, timestamp))
    if not allowed:
        raise HTTPException(403, "knowledge_permission_denied")
    if entry.status not in {"pending_review", "approved"}:
        raise HTTPException(409, "knowledge_not_withdrawable")
    _cas(session, entry, expected_version, {
        "status": "withdrawn", "withdrawn_by": actor.subject_id,
        "withdrawn_at": timestamp, "withdraw_reason": reason.strip(),
        "updated_at": timestamp,
    })
    _audit(session, actor, entry, "knowledge.withdrawn",
           f"withdraw:{entry.version}", timestamp)
    return entry


def _current_pairs(session, actor, statement, *, statuses, timestamp, human_preview=None):
    """Keep current content, publication and freeform metadata one fresh pair.

    can_read deliberately supports history and refreshes entry. Revalidate the
    observed identity/version AFTER it, so a concurrent revision/withdrawal
    cannot pair an old readable revision with a new entry's metadata.
    """
    result = []
    for entry in session.scalars(statement.execution_options(populate_existing=True)):
        observed = (entry.version, entry.current_revision_id)
        revision = _current_revision(session, entry)
        if not access_policy.can_read(session, actor, entry, revision,
                now=timestamp, human_preview=human_preview):
            continue
        if (revision is None or (entry.version, entry.current_revision_id) != observed
                or entry.current_revision_id != revision.id or revision.entry_id != entry.id
                or (entry.organization_id, entry.store_id) != (actor.organization_id, actor.store_id)
                or (revision.organization_id, revision.store_id) != (actor.organization_id, actor.store_id)
                or entry.status not in statuses
                or (entry.expires_at is not None and entry.expires_at <= timestamp)
                or not access_policy.can_read_reference(session, actor, entry.scope_ref, now=timestamp)):
            continue
        result.append((entry, revision))
    return result


def usable_entries(session: Session, actor: AuthenticatedSubject, *,
                   limit: int = 50, now: datetime | None = None
                   ) -> list[tuple[KnowledgeEntry, KnowledgeRevision | None]]:
    timestamp = _now(now)
    _require(session, actor, READ_PERMISSION, timestamp)
    statement = select(KnowledgeEntry).where(
        KnowledgeEntry.organization_id == actor.organization_id,
        KnowledgeEntry.store_id == actor.store_id,
        KnowledgeEntry.status == "approved",
        or_(KnowledgeEntry.expires_at.is_(None), KnowledgeEntry.expires_at > timestamp),
    ).order_by(KnowledgeEntry.submitted_at.desc()).limit(limit)
    return _current_pairs(session, actor, statement, statuses={"approved"}, timestamp=timestamp)


def entry_detail(session: Session, actor: AuthenticatedSubject, entry_id: str, *,
                 now: datetime | None = None
                 ) -> tuple[KnowledgeEntry, list[KnowledgeRevision]]:
    timestamp = _now(now)
    _require(session, actor, READ_PERMISSION, timestamp)
    entry = _entry(session, actor, entry_id)
    revisions = session.scalars(select(KnowledgeRevision).where(
        KnowledgeRevision.entry_id == entry.id
    ).order_by(KnowledgeRevision.version).execution_options(populate_existing=True)).all()
    return entry, [revision for revision in revisions
                   if access_policy.can_read(session, actor, entry, revision, now=timestamp)]


def review_queue(session: Session, actor: AuthenticatedSubject, *,
                 limit: int = 50, now: datetime | None = None, _human_preview=None
                 ) -> list[tuple[KnowledgeEntry, KnowledgeRevision | None]]:
    timestamp = _now(now)
    _require(session, actor, REVIEW_PERMISSION, timestamp)
    statement = select(KnowledgeEntry).where(
        KnowledgeEntry.organization_id == actor.organization_id,
        KnowledgeEntry.store_id == actor.store_id,
        KnowledgeEntry.status.in_(("pending_review", "approved")),
        or_(KnowledgeEntry.expires_at.is_(None), KnowledgeEntry.expires_at > timestamp),
    ).order_by(KnowledgeEntry.submitted_at.desc()).limit(limit)
    return _current_pairs(session, actor, statement, statuses={"pending_review", "approved"},
                          timestamp=timestamp, human_preview=_human_preview)


def materialize_usable_entries(session: Session, actor: AuthenticatedSubject, *,
                               limit: int = 50, now: datetime | None = None) -> tuple:
    """Current authorized plain records; caller still owns the transaction.

    The SQL window is deliberately BEFORE ACL, exactly like usable_entries.
    This is a foundation, not the later worker/result-root integration.
    """
    from backend.app.modules.identity.command_guard import refresh_authority
    from backend.app.modules.knowledge.retrieval import KnowledgeCandidate
    timestamp = _now(now)
    _require(session, actor, READ_PERMISSION, timestamp)
    statement = select(KnowledgeEntry).where(
        KnowledgeEntry.organization_id == actor.organization_id,
        KnowledgeEntry.store_id == actor.store_id,
        KnowledgeEntry.status == 'approved',
        or_(KnowledgeEntry.expires_at.is_(None), KnowledgeEntry.expires_at > timestamp),
    ).order_by(KnowledgeEntry.submitted_at.desc()).limit(limit)
    entries = list(session.scalars(statement.execution_options(populate_existing=True)))
    observed = {entry.id: (entry.version, entry.current_revision_id) for entry in entries}
    identities = list(observed)
    # Match can_read's fresh entry step in bounded queries, no per-entry refresh.
    for part in access_policy._chunks(identities):
        list(session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(part))
                             .execution_options(populate_existing=True)))
    inspections = access_policy.inspect_entries(session, entries)
    # Capture plain values BEFORE populate_existing can update these same ORM
    # instances. Tightening policy does not increment the entry's version.
    policy_signatures = {
        identity: {row.revision_id: tuple(getattr(row, field) for field in access_policy.ACCESS_FIELDS)
                   for row in inspection[1]}
        for identity, inspection in inspections.items()
    }
    revision_owners = {row.id: identity for identity, inspection in inspections.items()
                       for row in inspection[0]}
    # The final policy decision uses CURRENT authorization, not a cached allow.
    # This established helper expires authority only, not domain data/work.
    refresh_authority(session)
    final_time = _now(now)
    _require(session, actor, READ_PERMISSION, final_time)
    current = {}
    for part in access_policy._chunks(identities):
        for entry in session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.id.in_(part))
                                     .execution_options(populate_existing=True)):
            current[entry.id] = entry
    # All observed history participates, including foreign/corrupt policy rows;
    # an extra scope predicate here would hide a concurrent integrity change.
    current_policies = {identity: {} for identity in inspections}
    from backend.app.modules.knowledge.models import KnowledgeRevisionAccess
    for part in access_policy._chunks(list(revision_owners)):
        for row in session.scalars(select(KnowledgeRevisionAccess).where(
                KnowledgeRevisionAccess.revision_id.in_(part))
                .execution_options(populate_existing=True)):
            current_policies[revision_owners[row.revision_id]][row.revision_id] = tuple(
                getattr(row, field) for field in access_policy.ACCESS_FIELDS)
    restricted_value = []
    def restricted():
        if not restricted_value:
            restricted_value.append(access_policy.has_restricted(session, actor, final_time))
        return restricted_value[0]
    result = []
    for original in entries:
        entry = current.get(original.id)
        inspection = inspections[original.id]
        if current_policies[original.id] != policy_signatures[original.id]:
            continue
        revisions = {row.id: row for row in inspection[0]}
        revision = revisions.get(entry.current_revision_id) if entry is not None else None
        if (entry is None or revision is None
                or (entry.version, entry.current_revision_id) != observed[entry.id]
                or entry.status != 'approved'
                or entry.expires_at is not None and entry.expires_at <= final_time
                or (entry.organization_id, entry.store_id) != (actor.organization_id, actor.store_id)
                or (revision.organization_id, revision.store_id) != (actor.organization_id, actor.store_id)
                or revision.entry_id != entry.id):
            continue
        if not access_policy.can_read_prepared(actor, entry, revision, inspection, restricted=restricted):
            continue
        if access_policy._content_class(entry.scope_ref) != 'public' and not restricted():
            continue
        result.append(KnowledgeCandidate(entry_id=entry.id, revision_id=revision.id,
            entry_version=entry.version, revision_version=revision.version,
            title=revision.title, body=revision.body, source_type=revision.source_type,
            source_ref=revision.source_ref, scope_type=entry.scope_type, scope_ref=entry.scope_ref,
            submitted_at=entry.submitted_at, approved_by=entry.approved_by,
            approved_at=entry.approved_at, expires_at=entry.expires_at))
    return tuple(result)
