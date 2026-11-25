"""SRCH-002: manual aliases with audit and rebuildable projection."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog import repository
from backend.app.modules.search.models import ProductAlias
from backend.app.modules.search import projection

ALIAS_KINDS = {"code", "name", "pinyin"}


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise HTTPException(400, "timezone_required")
    return current.astimezone(timezone.utc)


def bind_alias(session: Session, *, organization_id: str, store_id: str,
               product_id: str, alias_value: str, alias_kind: str,
               actor_id: str, idempotency_key: str,
               now: datetime | None = None) -> ProductAlias:
    product = repository.find_product(session, organization_id, store_id, product_id)
    if product is None:
        raise HTTPException(404, "catalog_product_not_found")
    text = (alias_value or "").strip()
    if not text or len(text) > 120:
        raise HTTPException(400, "alias_value_invalid")
    if alias_kind not in ALIAS_KINDS:
        raise HTTPException(400, "alias_kind_invalid")
    timestamp = _now(now)
    existing = session.scalar(select(ProductAlias).where(
        ProductAlias.organization_id == organization_id,
        ProductAlias.store_id == store_id,
        ProductAlias.product_id == product.id,
        ProductAlias.alias_kind == alias_kind,
        ProductAlias.alias_value == text,
    ))
    if existing is not None:
        if existing.status == "active":
            return existing
        raise HTTPException(409, "alias_revoked_rebind_forbidden")
    alias = ProductAlias(
        id=str(uuid4()), organization_id=organization_id, store_id=store_id,
        product_id=product.id, alias_value=text, alias_kind=alias_kind,
        status="active", created_by=actor_id, created_at=timestamp,
    )
    session.add(alias)
    session.flush()
    record_event(
        session, actor_id=actor_id, organization_id=organization_id, store_id=store_id,
        action="catalog.alias.bind", target_type="product_alias", target_id=alias.id,
        idempotency_key=idempotency_key,
        details={"product_id": product.id, "alias_kind": alias_kind, "alias_value": text},
        occurred_at=timestamp,
    )
    projection.rebuild(session, organization_id=organization_id, store_id=store_id,
                      now=timestamp)
    return alias


def revoke_alias(session: Session, *, organization_id: str, store_id: str,
                 alias_id: str, actor_id: str, idempotency_key: str,
                 now: datetime | None = None) -> ProductAlias:
    alias = session.scalar(select(ProductAlias).where(
        ProductAlias.id == alias_id,
        ProductAlias.organization_id == organization_id,
        ProductAlias.store_id == store_id,
    ))
    if alias is None:
        raise HTTPException(404, "alias_not_found")
    if alias.status == "revoked":
        return alias
    timestamp = _now(now)
    alias.status = "revoked"
    alias.revoked_by = actor_id
    alias.revoked_at = timestamp
    session.flush()
    record_event(
        session, actor_id=actor_id, organization_id=organization_id, store_id=store_id,
        action="catalog.alias.revoke", target_type="product_alias", target_id=alias.id,
        idempotency_key=idempotency_key,
        details={"product_id": alias.product_id, "alias_kind": alias.alias_kind,
                 "alias_value": alias.alias_value},
        occurred_at=timestamp,
    )
    projection.rebuild(session, organization_id=organization_id, store_id=store_id,
                      now=timestamp)
    return alias


def list_aliases(session: Session, *, organization_id: str, store_id: str,
                 product_id: str | None = None) -> list[ProductAlias]:
    statement = select(ProductAlias).where(
        ProductAlias.organization_id == organization_id,
        ProductAlias.store_id == store_id,
        ProductAlias.status == "active",
    )
    if product_id is not None:
        statement = statement.where(ProductAlias.product_id == product_id)
    return list(session.scalars(statement.order_by(ProductAlias.created_at)))
