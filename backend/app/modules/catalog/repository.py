import hashlib

from sqlalchemy import Text, and_, bindparam, cast, func, or_, select
from sqlalchemy.orm import Session

from backend.app.modules.catalog.models import (CatalogConflict, CatalogExtension,
                                                CatalogProduct, CatalogSnapshot)


def find_product(session: Session, organization_id: str, store_id: str, product_id: str
                 ) -> CatalogProduct | None:
    return session.scalar(select(CatalogProduct).where(
        CatalogProduct.id == product_id,
        CatalogProduct.organization_id == organization_id,
        CatalogProduct.store_id == store_id,
    ))


def find_by_external(session: Session, organization_id: str, store_id: str,
                     source_system: str, external_code: str) -> CatalogProduct | None:
    return session.scalar(select(CatalogProduct).where(
        CatalogProduct.organization_id == organization_id,
        CatalogProduct.store_id == store_id,
        CatalogProduct.source_system == source_system,
        CatalogProduct.external_code == external_code,
    ))


def latest_snapshot(session: Session, product: CatalogProduct) -> CatalogSnapshot | None:
    if not product.current_snapshot_id:
        return None
    return session.get(CatalogSnapshot, product.current_snapshot_id)


def _current_snapshot_read_scope(product):
    return (
        CatalogSnapshot.id == product.current_snapshot_id,
        CatalogSnapshot.product_id == product.id,
        CatalogSnapshot.organization_id == product.organization_id,
        CatalogSnapshot.store_id == product.store_id,
    )


def read_latest_snapshot(session: Session, product: CatalogProduct) -> CatalogSnapshot | None:
    """Only project a snapshot belonging to the already scoped catalog product."""
    if not product.current_snapshot_id:
        return None
    return session.scalar(select(CatalogSnapshot).where(*_current_snapshot_read_scope(product)))


def snapshot_by_sha(session: Session, product_id: str, sha: str) -> CatalogSnapshot | None:
    return session.scalar(select(CatalogSnapshot).where(
        CatalogSnapshot.product_id == product_id,
        CatalogSnapshot.payload_sha256 == sha,
    ))


def extension_for(session: Session, product_id: str) -> CatalogExtension | None:
    return session.scalar(select(CatalogExtension).where(
        CatalogExtension.product_id == product_id))


def pending_conflict(session: Session, product_id: str, field_name: str,
                     source_value: dict) -> CatalogConflict | None:
    source = CatalogConflict.source_value
    if session.get_bind(mapper=CatalogConflict).dialect.name == "postgresql":
        # Native json has no equality operator. Retain the existing JSON
        # bind serializer and compare exact text, not normalized JSONB.
        incoming = bindparam("pending_source_value", value=source_value, type_=source.type)
        same_source = cast(source, Text).collate("C") == cast(incoming, Text)
    else:
        same_source = source == source_value
    return session.scalar(select(CatalogConflict).where(
        CatalogConflict.product_id == product_id,
        CatalogConflict.field_name == field_name,
        CatalogConflict.status == "pending",
        same_source,
    ))


def list_products(session: Session, organization_id: str, store_id: str,
                  limit: int) -> list[CatalogProduct]:
    return list(session.scalars(select(CatalogProduct).where(
        CatalogProduct.organization_id == organization_id,
        CatalogProduct.store_id == store_id,
        CatalogProduct.active.is_(True),
    ).order_by(CatalogProduct.external_code).limit(limit)))


def list_conflicts(session: Session, organization_id: str, store_id: str,
                   limit: int) -> list[CatalogConflict]:
    return list(session.scalars(select(CatalogConflict).where(
        CatalogConflict.organization_id == organization_id,
        CatalogConflict.store_id == store_id,
    ).order_by(CatalogConflict.created_at.desc()).limit(limit)))


