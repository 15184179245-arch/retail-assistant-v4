"""P1-C corrected Knowledge A -> B -> C vertical slice.

A: one short application Session reads the published K-P projection through
   ``projection.load_window``: the database computes the exact legacy token
   score over the pre-ACL ≤10,000 window and returns only compact citation
   metadata (no body / ``lower_search_bytes``).  The Session closes before any
   snapshot bytes or hash exist.
B: a fresh subprocess with no DB/credential environment only applies the
   original product/category scope regex, stable ``(-score, submitted_at)``
   order and the 1..20 cap.
C: a new Session re-reads the *current* published head with the current shared
   ACL, checks the full window's earliest expiry (including unmatched and
   restricted rows), pins generation/build/source/version identity, and returns
   citations only if the frozen input is byte-identical and the worker result
   equals the deterministic ranking of that frozen input.

The legacy ``retrieval.search_knowledge`` entry point is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
import hashlib
import os
import sqlite3
from pathlib import Path
import tempfile
import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.session import arm_sqlite_busy_timeout, close_sqlite_timeout_owned_session

from backend.app.modules.knowledge import projection, ranking, access_policy
from backend.app.modules.agents import readonly_compute


STALE_REASON = 'knowledge_source_changed'
HEAD_CHANGED_REASON = 'projection_head_changed'
AUTHORIZATION_CHANGED = 'knowledge_authorization_changed'
DEADLINE_REASON = 'tool_deadline_exceeded'
CLEANUP_REASON = 'worker_cleanup_unconfirmed'
DEFAULT_DEADLINE_SECONDS = 3.0
UNAVAILABLE_REASONS = frozenset({
    'projection_missing', 'build_missing', 'build_not_published', 'build_version',
    'generation_missing', 'generation_format', 'stale', 'entry_count_mismatch',
    'row_count_mismatch', 'projection_row_integrity', 'source_window_expired',
    'stale_during_read', DEADLINE_REASON, CLEANUP_REASON,
})


@dataclass(frozen=True, slots=True)
class PreparedKnowledgeSearch:
    organization_id: str
    store_id: str
    window_limit: int
    candidate_count: int
    generation: int
    build_id: str
    source_generation: int
    resolver_version: int
    normalizer_version: int
    projection_schema_version: int
    window_expires_at: str | None
    query: str
    rows: tuple[dict, ...]
    input_sha256: str
    snapshot_path: str
    snapshot_inode: int
    snapshot_device: int
    snapshot_size: int
    needs_restricted: bool
    created_at: str


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _compact(row: dict) -> dict:
    return {
        'entry_id': row['entry_id'],
        'revision_id': row['revision_id'],
        'entry_version': row['entry_version'],
        'revision_version': row['revision_version'],
        'title': row['title'],
        'excerpt': row.get('excerpt') or '',
        'source_type': row['source_type'],
        'source_ref': row.get('source_ref'),
        'scope_type': row['scope_type'],
        'scope_ref': row.get('scope_ref'),
        'submitted_at': _iso(row['submitted_at']),
        'approved_by': row.get('approved_by'),
        'approved_at': _iso(row.get('approved_at')),
        'expires_at': _iso(row.get('expires_at')),
        'score': int(row['score']),
    }


def _header(window, query: str) -> dict:
    return {
        'format': ranking.SNAPSHOT_FORMAT,
        'profile': ranking.SNAPSHOT_PROFILE,
        'count': len(window.rows),
        'generation': window.generation,
        'build_id': window.build_id,
        'source_generation': window.source_generation,
        'window_limit': window.window_limit,
        'window_expires_at': _iso(window.window_expires_at),
        'resolver_version': window.resolver_version,
        'normalizer_version': window.normalizer_version,
        'projection_schema_version': window.projection_schema_version,
        'query': query,
    }


def _write_snapshot(data: bytes) -> tuple[str, int, int]:
    directory = tempfile.mkdtemp(prefix='agt045-p1c-projection-')
    path = Path(directory) / 'snapshot.jsonl'
    try:
        os.chmod(directory, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        info = path.stat()
        return str(path), info.st_ino, info.st_size
    except BaseException:
        path.unlink(missing_ok=True)
        try:
            os.rmdir(directory)
        except OSError:
            pass
        raise


def _check_budget(deadline: float | None, cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise projection.KnowledgeProjectionUnavailable('cancelled')
    if deadline is not None and time.monotonic() >= deadline:
        raise projection.KnowledgeProjectionUnavailable('deadline_exceeded')


def _is_sqlite(session) -> bool:
    return session.get_bind().dialect.name == 'sqlite'


def begin_read(session, deadline: float | None = None) -> None:
    """Open the read transaction up front.

    SQLite's bounded wait remains in force through the owner's commit/rollback.
    The engine-local pool reset restores the original physical connection's
    timeout only after its transaction ended, before reuse. This preserves the
    prior BEGIN-time min(500, remaining) policy, not a hard total tool deadline.
    PG keeps its normal transaction (READ ONLY/RR is applied by its owner).
    """
    if _is_sqlite(session):
        if deadline is not None:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            arm_sqlite_busy_timeout(session, min(500, remaining_ms))
        try:
            session.execute(text('BEGIN IMMEDIATE'))
        except OperationalError as error:
            code = getattr(error.orig, 'sqlite_errorcode', None)
            if type(code) is int and (code & 0xff) in (
                    sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise projection.KnowledgeProjectionUnavailable('lock_timeout') from error
            raise


def prepare_search(session_factory, actor, *, query: str, now: datetime | None = None,
                   root_session=None,
                   window_limit: int = 10_000, deadline: float | None = None,
                   cancel_event=None) -> PreparedKnowledgeSearch:
    _check_budget(deadline, cancel_event)
    session = root_session if root_session is not None else session_factory()
    owned = root_session is None
    primary = None
    try:
        if owned:
            begin_read(session, deadline=deadline)
        window = projection.load_window(session, actor, now=now, query=query,
                                        deadline=deadline, cancel_event=cancel_event)
    except BaseException as error:
        primary = error
        raise
    finally:
        # A is a short read: close before serialising or hashing anything.
        if owned:
            close_sqlite_timeout_owned_session(session, primary)
    if window.window_limit != window_limit:
        raise projection.KnowledgeProjectionUnavailable('window_limit_mismatch')
    rows_list = []
    for index, row in enumerate(window.rows):
        if index % 256 == 0:
            _check_budget(deadline, cancel_event)
        rows_list.append(_compact(row))
    rows = tuple(rows_list)
    needs_restricted = any(
        row.get('read_class') == 'restricted' or row.get('scope_reference_class') != 'public'
        for row in window.rows)
    header = _header(window, query)
    data = ranking.snapshot_bytes(header, rows)
    path, inode, size = _write_snapshot(data)
    try:
        device = os.stat(path).st_dev
    except BaseException:
        cleanup_path = Path(path)
        cleanup_path.unlink(missing_ok=True)
        try:
            cleanup_path.parent.rmdir()
        except OSError:
            pass
        raise
    return PreparedKnowledgeSearch(
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        window_limit=window.window_limit,
        candidate_count=len(rows),
        generation=window.generation,
        build_id=window.build_id,
        source_generation=window.source_generation,
        resolver_version=window.resolver_version,
        normalizer_version=window.normalizer_version,
        projection_schema_version=window.projection_schema_version,
        window_expires_at=header['window_expires_at'],
        query=query,
        rows=rows,
        input_sha256=hashlib.sha256(data).hexdigest(),
        snapshot_path=path,
        snapshot_inode=inode,
        snapshot_device=device,
        snapshot_size=size,
        needs_restricted=needs_restricted,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def cleanup(prepared: PreparedKnowledgeSearch) -> None:
    path = Path(prepared.snapshot_path)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def score_prepared(prepared: PreparedKnowledgeSearch, *, deadline: float,
                   cancel_event=None, limit: int = 5) -> dict:
    envelope = {
        'profile': ranking.SNAPSHOT_PROFILE,
        'input_sha256': prepared.input_sha256,
        'snapshot_path': prepared.snapshot_path,
        'snapshot_inode': prepared.snapshot_inode,
        'snapshot_device': prepared.snapshot_device,
        'snapshot_size': prepared.snapshot_size,
        'query_sha256': hashlib.sha256(prepared.query.encode('utf-8')).hexdigest(),
        'limit': int(limit),
    }
    return readonly_compute.invoke(envelope, deadline=deadline,
                                   cancel_event=cancel_event)


def _unavailable(reason: str, *, query: str, input_sha256: str | None = None,
                 timings: dict | None = None) -> dict:
    result = {'state': 'unavailable', 'reason': reason, 'query': query,
              'citations': [], 'insufficient': True, 'answer': None}
    if input_sha256 is not None:
        result['input_sha256'] = input_sha256
    if timings is not None:
        result['timings'] = timings
    return result


def _stale(reason: str, prepared: PreparedKnowledgeSearch) -> dict:
    return {'state': 'stale', 'reason': reason, 'query': prepared.query,
            'citations': [], 'insufficient': True, 'answer': None,
            'input_sha256': prepared.input_sha256}


def _same_head(prepared: PreparedKnowledgeSearch, window) -> bool:
    return (
        window.generation == prepared.generation
        and window.build_id == prepared.build_id
        and window.source_generation == prepared.source_generation
        and window.window_limit == prepared.window_limit
        and window.resolver_version == prepared.resolver_version
        and window.normalizer_version == prepared.normalizer_version
        and window.projection_schema_version == prepared.projection_schema_version
        and _iso(window.window_expires_at) == prepared.window_expires_at
    )


def finalize_prepared(session_factory, prepared: PreparedKnowledgeSearch, actor, *,
                      clock=None, result: dict | None = None, limit: int = 5,
                      deadline: float | None = None, cancel_event=None,
                      root_session=None) -> dict:
    _check_budget(deadline, cancel_event)
    current_now = (clock or (lambda: datetime.now(timezone.utc)))()
    session = root_session if root_session is not None else session_factory()
    owned = root_session is None
    primary = None
    try:
        if owned:
            begin_read(session, deadline=deadline)
        try:
            window = projection.load_window(session, actor, now=current_now,
                                            query=prepared.query, deadline=deadline,
                                            cancel_event=cancel_event)
        except HTTPException as error:
            if error.status_code in (401, 403):
                return _stale(AUTHORIZATION_CHANGED, prepared)
            raise
        except projection.KnowledgeProjectionUnavailable as error:
            reason = str(error.args[0]) if error.args else 'projection_unavailable'
            # A published build that no longer describes the current source
            # generation is exactly the "old snapshot must not masquerade as
            # fresh" case: report the explicit stale state, not an unavailable
            # projection.  Natural window expiry (and genuinely missing heads)
            # stays unavailable.
            if reason in ('stale', 'stale_during_read'):
                return _stale(STALE_REASON, prepared)
            return _unavailable(reason, query=prepared.query,
                                input_sha256=prepared.input_sha256)
    except BaseException as error:
        primary = error
        raise
    finally:
        if owned:
            close_sqlite_timeout_owned_session(session, primary)
    _check_budget(deadline, cancel_event)
    if not _same_head(prepared, window):
        return _stale(HEAD_CHANGED_REASON, prepared)
    current_rows_list = []
    for index, row in enumerate(window.rows):
        if index % 256 == 0:
            _check_budget(deadline, cancel_event)
        current_rows_list.append(_compact(row))
    current_rows = tuple(current_rows_list)
    _check_budget(deadline, cancel_event)
    current = ranking.snapshot_bytes(_header(window, prepared.query), current_rows)
    if hashlib.sha256(current).hexdigest() != prepared.input_sha256:
        return _stale(STALE_REASON, prepared)
    if result is None:
        raise ValueError('worker_result_required')
    expected = ranking.rank_candidates(prepared.rows, query=prepared.query, limit=limit)
    if result != expected:
        raise readonly_compute.WorkerError('worker_result_mismatch')
    if root_session is not None:
        # K2: the result joins the caller's existing root transaction.  Pin the
        # exact projection identity/ACL/expiry again at commit time so the
        # root commit cannot land on a head that moved after C.
        from backend.app.application import commit_guard
        commit_guard.enlist(root_session, action='p1c_knowledge_result',
                            check=projection.pin_prepared(actor, prepared, deadline=deadline,
                                               cancel_event=cancel_event))
    return {'state': 'ready', **expected}


def compute_search(session_factory, actor, *, query: str, limit: int = 5,
                   now: datetime | None = None, clock=None,
                   window_limit: int = 10_000,
                   deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
                   remaining_seconds: float | None = None,
                   cancel_event=None, root_session=None) -> dict:
    """Explicit P1-C entry point; legacy ``search_knowledge`` is unchanged.

    ``deadline_seconds`` is the whole-tool budget from this call's start and is
    further capped by ``remaining_seconds`` when the caller has a run deadline.
    A/C use the remaining budget; B may not start after it is consumed.
    """
    if session_factory is None:
        if root_session is None:
            raise ValueError('session_factory_required')
        session_factory = sessionmaker(bind=root_session.get_bind(), class_=Session,
                                       autoflush=False, expire_on_commit=False)
    started = time.monotonic()
    budget = float(deadline_seconds)
    if remaining_seconds is not None:
        budget = min(budget, max(0.0, float(remaining_seconds)))
    deadline = started + budget
    text = (query or '').strip()
    if not text:
        return {'state': 'ready', **ranking.rank_candidates([], query=query, limit=limit)}
    prepared = None
    timings = {'a_ms': None, 'b_ms': None, 'c_ms': None,
               'total_ms': None, 'deadline_seconds': budget}
    try:
        try:
            prepared = prepare_search(session_factory, actor, query=text, now=now,
                                      window_limit=window_limit, deadline=deadline,
                                      cancel_event=cancel_event, root_session=root_session)
        except projection.KnowledgeProjectionUnavailable as error:
            reason = str(error.args[0]) if error.args else 'projection_unavailable'
            if reason == 'deadline_exceeded':
                reason = DEADLINE_REASON
            elif reason == 'cancelled':
                reason = 'worker_cancelled'
            return _unavailable(reason, query=text)
        timings['a_ms'] = round((time.monotonic() - started) * 1000, 1)
        if time.monotonic() >= deadline:
            timings['total_ms'] = round((time.monotonic() - started) * 1000, 1)
            return _unavailable(DEADLINE_REASON, query=text,
                                input_sha256=prepared.input_sha256, timings=timings)
        b_started = time.monotonic()
        try:
            worker = score_prepared(prepared, deadline=deadline,
                                    cancel_event=cancel_event, limit=limit)
        except readonly_compute.WorkerError as error:
            if error.code == 'worker_timeout':
                reason = DEADLINE_REASON
            elif error.code == 'worker_cancelled':
                reason = 'worker_cancelled'
            elif (error.code == 'worker_transport_failed'
                  and error.detail == 'transport_cleanup_unconfirmed'):
                reason = CLEANUP_REASON
            else:
                raise
            timings['b_ms'] = round((time.monotonic() - b_started) * 1000, 1)
            timings['total_ms'] = round((time.monotonic() - started) * 1000, 1)
            return _unavailable(reason, query=text,
                                input_sha256=prepared.input_sha256, timings=timings)
        timings['b_ms'] = round((time.monotonic() - b_started) * 1000, 1)
        c_started = time.monotonic()
        try:
            confirmed = finalize_prepared(session_factory, prepared, actor, clock=clock,
                                          result=worker['result'], limit=limit,
                                          deadline=deadline, cancel_event=cancel_event,
                                          root_session=root_session)
        except projection.KnowledgeProjectionUnavailable as error:
            reason = str(error.args[0]) if error.args else 'projection_unavailable'
            if reason == 'deadline_exceeded':
                reason = DEADLINE_REASON
            elif reason == 'cancelled':
                reason = 'worker_cancelled'
            timings['c_ms'] = round((time.monotonic() - c_started) * 1000, 1)
            timings['total_ms'] = round((time.monotonic() - started) * 1000, 1)
            return _unavailable(reason, query=text,
                                input_sha256=prepared.input_sha256, timings=timings)
        timings['c_ms'] = round((time.monotonic() - c_started) * 1000, 1)
        timings['total_ms'] = round((time.monotonic() - started) * 1000, 1)
        if time.monotonic() >= deadline:
            return _unavailable(DEADLINE_REASON, query=text,
                                input_sha256=prepared.input_sha256, timings=timings)
        if confirmed['state'] != 'ready':
            return {**confirmed, 'input_sha256': prepared.input_sha256,
                    'timings': timings}
        response = dict(confirmed)
        response.update({
            'input_sha256': prepared.input_sha256,
            'generation': prepared.generation,
            'build_id': prepared.build_id,
            'source_generation': prepared.source_generation,
            'candidate_count': prepared.candidate_count,
            'snapshot_bytes': prepared.snapshot_size,
            'timings': timings,
        })
        return response
    finally:
        if prepared is not None:
            cleanup(prepared)
