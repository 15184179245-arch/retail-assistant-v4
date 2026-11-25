"""Read adapters over existing owners. No ORM models, writes, or external IO.

A source_checked observation is not a claim that upstream/business data is true.
The adapters run only in service's read-only session, never a caller session.
"""
from datetime import datetime, timezone
from hashlib import sha256

from backend.app.modules.catalog.service import _canonical

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.knowledge import service as knowledge, access_policy
from backend.app.modules.knowledge.models import KnowledgeEntry
from backend.app.modules.memory import service as memory
from backend.app.modules.memory.models import PersonalMemory
from backend.app.modules.analytics import service as analytics
from backend.app.modules.analytics.models import AnalyticsSnapshot
from backend.app.modules.search import service as search

MAX_SOURCES = 1000
PERMISSIONS = {'catalog': 'catalog.read', 'search': 'catalog.read',
               'knowledge': 'knowledge.read', 'memory': 'memory.read',
               'analytics': 'analytics.read'}


def _time(value):
    if value is None:
        return 'unknown'
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _scope(model, actor):
    return (model.organization_id == actor.organization_id, model.store_id == actor.store_id)


def _item(actor, *, kind, identity, version_ref, version, source_version, as_of,
          content, owner=None, coverage='unknown', verification='source_checked'):
    return {'kind': kind, 'object_id': identity, 'version_ref': version_ref,
        'version': str(version), 'source_version': str(source_version) if source_version is not None else 'unknown',
        'source_identity': {'organization_id': actor.organization_id, 'store_id': actor.store_id,
                            'owner_id': owner if owner is not None else 'unknown'},
        'as_of': _time(as_of), 'coverage': coverage, 'truncated': 'unknown',
        'verification': verification, 'content': content}


def _catalog_item(session, actor, product):
    snapshot = session.scalar(select(CatalogSnapshot).where(
        *_scope(CatalogSnapshot, actor), CatalogSnapshot.id == product.current_snapshot_id))
    if (snapshot is None or snapshot.product_id != product.id
            or snapshot.source_version != product.source_version):
        raise HTTPException(409, 'evidence_source_unavailable')
    observation = {'payload': snapshot.payload, 'source_version': snapshot.source_version,
                   'source_updated_at': snapshot.source_updated_at.isoformat() if snapshot.source_updated_at else None}
    if sha256(_canonical(observation).encode('utf-8')).hexdigest() != snapshot.payload_sha256:
        raise HTTPException(409, 'evidence_source_unavailable')
    payload = snapshot.payload or {}
    # Do not expose raw snapshots/costs through a catalog.read-only permission.
    content = {'external_code': product.external_code, 'source_system': product.source_system,
               **{key: payload.get(key) for key in ('name', 'barCode', 'spec', 'unit')}}
    return _item(actor, kind='catalog', identity=product.id, version_ref=snapshot.id,
        version=product.version, source_version=snapshot.source_version,
        as_of=snapshot.captured_at, content=content)