def catalog_version(session: Session, organization_id: str, store_id: str) -> str:
    rows = session.execute(select(
        CatalogProduct.id, CatalogProduct.version,
        CatalogProduct.current_snapshot_id, CatalogProduct.active,
    ).where(
        CatalogProduct.organization_id == organization_id,
        CatalogProduct.store_id == store_id,
        CatalogProduct.active.is_(True),
    ).order_by(CatalogProduct.id)).all()
    # A page watermark must change on any included row's add/update/remove/
    # snapshot switch, not only when the maximum per-product version changes.
    material = "|".join(
        f"{row.id}:{row.version}:{row.current_snapshot_id or ''}:{int(row.active)}"
        for row in rows
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def cache_page(session: Session, organization_id: str, store_id: str, limit: int,
               after_code: str | None, after_id: str | None,
               since_version: int | None) -> list[tuple[CatalogProduct, CatalogSnapshot | None]]:
    query = select(CatalogProduct, CatalogSnapshot).outerjoin(
        CatalogSnapshot, and_(*_current_snapshot_read_scope(CatalogProduct)),
    ).where(
        CatalogProduct.organization_id == organization_id,
        CatalogProduct.store_id == store_id,
        CatalogProduct.active.is_(True),
    )
    if since_version is not None:
        query = query.where(CatalogProduct.version > since_version)
    if after_code is not None and after_id is not None:
        query = query.where(or_(
            CatalogProduct.external_code > after_code,
            and_(CatalogProduct.external_code == after_code,
                 CatalogProduct.id > after_id),
        ))
    rows = session.execute(query.order_by(
        CatalogProduct.external_code, CatalogProduct.id).limit(limit + 1)).all()
    return [(product, snapshot) for product, snapshot in rows]


def exact_matches(session: Session, organization_id: str, store_id: str,
                  query: str, limit: int):
    """Independent selective index probes, each fetching its snapshot once.

    Keep these probes separate: SQLite's UNION merge/order optimization can
    choose the scope/id index and scan a store instead of the code/hash index.
    A fixed extra round trip is preferable to that data-dependent full scan.
    """
    scope = (CatalogProduct.organization_id == organization_id,
             CatalogProduct.store_id == store_id, CatalogProduct.active.is_(True))
    statement = select(CatalogProduct, CatalogSnapshot).join(CatalogSnapshot, and_(
        CatalogSnapshot.id == CatalogProduct.current_snapshot_id,
        CatalogSnapshot.product_id == CatalogProduct.id,
        CatalogSnapshot.organization_id == CatalogProduct.organization_id,
        CatalogSnapshot.store_id == CatalogProduct.store_id))
    code_rows = session.execute(statement.where(*scope, CatalogProduct.external_code == query).limit(limit))
    result = list(code_rows)
    barcode_rows = session.execute(statement.where(*scope,
        CatalogProduct.barcode_sha256 == hashlib.sha256(query.encode()).hexdigest()).limit(limit))
    result.extend((product, snapshot) for product, snapshot in barcode_rows
                  if (snapshot.payload or {}).get("barCode") == query)
    seen = set()
    unique = []
    for product, snapshot in result:
        if product.id not in seen:
            seen.add(product.id)
            unique.append((product, snapshot))
    return unique


def exact_products(session: Session, organization_id: str, store_id: str,
                   query: str, limit: int) -> list[CatalogProduct]:
    return [product for product, _ in exact_matches(session, organization_id, store_id, query, limit)]


def snapshots_by_product_ids(session: Session, product_ids: list[str]
                             ) -> dict[str, CatalogSnapshot]:
    if not product_ids:
        return {}
    rows = session.scalars(select(CatalogSnapshot).where(
        CatalogSnapshot.id.in_({
            product.current_snapshot_id for product in session.scalars(
                select(CatalogProduct).where(CatalogProduct.id.in_(product_ids)))
            if product.current_snapshot_id
        })
    ))
    return {snapshot.id: snapshot for snapshot in rows}


def products_with_snapshots(session: Session, organization_id: str, store_id: str,
                            limit: int, product_ids=None):
    """Bounded snapshot batch; exact scope and active state remain authoritative."""
    query = select(CatalogProduct, CatalogSnapshot).outerjoin(CatalogSnapshot, and_(
        CatalogSnapshot.id == CatalogProduct.current_snapshot_id,
        CatalogSnapshot.product_id == CatalogProduct.id,
        CatalogSnapshot.organization_id == CatalogProduct.organization_id,
        CatalogSnapshot.store_id == CatalogProduct.store_id,
    )).where(CatalogProduct.organization_id == organization_id,
             CatalogProduct.store_id == store_id, CatalogProduct.active.is_(True))
    if product_ids is not None:
        query = query.where(CatalogProduct.id.in_(product_ids))
    if product_ids is None:
        query = query.order_by(CatalogProduct.external_code, CatalogProduct.id)
    # Sorting a small fetched batch in memory avoids the planner scanning the
    # entire scope's code index merely to satisfy ORDER BY for a handful of IDs.
    rows = [(product, snapshot) for product, snapshot in session.execute(query.limit(limit))
            if snapshot is not None]
    return sorted(rows, key=lambda pair: (pair[0].external_code, pair[0].id))


def search_values(session: Session, organization_id: str, store_id: str,
                  limit: int, field: str):
    """Read a small JSON scalar, never hydrate full source rows for a miss.

    JSON typed extraction preserves original scalar/object values. No trimming,
    rewriting, SQL UPDATE, or persisted derived data occurs on this read path.
    """
    fields = [CatalogSnapshot.payload[field]]
    if field == "name_and_pinyin":
        fields = [CatalogSnapshot.payload["name"], func.coalesce(
            CatalogSnapshot.payload["pyCode"].as_string(),
            CatalogSnapshot.payload["raw"]["pyCode"].as_string(),
            CatalogSnapshot.payload["raw"]["raw"]["pyCode"].as_string())]
    statement = select(CatalogProduct.id, CatalogSnapshot.id, *fields).outerjoin(
        CatalogSnapshot, and_(CatalogSnapshot.id == CatalogProduct.current_snapshot_id,
            CatalogSnapshot.product_id == CatalogProduct.id,
            CatalogSnapshot.organization_id == CatalogProduct.organization_id,
            CatalogSnapshot.store_id == CatalogProduct.store_id)).where(
        CatalogProduct.organization_id == organization_id, CatalogProduct.store_id == store_id,
        CatalogProduct.active.is_(True)).order_by(
        CatalogProduct.external_code, CatalogProduct.id).limit(limit)
    result = []
    for identity, snapshot_id, *values in session.execute(statement):
        if snapshot_id is not None:
            value = {"name": values[0], "pyCode": values[1]} if field == "name_and_pinyin" else values[0]
            result.append((identity, value))
    return result
