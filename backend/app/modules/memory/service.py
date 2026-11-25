"""Personal session memory with atomic version writes and immutable command receipts."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.audit.service import record_event
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.auth.models import LoginSession
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority
from backend.app.application import commit_guard
from backend.app.modules.knowledge.commands import execute_command
from backend.app.modules.knowledge.models import KnowledgeEntry, KnowledgeRevision
from backend.app.modules.knowledge import access_policy
from backend.app.modules.knowledge.service import (
    submit_knowledge,
    review_knowledge,
    withdraw_knowledge,
    usable_entries,
    _audit as _audit_knowledge,
)
from backend.app.modules.memory.models import PersonalMemory, MemoryCommand
from backend.app.modules.memory.policy import (
    classify_content,
    filter_content_for_reader,
    validate_scope_promotion,
    RESTRICTED_PERMISSION,
    PROMOTE_PERMISSION,
)
from backend.app.modules.memory.schemas import MemoryOut

READ, WRITE = "memory.read", "memory.write"
MAX_TTL, DEFAULT_TTL = timedelta(days=30), timedelta(hours=24)

class MemoryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.utcoffset() is None:
        raise MemoryError("memory_time_invalid")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise MemoryError("memory_time_invalid") from None

def _require(session, actor, permission, now):
    session.flush()
    session.expire_all()
    from backend.app.modules.assistant.principal import WorkPrincipal, check
    if type(actor) is WorkPrincipal:
        check(session, actor, permission=permission, now=now, lock=True)
    else:
        validate_actor_session(session, actor, now=now)
    try:
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)
    except HTTPException as error:
        if error.status_code == 401:
            raise
        raise MemoryError("memory_permission_denied") from error


def _write_authority(session, actor, current):
    # Same canonical fence/epoch as logout and other application commands.
    # No command effects or explicit flush happen before this lock.
    with session.no_autoflush:
        epoch = lock_command_authority(session, actor)
        _require(session, actor, WRITE, current)
    return epoch


def _out(row):
    return MemoryOut.model_validate(row, from_attributes=True).model_dump()

def _session_deadline(session, actor):
    deadline = session.get(LoginSession, actor.session_id).expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return min(deadline, actor.auth_expires_at)


def _policy():
    raw = os.environ.get("MEMORY_PERSIST_MAX_SECONDS", "")
    if not raw.isascii() or not raw.isdigit() or not 0 < int(raw) <= int(MAX_TTL.total_seconds()):
        raise MemoryError("memory_policy_required")
    return timedelta(seconds=int(raw))

def _scope(actor):
    return (PersonalMemory.organization_id == actor.organization_id,
            PersonalMemory.store_id == actor.store_id, PersonalMemory.owner_id == actor.subject_id)

def _visibility_clause(actor):
    from backend.app.modules.assistant.principal import WorkPrincipal
    if type(actor) is WorkPrincipal:
        return PersonalMemory.persistent.is_(True)
    return or_(PersonalMemory.persistent.is_(True),
               PersonalMemory.origin_session_id == actor.session_id)


def _session_visible(row, actor):
    from backend.app.modules.assistant.principal import WorkPrincipal
    if type(actor) is WorkPrincipal:
        return bool(row.persistent)
    return bool(row.persistent or (row.origin_session_id and row.origin_session_id == actor.session_id))


def _unexpired(current):
    """Only explicitly durable rows may omit a deadline; legacy TTL stays fixed."""
    return or_(and_(PersonalMemory.retain_until_withdrawn.is_(True),
                    PersonalMemory.persistent.is_(True), PersonalMemory.expires_at.is_(None)),
               and_(PersonalMemory.retain_until_withdrawn.is_(False), PersonalMemory.expires_at > current))


def _is_unexpired(row, current):
    if row.retain_until_withdrawn:
        return bool(row.persistent and row.expires_at is None)
    return row.expires_at is not None and row.expires_at > current

def _row(session, actor, identity, current, *, withdrawn=False):
    row = session.scalar(select(PersonalMemory).where(*_scope(actor), PersonalMemory.id == identity)
                         .execution_options(populate_existing=True))
    if row is None or not _session_visible(row, actor) or (not withdrawn and
            (not row.active or not _is_unexpired(row, current))):
        raise MemoryError("memory_not_found")
    return row

def _command(session, actor, operation, key, payload, execute, current, epoch):
    connection = session.connection()
    if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    # Legacy rows have no immutable response. Do not execute their keys again
    # or fabricate an original response from mutable content.
    legacy_column = {"save": PersonalMemory.idempotency_key,
                     "correct": PersonalMemory.correction_key,
                     "withdraw": PersonalMemory.withdraw_key}.get(operation)
    if legacy_column is not None and isinstance(key, str) and session.scalar(select(PersonalMemory.id).where(
            *_scope(actor), legacy_column == key.strip()).limit(1)) is not None:
        raise MemoryError("memory_command_conflict")
    try:
        with session.begin_nested():
            result = execute_command(session, actor, operation=operation, key=key, payload=payload,
                                     execute=execute, receipt_model=MemoryCommand)
            # A receipt never restores visibility of revoked/sessionless content.
            _row(session, actor, result["id"], current, withdrawn=operation == "withdraw")
            commit_guard.enlist(session, action=WRITE, check=lambda db, instant:
                check_command_authority(db, actor, permission=WRITE, epoch=epoch, now=instant))
        return MemoryOut.model_validate(result).model_dump()
    except HTTPException as error:
        code = {"idempotency_conflict": "memory_idempotency_conflict",
                "idempotency_key_invalid": "memory_idempotency_invalid"}.get(
                    error.detail, "memory_command_conflict")
        raise MemoryError(code) from error

def _audit(session, actor, row, action, key, current):
    record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
        store_id=actor.store_id, action=f"memory.{action}", target_type="personal_memory", target_id=row.id,
        idempotency_key=f"memory:{action}:{row.id}:{row.version}",
        details={"version": row.version}, occurred_at=current)

def save_memory(session, actor, *, content, purpose=None, source="explicit", persist=False,
                expires_at=None, retain_until_withdrawn=False, idempotency_key, now=None):
    current = _now(now)
    from backend.app.modules.assistant.principal import WorkPrincipal
    if type(actor) is WorkPrincipal and persist is not True:
        raise MemoryError('memory_policy_required')
    epoch = _write_authority(session, actor, current)
    content = content.strip()
    if not 1 <= len(content) <= 5000:
        raise MemoryError("memory_content_invalid")
    if purpose is not None and not 1 <= len(purpose.strip()) <= 200:
        raise MemoryError("memory_purpose_invalid")
    if type(persist) is not bool or type(retain_until_withdrawn) is not bool:
        raise MemoryError("memory_policy_required")
    if retain_until_withdrawn:
        if (os.environ.get('MEMORY_DURABLE_ENABLED') != 'true' or not persist
                or not purpose or expires_at is not None):
            raise MemoryError('memory_policy_required')
        deadline = None
    elif persist:
        ttl = _policy()
        if not purpose or expires_at is None:
            raise MemoryError("memory_policy_required")
        deadline = _now(expires_at)
        if not current < deadline <= current + ttl:
            raise MemoryError("memory_expiry_invalid")
    else:
        deadline = min(current + DEFAULT_TTL, _session_deadline(session, actor))
        purpose = purpose or "session_context"
        if expires_at is not None:
            deadline = min(deadline, _now(expires_at))
        if deadline <= current:
            raise MemoryError("memory_expiry_invalid")
    payload = dict(content=content, purpose=purpose, source=source, persist=persist,
                   expires_at=_now(expires_at).isoformat() if expires_at else None)
    # Keep old request digests identical. Old callers never sent this new option.
    if retain_until_withdrawn:
        payload['retain_until_withdrawn'] = True
    def execute():
        row = PersonalMemory(id=str(uuid4()), organization_id=actor.organization_id, store_id=actor.store_id,
            owner_id=actor.subject_id, content=content, purpose=purpose, source=source[:80],
            origin_session_id=actor.session_id, persistent=persist, retain_until_withdrawn=retain_until_withdrawn,
            idempotency_key=None, create_digest=None, expires_at=deadline, version=1,
            active=True, created_at=current, updated_at=current)
        session.add(row)
        session.flush()
        _audit(session, actor, row, "saved", idempotency_key, current)
        return MemoryOut.model_validate(_out(row)).model_dump(mode="json")
    return _command(session, actor, "save", idempotency_key, payload, execute, current, epoch)

def list_memories(session, actor, *, now=None):
    current = _now(now)
    _require(session, actor, READ, current)
    rows = session.scalars(select(PersonalMemory).where(*_scope(actor), PersonalMemory.active.is_(True),
        _unexpired(current), _visibility_clause(actor))
        .order_by(PersonalMemory.updated_at.desc(), PersonalMemory.id))
    return [_out(row) for row in rows]

def get_memory(session, actor, memory_id, *, now=None):
    current = _now(now)
    _require(session, actor, READ, current)
    return _out(_row(session, actor, memory_id, current))

def _cas(session, actor, row, version, values, current):
    if type(version) is not int or version <= 0:
        raise MemoryError("memory_version_conflict")
    count = session.execute(update(PersonalMemory).where(*_scope(actor), PersonalMemory.id == row.id,
        PersonalMemory.active.is_(True), PersonalMemory.version == version, _unexpired(current))
        .values(**values, version=version+1, updated_at=current)
        .execution_options(synchronize_session=False)).rowcount
    if count != 1:
        raise MemoryError("memory_version_conflict")
    session.expire(row)
    session.refresh(row)

def _linked_knowledge(session, actor, memory_id):
    entries = session.scalars(select(KnowledgeEntry).where(
        KnowledgeEntry.organization_id == actor.organization_id,
        KnowledgeEntry.store_id == actor.store_id,
        KnowledgeEntry.status.in_(("pending_review", "approved"))))
    for entry in entries:
        _, _, facts, conflict = access_policy.inspect_entry(session, entry)
        if not conflict and any(f and f["origin_kind"] == "memory" and f["origin_memory_id"] == memory_id
                                for f in facts.values()):
            yield entry


def _invalidate_linked_knowledge(session, actor, memory_id, reason, current):
    for entry in _linked_knowledge(session, actor, memory_id):
        entry.status = "withdrawn"
        entry.withdrawn_by = actor.subject_id
        entry.withdrawn_at = current
        entry.withdraw_reason = reason
        entry.updated_at = current
        _audit_knowledge(session, actor, entry, "knowledge.withdrawn", f"auto_withdraw:{entry.id}:{entry.version}", current)


def correct_memory(session, actor, memory_id, *, content, expected_version, idempotency_key,
                   purpose=None, expires_at=None, now=None):
    current = _now(now)
    epoch = _write_authority(session, actor, current)
    content = content.strip()
    if not 1 <= len(content) <= 5000:
        raise MemoryError("memory_content_invalid")
    if purpose is not None and not 1 <= len(purpose.strip()) <= 200:
        raise MemoryError("memory_purpose_invalid")
    payload = dict(id=memory_id, content=content, purpose=purpose, expected_version=expected_version,
                   expires_at=_now(expires_at).isoformat() if expires_at else None)
    def execute():
        row = _row(session, actor, memory_id, current)
        values = {"content": content}
        if purpose is not None: values["purpose"] = purpose
        if expires_at is not None:
            if row.retain_until_withdrawn:
                raise MemoryError('memory_policy_required')
            deadline = _now(expires_at)
            cap = row.created_at + (_policy() if row.persistent else DEFAULT_TTL)
            if not row.persistent:
                cap = min(cap, _session_deadline(session, actor))
            if deadline <= current or deadline > cap: raise MemoryError("memory_policy_required")
            values["expires_at"] = deadline
        _cas(session, actor, row, expected_version, values, current)
        _invalidate_linked_knowledge(session, actor, row.id, "原个人记忆已被作者更正，经验候选自动撤回", current)
        _audit(session, actor, row, "corrected", idempotency_key, current)
        return MemoryOut.model_validate(_out(row)).model_dump(mode="json")
    return _command(session, actor, "correct", idempotency_key, payload, execute, current, epoch)

def withdraw_memory(session, actor, memory_id, *, expected_version, idempotency_key, now=None):
    current = _now(now)
    epoch = _write_authority(session, actor, current)
    def execute():
        row = _row(session, actor, memory_id, current)
        _cas(session, actor, row, expected_version,
             {"active": False, "expires_at": None if row.retain_until_withdrawn else current}, current)
        _invalidate_linked_knowledge(session, actor, row.id, "原个人记忆已被作者撤回", current)
        _audit(session, actor, row, "withdrawn", idempotency_key, current)
        return MemoryOut.model_validate(_out(row)).model_dump(mode="json")
    return _command(session, actor, "withdraw", idempotency_key,
                    dict(id=memory_id, expected_version=expected_version), execute, current, epoch)


def promote_memory_to_candidate(session, actor, memory_id, *, expected_version, title,
                                scope_type="store", scope_ref=None, idempotency_key, now=None):
    """Atomically bind exact source provenance; authorization precedes replay."""
    current = _now(now)
    _require(session, actor, WRITE, current)
    # Resolve scoped owner visibility before any receipt/effect.
    row = _row(session, actor, memory_id, current)
    _require(session, actor, PROMOTE_PERMISSION, current)
    if row.version != expected_version:
        raise MemoryError("memory_version_conflict")
    _require(session, actor, "knowledge.submit", current)
    title = title.strip()
    if not 1 <= len(title) <= 200:
        raise MemoryError("memory_title_invalid")
    try:
        validate_scope_promotion(actor_org_id=actor.organization_id, actor_store_id=actor.store_id,
            memory_org_id=row.organization_id, memory_store_id=row.store_id,
            target_scope_type=scope_type, target_scope_ref=scope_ref)
    except ValueError as error:
        raise MemoryError(str(error)) from error
    if classify_content("\n".join((row.content, title, scope_ref or "")))["is_restricted"] and not access_policy.has_restricted(session, actor, current):
        raise MemoryError("memory_restricted_topic_unauthorized")
    payload = dict(memory_id=memory_id, expected_version=expected_version, title=title,
                   scope_type=scope_type, scope_ref=scope_ref)

    def execute():
        if next(_linked_knowledge(session, actor, row.id), None) is not None:
            raise MemoryError("memory_already_promoted")
        entry = submit_knowledge(session, actor, title=title, body=row.content, source_type="manual",
            source_ref=f"memory:{row.id}", scope_type=scope_type, scope_ref=scope_ref, now=current,
            _origin=("memory", row.id, row.version))
        _audit(session, actor, row, "promoted_candidate", idempotency_key, current)
        return dict(id=entry.id, knowledge_entry_id=entry.id, current_revision_id=entry.current_revision_id,
                    memory_id=row.id, status=entry.status, version=entry.version, title=title,
                    submitted_by=actor.subject_id)

    try:
        connection = session.connection()
        if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        with session.begin_nested():
            result = execute_command(session, actor, operation="promote", key=idempotency_key,
                payload=payload, execute=execute, receipt_model=MemoryCommand)
            entry = session.get(KnowledgeEntry, result.get("knowledge_entry_id"), populate_existing=True)
            revision = session.get(KnowledgeRevision, result.get("current_revision_id"), populate_existing=True)
            if not entry or not access_policy.can_read(session, actor, entry, revision, now=current):
                raise MemoryError("memory_restricted_topic_unauthorized")
        from copy import deepcopy
        return deepcopy(result)
    except HTTPException as error:
        code = {"idempotency_conflict": "memory_idempotency_conflict",
                "idempotency_key_invalid": "memory_idempotency_invalid"}.get(error.detail, "memory_command_conflict")
        raise MemoryError(code) from error


def list_shared_business_memories(session, actor, *, now=None):
    """Only authorized proven memory-origin content; no keyword-only redaction."""
    current = _now(now)
    _require(session, actor, READ, current)
    result = []
    for entry, revision in usable_entries(session, actor, now=current):
        _, _, facts, conflict = access_policy.inspect_entry(session, entry)
        fact = facts.get(revision.id) if revision else None
        if not conflict and fact and fact["origin_kind"] == "memory":
            result.append(dict(entry_id=entry.id, title=revision.title, content=revision.body,
                source_ref=revision.source_ref, approved_by=entry.approved_by,
                status=entry.status, updated_at=entry.updated_at.isoformat()))
    return result