def read_sources(session, actor, *, kind, query=None):
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission=PERMISSIONS[kind])
    if kind == 'catalog':
        rows = list(session.scalars(select(CatalogProduct).where(
            *_scope(CatalogProduct, actor), CatalogProduct.active.is_(True))
            .order_by(CatalogProduct.id).limit(MAX_SOURCES + 1)))
        return [_catalog_item(session, actor, row) for row in rows[:MAX_SOURCES]], len(rows) > MAX_SOURCES
    if kind == 'search':
        # The deterministic DB search is authoritative for its own bounded
        # result. Optional filesystem/derived suggestions cannot mint evidence.
        result = search._search_products(session, organization_id=actor.organization_id,
            store_id=actor.store_id, query=query, limit=MAX_SOURCES)
        items = []
        by_source = {}
        matches = [*result['exact'], *result['candidates']]
        for match in matches[:MAX_SOURCES]:
            product = session.scalar(select(CatalogProduct).where(
                *_scope(CatalogProduct, actor), CatalogProduct.id == match['product_id'],
                CatalogProduct.active.is_(True)))
            if product is None or product.source_version != match['source_version']:
                raise HTTPException(409, 'evidence_source_unavailable')
            item = _catalog_item(session, actor, product)
            if item['as_of'] != _time(match['captured_at']):
                raise HTTPException(409, 'evidence_source_unavailable')
            observation = {key: match[key] for key in
                           ('match_field', 'match_type', 'original_value', 'normalized_value')}
            key = (item['object_id'], item['version_ref'])
            if key in by_source:
                by_source[key]['content']['search_matches'].append(observation)
                continue
            item['content']['search_matches'] = [observation]
            item['verification'] = 'candidate_only' if match in result['candidates'] else 'source_checked'
            by_source[key] = item
            items.append(item)
        return items, bool(result['has_more'] or result['scan_truncated'] or result['truncated']
                           or len(matches) > MAX_SOURCES)
    if kind == 'knowledge':
        # Preserve the established publication, expiry and per-revision ACL.
        pairs = knowledge.usable_entries(session, actor, limit=MAX_SOURCES + 1)
        candidates = list(session.scalars(select(KnowledgeEntry.id).where(
            *_scope(KnowledgeEntry, actor), KnowledgeEntry.status == 'approved')
            .limit(MAX_SOURCES + 1)))
        items = []
        for entry, revision in pairs[:MAX_SOURCES]:
            if revision is None or not access_policy.can_read_reference(session, actor, entry.scope_ref):
                continue
            items.append(_item(actor, kind=kind, identity=entry.id, version_ref=revision.id,
                version=entry.version, source_version=revision.version, as_of=entry.approved_at,
                owner=entry.submitted_by, verification='approved_source',
                content={'title': revision.title, 'body': revision.body,
                         'source_type': revision.source_type, 'source_ref': revision.source_ref}))
        return sorted(items, key=lambda item: item['object_id']), len(candidates) > MAX_SOURCES
    if kind == 'memory':
        # A real active login and the existing owner/session policy are required.
        # Read each bounded row through the original policy, not an imitation.
        memory._require(session, actor, memory.READ, datetime.now(timezone.utc))
        rows = list(session.scalars(select(PersonalMemory).where(
            *memory._scope(actor), PersonalMemory.active.is_(True),
            memory._unexpired(datetime.now(timezone.utc)))
            .order_by(PersonalMemory.id).limit(MAX_SOURCES + 1)))
        items = []
        for row in rows[:MAX_SOURCES]:
            if not memory._session_visible(row, actor):
                continue
            value = memory.get_memory(session, actor, row.id)
            items.append(_item(actor, kind=kind, identity=row.id, version_ref=row.id,
                version=value['version'], source_version=value['version'], as_of=value['updated_at'],
                owner=actor.subject_id, verification='owner_only',
                content={'content': value['content'], 'purpose': value['purpose'],
                         'source': value['source'], 'persistent': row.persistent,
                         'expires_at': None if row.retain_until_withdrawn else _time(value['expires_at']),
                         'retain_until_withdrawn': row.retain_until_withdrawn, 'authority': 'personal_memory'}))
        return items, len(rows) > MAX_SOURCES
    if kind == 'analytics':
        analytics._scope(session, actor)
        rows = list(session.scalars(select(AnalyticsSnapshot).where(*_scope(AnalyticsSnapshot, actor))
            .order_by(AnalyticsSnapshot.id).limit(MAX_SOURCES + 1)))
        items = []
        for row in rows[:MAX_SOURCES]:
            view = analytics.read_snapshot(session, actor, row.id)
            if not view['fresh']:
                raise HTTPException(409, 'evidence_source_unavailable')
            items.append(_item(actor, kind=kind, identity=row.id, version_ref=row.id,
                version=row.version, source_version=row.source_digest, as_of=row.source_last_recorded_at,
                owner=row.created_by, coverage=row.coverage_kind, verification='unknown', content=view))
        return items, len(rows) > MAX_SOURCES
    raise HTTPException(422, 'evidence_kind_invalid')
