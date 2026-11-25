"""Host-internal local catalog query; caller supplies already authorized scope.

No network, authorization issuance, implicit flush, or publication. A malformed
current source row invalidates the whole result, including an otherwise empty page.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import json
from hashlib import sha256
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from .category_directory import Category, CategoryDirectory


class CategoryCatalogQueryError(ValueError):
    pass


def _deny():
    raise CategoryCatalogQueryError('guanjia_category_catalog_unavailable')


def _text(value):
    if (type(value) is not str or not value or value != value.strip()
            or len(value) > 200 or any(ord(c) < 32 for c in value)):
        _deny()
    return value


@dataclass(frozen=True)
class CategoryProduct:
    product_id: str
    external_code: str
    category_business_id: str
    snapshot_id: str
    snapshot_sha256: str
    product_version: int


@dataclass(frozen=True)
class CategoryCatalogPage:
    category_business_id: str
    descendant_business_ids: tuple[str, ...]
    directory_sha256: str
    directory_observed_at: datetime
    total: int
    limit: int
    offset: int
    items: tuple[CategoryProduct, ...]


@dataclass(frozen=True)
class CategoryStatusProduct(CategoryProduct):
    """Raw isActive meaning from the validated local source snapshot only."""

    upstream_status: Literal['enabled', 'disabled', 'unknown']
    upstream_status_reason: Literal['missing', 'null', 'invalid_type'] | None


@dataclass(frozen=True)
class CategoryUpstreamStatusCounts:
    enabled: int
    disabled: int
    unknown: int


@dataclass(frozen=True)
class CategoryCatalogStatusPage(CategoryCatalogPage):
    # Independent opt-in result: never add fields to the legacy receipt page.
    items: tuple[CategoryStatusProduct, ...]
    candidate_contract: Literal['v4.guanjia-category-status/1']
    authority: Literal['local_source_snapshot_only']
    total_semantics: Literal['local_active_catalog_records']
    upstream_status_counts: CategoryUpstreamStatusCounts


def _upstream_status(raw):
    if 'isActive' not in raw:
        return 'unknown', 'missing'
    value = raw['isActive']
    if value is None:
        return 'unknown', 'null'
    # JSON numbers, strings and truthy containers are not source booleans.
    if type(value) is not bool:
        return 'unknown', 'invalid_type'
    return ('enabled' if value else 'disabled'), None


_LEGACY_PLUS_EIGHT = timezone(timedelta(hours=8))


def _observation_digest_matches(payload, source_version, stored_time, expected):
    # UTCDateTime has already normalized the source instant. Keep the old
    # UTC/null encoding as the fast path, then check one historical spelling.
    if stored_time is not None and (type(stored_time) is not datetime
            or stored_time.utcoffset() != timedelta(0)):
        return False
    observation = {'payload': payload, 'source_version': source_version,
        'source_updated_at': stored_time.isoformat() if stored_time is not None else None}

    def matches():
        return sha256(json.dumps(observation, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest() == expected

    if matches():
        return True
    if stored_time is None:
        return False
    # This candidate is evidence only if it matches the original SHA exactly.
    # Neither directory nor store metadata attests the original source offset.
    try:
        observation['source_updated_at'] = stored_time.astimezone(_LEGACY_PLUS_EIGHT).isoformat()
    except OverflowError:
        return False
    return matches()


def _scope(directory, identity):
    if (type(directory) is not CategoryDirectory or directory.complete is not True
            or type(directory.categories) is not tuple
            or not re.fullmatch('[0-9a-f]{64}', str(directory.content_sha256))
            or type(directory.observed_at) is not datetime
            or directory.observed_at.tzinfo is None
            or directory.observed_at.utcoffset() is None):
        _deny()
    by_id = {}
    for category in directory.categories:
        if type(category) is not Category:
            _deny()
        key = _text(category.business_id)
        if key == '0' or key in by_id:
            _deny()
        _text(category.name)
        _text(category.code)
        if category.parent_business_id is not None:
            _text(category.parent_business_id)
        by_id[key] = category
    if identity not in by_id:
        _deny()
    included = []
    for category in directory.categories:
        chain, seen, current = [], set(), category
        while current is not None:
            if current.business_id in seen:
                _deny()
            seen.add(current.business_id)
            chain.append(current)
            parent = current.parent_business_id
            if parent is not None and parent not in by_id:
                _deny()
            current = by_id[parent] if parent is not None else None
        if category.path != tuple(item.name for item in reversed(chain)):
            _deny()
        if identity in seen:
            included.append(category.business_id)
    return by_id, tuple(sorted(included))


def query_category_catalog(session: Session, directory: CategoryDirectory, *,
                           organization_id: str, store_id: str,
                           category_business_id: str, limit: int = 50,
                           offset: int = 0) -> CategoryCatalogPage:
    """Stable ID ordering over the current transaction; no identity from model input.

    Total counts locally active catalog rows, not upstream-enabled products.
    Preserve this exact result shape for existing persisted category receipts.
    """
    return _query_category_catalog(session, directory, organization_id=organization_id,
        store_id=store_id, category_business_id=category_business_id, limit=limit,
        offset=offset, include_upstream_status=False)


def query_category_catalog_status(session: Session, directory: CategoryDirectory, *,
                                  organization_id: str, store_id: str,
                                  category_business_id: str, limit: int = 50,
                                  offset: int = 0) -> CategoryCatalogStatusPage:
    """Opt-in snapshot status candidate; not a registered tool or live ERP read.

    Do not filter disabled or unknown records. Counts describe all matching
    rows, including those outside this page. Callers must provide an authorized
    scope and a transaction spanning any pages that need one consistent view.
    The legacy query and its receipt serialization remain unchanged.
    """
    return _query_category_catalog(session, directory, organization_id=organization_id,
        store_id=store_id, category_business_id=category_business_id, limit=limit,
        offset=offset, include_upstream_status=True)


def _query_category_catalog(session, directory, *, organization_id, store_id,
                            category_business_id, limit, offset, include_upstream_status):
    for value in (organization_id, store_id, category_business_id):
        _text(value)
    if (not isinstance(session, Session) or type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 1_000_000):
        _deny()
    by_id, descendants = _scope(directory, category_business_id)
    # Scalar rows bypass the ORM identity map: pending edits in the caller's
    # Session cannot masquerade as the current persisted source observation.
    statement = select(
        CatalogProduct.id.label('product_id'),
        CatalogProduct.external_code.label('external_code'),
        CatalogProduct.version.label('product_version'),
        CatalogProduct.source_version.label('product_source_version'),
        CatalogSnapshot.id.label('snapshot_id'),
        CatalogSnapshot.product_id.label('snapshot_product_id'),
        CatalogSnapshot.organization_id.label('snapshot_organization_id'),
        CatalogSnapshot.store_id.label('snapshot_store_id'),
        CatalogSnapshot.source_version.label('snapshot_source_version'),
        CatalogSnapshot.source_updated_at.label('source_updated_at'),
        CatalogSnapshot.payload.label('payload'),
        CatalogSnapshot.payload_sha256.label('payload_sha256'),
    ).outerjoin(
        CatalogSnapshot, CatalogSnapshot.id == CatalogProduct.current_snapshot_id
    ).where(CatalogProduct.organization_id == organization_id,
            CatalogProduct.store_id == store_id,
            CatalogProduct.source_system == 'guanjia', CatalogProduct.active.is_(True)
    ).order_by(CatalogProduct.id)
    matches = []
    status_counts = {'enabled': 0, 'disabled': 0, 'unknown': 0}
    with session.no_autoflush:
        for row in session.execute(statement).mappings():
            if (row['snapshot_id'] is None or row['snapshot_product_id'] != row['product_id']
                    or row['snapshot_organization_id'] != organization_id or row['snapshot_store_id'] != store_id
                    or type(row['payload']) is not dict
                    or type(row['payload'].get('raw')) is not dict):
                _deny()
            category_id = _text(row['payload']['raw'].get('productCategoryId'))
            if category_id not in by_id:
                _deny()
            if not re.fullmatch('[0-9a-f]{64}', row['payload_sha256'] or ''):
                _deny()
            if (row['snapshot_source_version'] != row['product_source_version']
                    or row['payload'].get('external_code') != row['external_code']
                    or row['payload']['raw'].get('code') != row['external_code']):
                _deny()
            try:
                verified = _observation_digest_matches(row['payload'], row['snapshot_source_version'],
                    row['source_updated_at'], row['payload_sha256'])
            except (TypeError, ValueError, RecursionError):
                verified = False
            if not verified:
                _deny()
            if category_id in descendants:
                fields = (row['product_id'], row['external_code'], category_id,
                    row['snapshot_id'], row['payload_sha256'], row['product_version'])
                if include_upstream_status:
                    status, reason = _upstream_status(row['payload']['raw'])
                    status_counts[status] += 1
                    matches.append(CategoryStatusProduct(*fields, status, reason))
                else:
                    matches.append(CategoryProduct(*fields))
    if offset > len(matches):
        _deny()
    fields = (category_business_id, descendants, directory.content_sha256,
        directory.observed_at, len(matches), limit, offset, tuple(matches[offset:offset + limit]))
    if include_upstream_status:
        return CategoryCatalogStatusPage(*fields, 'v4.guanjia-category-status/1',
            'local_source_snapshot_only', 'local_active_catalog_records',
            CategoryUpstreamStatusCounts(**status_counts))
    return CategoryCatalogPage(*fields)
