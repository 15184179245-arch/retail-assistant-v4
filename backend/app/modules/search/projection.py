"""Rebuildable derived search rows; source snapshots/aliases remain truth."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from hashlib import sha256

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.orm import Session

from backend.app.modules.catalog import repository
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.search.models import ProductAlias, SearchProjection

MAX_PROJECTION = 100_000


def strip_outer_quotes(value):
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value


def normalize_barcode(value):
    return (strip_outer_quotes(value) or None) if isinstance(value, str) else None


def _source_pinyin(payload):
    for candidate in (payload, payload.get("raw") or {}, (payload.get("raw") or {}).get("raw") or {}):
        if isinstance(candidate, dict) and isinstance(candidate.get("pyCode"), str):
            return candidate["pyCode"]
    return ""



def rebuild(session: Session, *, organization_id: str, store_id: str,
            now: datetime | None = None) -> int:
    timestamp = now or datetime.now(timezone.utc)
    session.execute(delete(SearchProjection).where(
        SearchProjection.organization_id == organization_id,
        SearchProjection.store_id == store_id,
    ))
    aliases = list(session.scalars(select(ProductAlias).where(
        ProductAlias.organization_id == organization_id,
        ProductAlias.store_id == store_id,
        ProductAlias.status == "active",
    )))
    by_product: dict[str, list[ProductAlias]] = {}
    for alias in aliases:
        by_product.setdefault(alias.product_id, []).append(alias)
    built = 0
    for product, snapshot in repository.products_with_snapshots(session, organization_id, store_id, MAX_PROJECTION):
        payload = snapshot.payload or {}
        bound = by_product.get(product.id, [])
        alias_values = [item.alias_value for item in bound]
        pinyin_values = [item.alias_value for item in bound if item.alias_kind == "pinyin"]
        pinyin_text = "\n".join(value for value in [_source_pinyin(payload), *pinyin_values] if value)
        alias_text = "\n".join(alias_values)
        name_text = str(payload.get("name") or "")
        normalized = normalize_barcode(payload.get("barCode"))
        session.add(SearchProjection(
            product_id=product.id, organization_id=organization_id, store_id=store_id,
            code=product.external_code,
            barcode=payload.get("barCode") if isinstance(payload.get("barCode"), str) else None,
            snapshot_id=snapshot.id,
            name_text=name_text, alias_text=alias_text, pinyin_text=pinyin_text,
            search_text="\n".join((name_text, alias_text, pinyin_text)).casefold(),
            normalized_barcode_sha256=sha256(normalized.encode()).hexdigest() if normalized else None,
            updated_at=timestamp,
        ))
        built += 1
    session.flush()
    return built


def rows(session: Session, organization_id: str, store_id: str) -> list[SearchProjection]:
    return list(session.scalars(select(SearchProjection).where(
        SearchProjection.organization_id == organization_id,
        SearchProjection.store_id == store_id,
    )))


def _ephemeral_rows(session: Session, organization_id: str, store_id: str
                    ) -> list[SimpleNamespace]:
    aliases = list(session.scalars(select(ProductAlias).where(
        ProductAlias.organization_id == organization_id,
        ProductAlias.store_id == store_id,
        ProductAlias.status == "active",
    )))
    by_product: dict[str, list[ProductAlias]] = {}
    for alias in aliases:
        by_product.setdefault(alias.product_id, []).append(alias)
    result: list[SimpleNamespace] = []
    for identity, values in repository.search_values(
            session, organization_id, store_id, MAX_PROJECTION, "name_and_pinyin"):
        bound = by_product.get(identity, [])
        result.append(SimpleNamespace(
            product_id=identity,
            name_text=str(values["name"] or ""),
            alias_text="\n".join(item.alias_value for item in bound),
            pinyin_text="\n".join(value for value in [str(values["pyCode"] or ""),
                *(item.alias_value for item in bound if item.alias_kind == "pinyin")] if value),
        ))
    return result


def is_stale(session: Session, organization_id: str, store_id: str) -> bool:
    # A SQL existence probe returns one scalar, never hydrates the catalog.
    return session.scalar(select(CatalogProduct.id).outerjoin(SearchProjection, and_(
        SearchProjection.product_id == CatalogProduct.id,
        SearchProjection.organization_id == CatalogProduct.organization_id,
        SearchProjection.store_id == CatalogProduct.store_id)).where(
        CatalogProduct.organization_id == organization_id, CatalogProduct.store_id == store_id,
        CatalogProduct.active.is_(True), CatalogProduct.current_snapshot_id.is_not(None),
        or_(SearchProjection.product_id.is_(None), SearchProjection.snapshot_id.is_(None),
            SearchProjection.search_text == "",
            SearchProjection.snapshot_id != CatalogProduct.current_snapshot_id)).limit(1)) is not None


def search_rows(session: Session, organization_id: str, store_id: str):
    """Read-only rows for GET: never DML. Stale/missing projection falls back
    to a pure snapshot+alias computation; a separate explicit rebuild persists."""
    if is_stale(session, organization_id, store_id):
        return _ephemeral_rows(session, organization_id, store_id)
    return rows(session, organization_id, store_id)


def indexed_ids(session, organization_id, store_id, *, query, normalized=False, limit=1001):
    """Use only a fresh, independently rebuildable projection. GET never writes."""
    scope = (SearchProjection.organization_id == organization_id,
             SearchProjection.store_id == store_id,
             CatalogProduct.organization_id == organization_id, CatalogProduct.store_id == store_id,
             CatalogProduct.active.is_(True),
             CatalogProduct.current_snapshot_id == SearchProjection.snapshot_id)
    statement = select(SearchProjection.product_id).join(CatalogProduct,
        CatalogProduct.id == SearchProjection.product_id).where(*scope)
    if normalized:
        statement = statement.where(SearchProjection.normalized_barcode_sha256 == sha256(query.encode()).hexdigest())
    elif session.get_bind().dialect.name == "sqlite" and len(query) >= 3 and "\x00" not in query and session.scalar(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='search_projection_fts'")):
        # Literal phrase quoting blocks FTS operator injection; instr verifies
        # the exact casefold substring after the index narrows the candidates.
        phrase = '"' + query.replace('"', '""') + '"'
        return list(session.scalars(text("""SELECT p.product_id FROM search_projection_fts
            JOIN search_projections p ON p.rowid=search_projection_fts.rowid
            JOIN catalog_products c ON c.id=p.product_id
            WHERE search_projection_fts MATCH :phrase AND p.organization_id=:org AND p.store_id=:store
              AND c.organization_id=:org AND c.store_id=:store AND c.active=1
              AND c.current_snapshot_id=p.snapshot_id AND instr(p.search_text,:needle)>0
            ORDER BY c.external_code,c.id LIMIT :limit"""),
            {"phrase": phrase, "needle": query, "org": organization_id, "store": store_id, "limit": limit}))
    else:
        # One/two-character searches scan only the slim derived text; SQL
        # parameter escaping preserves literal percent/underscore characters.
        statement = statement.where(SearchProjection.search_text.contains(query, autoescape=True))
    return list(session.scalars(statement.order_by(CatalogProduct.external_code, CatalogProduct.id).limit(limit)))
