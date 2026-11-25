"""CORE-009 read-only facade, borrowing a transaction without owning its fate.

Pending caller DML is rejected intact. A separate identity map rechecks current
rows on the borrowed connection; it never commits, rolls back, expires or
flushes the caller Session. Two matching reads are required before disclosure.
This is not an external-network atomicity or target-PostgreSQL guarantee.
"""
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session

from backend.app.modules.evidence import adapters
from backend.app.modules.evidence.schemas import build_query_page


class _ReadSession(Session):
    def flush(self, objects=None):
        if self.new or self.dirty or self.deleted:
            raise HTTPException(409, 'evidence_query_write_attempt')
        # Existing read policies explicitly flush their own clean identity map.
        # Do not call flush on the borrowed session or create a DB write.

    def commit(self):
        raise HTTPException(409, 'evidence_query_write_attempt')

    def rollback(self):
        raise HTTPException(409, 'evidence_query_write_attempt')


def _clean(session):
    if session.new or session.dirty or session.deleted:
        raise HTTPException(409, 'evidence_query_pending_changes')
    if not session.is_active:
        raise HTTPException(409, 'evidence_query_session_unavailable')


@contextmanager
def _reader(session):
    _clean(session)
    connection = session.connection()

    def select_only(conn, cursor, statement, parameters, context, executemany):
        # The frozen adapters use compiled SELECTs only; no raw SQL, DML or DDL.
        if context.compiled is None or not getattr(context.compiled.statement, 'is_select', False):
            raise HTTPException(409, 'evidence_query_write_attempt')

    event.listen(connection, 'before_cursor_execute', select_only)
    reader = _ReadSession(bind=connection, autoflush=False, expire_on_commit=False,
                          join_transaction_mode='rollback_only')
    try:
        yield reader
        _clean(session)
    finally:
        # rollback_only on an externally owned transaction: close relinquishes
        # this identity map, never the caller's transaction/connection.
        reader.close()
        event.remove(connection, 'before_cursor_execute', select_only)


def _inputs(kind, offset, limit, query, reference):
    if type(kind) is not str or kind not in adapters.PERMISSIONS:
        raise HTTPException(422, 'evidence_kind_invalid')
    if type(offset) is not int or not 0 <= offset <= adapters.MAX_SOURCES:
        raise HTTPException(422, 'evidence_page_invalid')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise HTTPException(422, 'evidence_page_invalid')
    if (kind == 'search' and (type(query) is not str or not query.strip() or len(query) > 200)
            or kind != 'search' and query is not None):
        raise HTTPException(422, 'evidence_query_invalid')
    if reference is not None:
        keys = {'kind', 'object_id', 'version_ref', 'version', 'source_version'}
        if (type(reference) is not dict or set(reference) != keys
                or any(type(v) is not str or not 0 < len(v) <= 200 for v in reference.values())
                or reference['kind'] != ('catalog' if kind == 'search' else kind)):
            raise HTTPException(422, 'evidence_reference_invalid')


def query_evidence(session, actor, *, kind, offset=0, limit=20, query=None, reference=None):
    """An authorized query/reference lookup, not permission to create a receipt."""
    _inputs(kind, offset, limit, query, reference)
    with _reader(session) as reader:
        first = adapters.read_sources(reader, actor, kind=kind, query=query)
        reader.expire_all()
        second = adapters.read_sources(reader, actor, kind=kind, query=query)
        if first != second:
            raise HTTPException(409, 'evidence_source_changed')
        items, incomplete = second
        if reference is not None:
            items = [item for item in items if all(item[key] == value for key, value in reference.items())]
            if len(items) != 1:
                raise HTTPException(404, 'evidence_source_unavailable')
            incomplete = False
        return build_query_page(kind, items, offset=offset, limit=limit, truncated=incomplete,
                                queried_at=datetime.now(timezone.utc).isoformat())


def read_receipt_evidence(session, actor, run_id):
    """Only persisted, server-created receipts select citations for this view."""
    from backend.app.modules.agents import answer_receipts
    from backend.app.modules.agents.models import AgentRun
    from sqlalchemy import select

    if type(run_id) is not str or not 0 < len(run_id) <= 120:
        raise HTTPException(422, 'evidence_reference_invalid')
    with _reader(session) as reader:
        def observe():
            run = reader.scalar(select(AgentRun).where(AgentRun.id == run_id,
                AgentRun.organization_id == actor.organization_id, AgentRun.store_id == actor.store_id))
            if run is None:
                raise HTTPException(404, 'evidence_source_unavailable')
            original = answer_receipts.read_answer(reader, actor, run)
            if original['answer_status'] != 'available':
                return {'answer': original, 'evidence': build_query_page('answer', [], offset=0,
                    limit=20, truncated=False, queried_at='unknown')}
            items = []
            # read_answer has already verified ALL dependencies and filtered
            # own_evidence. Do not accept caller/model citation lists here.
            for citation in original['citations']:
                if citation['kind'] == 'guanjia_category':
                    from backend.app.modules.integrations.guanjia import category_host
                    reference = {'kind':'guanjia_category','id':citation['object_id'],'sha256':citation['version_ref']}
                    if category_host.citation(reader.get_bind().engine,actor,reference) != citation:
                        raise HTTPException(409,'evidence_source_unavailable')
                    items.append(adapters._item(actor,kind='guanjia_category',identity=citation['object_id'],
                        version_ref=citation['version_ref'],version=citation['version'],source_version=None,as_of=None,
                        content=dict(citation),owner=actor.subject_id,verification='receipt_checked'))
                    continue
                if citation['kind'] == 'guanjia_observation':
                    from backend.app.modules.integrations.guanjia import observation_receipts
                    reference = {'kind': 'guanjia_observation', 'id': citation['object_id'],
                                 'sha256': citation['version_ref']}
                    checked = observation_receipts.citation(reader.get_bind().engine, actor, reference)
                    if checked != citation:
                        raise HTTPException(409, 'evidence_source_unavailable')
                    # Only source metadata; observation time is not source time
                    # and cannot establish a source freshness watermark.
                    items.append(adapters._item(actor, kind='guanjia_observation',
                        identity=citation['object_id'], version_ref=citation['version_ref'],
                        version=citation['version'], source_version=None, as_of=None,
                        content=dict(citation), owner=actor.subject_id, verification='receipt_checked'))
                    continue
                sources, _ = adapters.read_sources(reader, actor, kind=citation['kind'])
                matches = [item for item in sources if item['object_id'] == citation['object_id']
                    and item['version_ref'] == citation['version_ref']
                    and item['source_version'] == citation['version']]
                if len(matches) != 1:
                    raise HTTPException(409, 'evidence_source_unavailable')
                # Receipt metadata never adds source bodies or personal memory.
                item = dict(matches[0]); item['content'] = dict(citation)
                items.append(item)
            return {'answer': original, 'evidence': build_query_page('answer', items,
                offset=0, limit=len(items) or 1, truncated=False, queried_at='unknown')}
        first = observe()
        reader.expire_all()
        second = observe()
        if first != second:
            raise HTTPException(409, 'evidence_source_changed')
        return second
