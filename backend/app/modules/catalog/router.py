import base64
import json
import re
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.modules.identity.service import authorize_scope
from backend.app.core.security import AuthenticatedSubject, require_authenticated_subject
from backend.app.db.session import get_session
from backend.app.modules.catalog import repository, service
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.catalog.schemas import (CatalogCacheEnvelope,
                                                 CatalogCacheItem,
                                                 CatalogCachePage,
                                                 CatalogConflictListEnvelope,
                                                 CatalogConflictOut,
                                                 CatalogProductEnvelope,
                                                 CatalogProductListEnvelope,
                                                 CatalogProductOut,
                                                 CatalogSnapshotOut)

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _snapshot_out(snapshot: CatalogSnapshot | None) -> CatalogSnapshotOut | None:
    return CatalogSnapshotOut.model_validate(snapshot) if snapshot is not None else None


def _product_out(product: CatalogProduct, snapshot: CatalogSnapshot | None) -> CatalogProductOut:
    return CatalogProductOut(
        id=product.id, organization_id=product.organization_id, store_id=product.store_id,
        source_system=product.source_system, external_code=product.external_code,
        source_version=product.source_version, source_updated_at=product.source_updated_at,
        active=product.active, version=product.version, snapshot=_snapshot_out(snapshot),
    )


@router.get("", response_model=CatalogProductListEnvelope)
def list_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=50, ge=1, le=100),
) -> CatalogProductListEnvelope:
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="catalog.read")
    pairs = service.list_products(session, organization_id=actor.organization_id,
                                  store_id=actor.store_id, limit=limit)
    return CatalogProductListEnvelope(
        success=True,
        data={"items": [_product_out(product, snapshot).model_dump(mode="json")
                        for product, snapshot in pairs]},
        code="OK", error=None, user_message="商品已读取。", hint=None,
    )


CACHE_FIELDS = ("name", "spec", "model", "unit", "category", "brand",
                "barCode", "barcode_flags", "sell_price")


def _encode_cursor(version: str, code: str, product_id: str) -> str:
    raw = json.dumps({"v": version, "k": code, "i": product_id},
                     ensure_ascii=False, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        value = json.loads(raw)
        version, code, product_id = value["v"], value["k"], value["i"]
        if (not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version)
                or not isinstance(code, str) or not isinstance(product_id, str)):
            raise ValueError
        return version, code, product_id
    except Exception as error:
        raise HTTPException(400, "catalog_cache_cursor_invalid") from error


def _cache_item(product: CatalogProduct, snapshot: CatalogSnapshot | None) -> CatalogCacheItem:
    payload = (snapshot.payload if snapshot is not None else {}) or {}
    return CatalogCacheItem(
        product_id=product.id, external_code=product.external_code,
        source_system=product.source_system, source_version=product.source_version,
        source_updated_at=product.source_updated_at, active=product.active,
        version=product.version, name=payload.get("name"), spec=payload.get("spec"),
        model=payload.get("model"), unit=payload.get("unit"),
        category=payload.get("category"), brand=payload.get("brand"),
        barcode=payload.get("barCode"), barcode_flags=list(payload.get("barcode_flags") or []),
        sell_price=payload.get("sell_price"),
        price_source_version=(snapshot.source_version if snapshot is not None else product.source_version),
        price_captured_at=(snapshot.captured_at if snapshot is not None else product.updated_at),
        snapshot_sha256=(snapshot.payload_sha256 if snapshot is not None else ""),
    )


@router.get("/cache", response_model=CatalogCacheEnvelope)
def cache_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    since_version: int | None = Query(default=None, ge=0),
    known_version: str | None = Query(default=None),
) -> CatalogCacheEnvelope:
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="catalog.read")
    watermark = repository.catalog_version(session, actor.organization_id, actor.store_id)
    if known_version is not None and known_version == watermark and cursor is None:
        page = CatalogCachePage(items=[], next_cursor=None, has_more=False,
                                catalog_version=watermark, generated_at=datetime.now(timezone.utc),
                                changed=False)
        return CatalogCacheEnvelope(success=True, data=page, code="OK", error=None,
                                    user_message="商品缓存未变化。", hint=None)
    after_code = after_id = None
    if cursor is not None:
        cursor_version, after_code, after_id = _decode_cursor(cursor)
        if cursor_version != watermark:
            page = CatalogCachePage(items=[], next_cursor=None, has_more=False,
                                    catalog_version=watermark, generated_at=datetime.now(timezone.utc),
                                    changed=True, reset_required=True)
            return CatalogCacheEnvelope(success=True, data=page, code="OK", error=None,
                                        user_message="商品版本已变化，请重新拉取全量。", hint=None)
    rows = repository.cache_page(session, actor.organization_id, actor.store_id, limit,
                                 after_code, after_id, since_version)
    has_more = len(rows) > limit
    rows = rows[:limit]
    after_watermark = repository.catalog_version(session, actor.organization_id, actor.store_id)
    if after_watermark != watermark:
        page = CatalogCachePage(items=[], next_cursor=None, has_more=False,
                                catalog_version=after_watermark,
                                generated_at=datetime.now(timezone.utc),
                                changed=True, reset_required=True)
    else:
        items = [_cache_item(product, snapshot) for product, snapshot in rows]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(watermark, last.external_code, last.product_id)
        page = CatalogCachePage(items=items, next_cursor=next_cursor, has_more=has_more,
                                catalog_version=watermark, generated_at=datetime.now(timezone.utc))
    return CatalogCacheEnvelope(success=True, data=page, code="OK", error=None,
                                user_message="商品缓存已读取。", hint=None)


@router.get("/conflicts", response_model=CatalogConflictListEnvelope)
def conflicts_route(
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
    limit: int = Query(default=50, ge=1, le=100),
) -> CatalogConflictListEnvelope:
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="catalog.read")
    conflicts = service.list_conflicts(session, organization_id=actor.organization_id,
                                       store_id=actor.store_id, limit=limit)
    return CatalogConflictListEnvelope(
        success=True,
        data={"items": [CatalogConflictOut.model_validate(item).model_dump(mode="json")
                        for item in conflicts]},
        code="OK", error=None, user_message="商品冲突已读取。", hint=None,
    )


@router.get("/{product_id}", response_model=CatalogProductEnvelope)
def detail_route(
    product_id: str,
    actor: Annotated[AuthenticatedSubject, Depends(require_authenticated_subject)],
    session: Annotated[Session, Depends(get_session)],
) -> CatalogProductEnvelope:
    authorize_scope(session, actor, organization_id=actor.organization_id,
                    store_id=actor.store_id, permission="catalog.read")
    product, snapshot = service.get_product(
        session, organization_id=actor.organization_id,
        store_id=actor.store_id, product_id=product_id)
    return CatalogProductEnvelope(
        success=True, data=_product_out(product, snapshot), code="OK", error=None,
        user_message="商品详情已读取。", hint=None,
    )
