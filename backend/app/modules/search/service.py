"""Deterministic lookup: exact source values win; normalization only proposes."""

from __future__ import annotations

from typing import Any
import re

from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from backend.app.modules.catalog import repository
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.search import projection
from backend.app.modules.search.models import ProductAlias

MATCH_FIELDS = ("code", "barcode")
MAX_SCAN = 10_000
MAX_EXACT = 1_000


def _strip_outer_quotes(value: str) -> str:
    return projection.strip_outer_quotes(value)


def normalize_barcode_candidate(value: Any) -> str | None:
    """Only trim outer whitespace/quotes. Never expand scientific notation."""
    return projection.normalize_barcode(value)


def _match(product: CatalogProduct, snapshot: CatalogSnapshot, *,
           field: str, match_type: str, original: str,
           normalized: str | None = None) -> dict[str, Any]:
    payload = snapshot.payload or {}
    return {
        "product_id": product.id,
        "external_code": product.external_code,
        "match_field": field,
        "match_type": match_type,
        "original_value": original,
        "normalized_value": normalized,
        "name": payload.get("name"),
        "spec": payload.get("spec"),
        "unit": payload.get("unit"),
        "barcode": payload.get("barCode"),
        "source_system": product.source_system,
        "source_version": product.source_version,
        "source_updated_at": product.source_updated_at,
        "captured_at": snapshot.captured_at,
    }


def _scan_truncated(session: Session, organization_id: str, store_id: str) -> bool:
    # An existence probe stops at the candidate boundary; do not COUNT the
    # whole store on every high-frequency exact search.
    return session.scalar(select(literal(1)).select_from(CatalogProduct).where(
        CatalogProduct.organization_id == organization_id,
        CatalogProduct.store_id == store_id,
        CatalogProduct.active.is_(True),
    ).offset(MAX_SCAN).limit(1)) is not None


def _bounded(items: list[dict[str, Any]], limit: int, ambiguous: bool
             ) -> tuple[list[dict[str, Any]], bool]:
    if ambiguous:
        return items, False  # never drop collision candidates
    return items[:limit], len(items) > limit


def _search_products(session: Session, *, organization_id: str, store_id: str,
                    query: str, limit: int = 20) -> dict[str, Any]:
    raw_query = query or ""
    text = raw_query.strip()
    base = {"query": raw_query, "exact": [], "candidates": [],
            "ambiguous": False, "message": "", "exact_total": 0,
            "candidate_total": 0, "has_more": False,
            "scan_truncated": False,
            "truncated": False}
    if not text:
        base["message"] = "请输入商品编号或条码。"
        return base

    def finish_exact(items: list[dict[str, Any]], message: str,
                     truncated: bool = False) -> dict[str, Any]:
        products = {item["product_id"] for item in items}
        base["ambiguous"] = len(products) > 1
        base["truncated"] = truncated
        base["exact_total"] = len(items)
        if truncated:
            items = items[:MAX_EXACT]
            base["exact"] = items
            base["has_more"] = True
            if base["ambiguous"]:
                base["message"] = ("命中多个商品且超过安全上限，仅返回前 "
                                   f"{MAX_EXACT} 条，请缩小查询范围。")
            else:
                base["message"] = (f"命中数超过安全上限，仅返回前 {MAX_EXACT} 条，"
                                   "请缩小查询范围。")
        else:
            base["exact"], base["has_more"] = _bounded(items, limit, base["ambiguous"])
            base["message"] = message
            if base["ambiguous"]:
                base["message"] = message.replace("。", "，命中多个商品请人工确认；已返回全部候选。")
        # exact SQL is not affected by the candidate scan cap; do not append
        # a "possibly incomplete" warning to an exact hit.
        return base

    exact_rows = repository.exact_matches(
        session, organization_id, store_id, raw_query, MAX_EXACT + 1)
    truncated = len(exact_rows) > MAX_EXACT
    source_exact: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for product, snapshot in exact_rows:
        if product.external_code == raw_query and (product.id, "code") not in seen:
            source_exact.append(_match(product, snapshot, field="code",
                                       match_type="exact_code", original=raw_query))
            seen.add((product.id, "code"))
        if (snapshot.payload or {}).get("barCode") == raw_query \
                and (product.id, "barcode") not in seen:
            source_exact.append(_match(product, snapshot, field="barcode",
                                       match_type="exact_barcode", original=raw_query))
            seen.add((product.id, "barcode"))
    if source_exact:
        return finish_exact(source_exact, "精确命中。", truncated=truncated)

    # Only when the source itself has no exact hit may a manual alias be used.
    alias_exact: list[dict[str, Any]] = []
    alias_ids = select(ProductAlias.product_id).where(
        ProductAlias.organization_id == organization_id,
        ProductAlias.store_id == store_id,
        ProductAlias.alias_value == raw_query,
        ProductAlias.status == "active",
    )
    for product, snapshot in repository.products_with_snapshots(
            session, organization_id, store_id, MAX_EXACT + 1, alias_ids):
        alias_exact.append(_match(product, snapshot, field="alias",
                                  match_type="alias_exact", original=raw_query))
        seen.add((product.id, "alias"))
    if alias_exact:
        return finish_exact(alias_exact, "人工副码精确命中。", truncated=len(alias_exact) > MAX_EXACT)

    normalized_query = _strip_outer_quotes(raw_query)
    candidates: list[dict[str, Any]] = []
    seen_products: set[str] = set()
    fresh_projection = not projection.is_stale(session, organization_id, store_id)
    if not fresh_projection:
        base["scan_truncated"] = _scan_truncated(session, organization_id, store_id)
    if fresh_projection:
        normalized_ids = projection.indexed_ids(session, organization_id, store_id,
            query=normalized_query, normalized=True, limit=MAX_EXACT+1)
    else:
        normalized_ids = [identity for identity, barcode in repository.search_values(
            session, organization_id, store_id, MAX_SCAN, "barCode")
            if normalize_barcode_candidate(barcode) is not None
            and normalize_barcode_candidate(barcode) == normalized_query]
    for offset in range(0, len(normalized_ids), 500):
        for product, snapshot in repository.products_with_snapshots(
                session, organization_id, store_id, 500, normalized_ids[offset:offset+500]):
            if product.id in seen_products:
                continue
            barcode = (snapshot.payload or {}).get("barCode")
            normalized_barcode = normalize_barcode_candidate(barcode)
            if normalized_barcode != normalized_query:
                continue
            candidates.append(_match(product, snapshot, field="barcode",
                                     match_type="normalized_barcode", original=barcode,
                                     normalized=normalized_barcode))
            seen_products.add(product.id)
    if candidates:
        base["candidate_total"] = len(candidates)
        base["ambiguous"] = len(seen_products) > 1
        base["candidates"], base["has_more"] = _bounded(
            candidates, limit, base["ambiguous"])
        if base["ambiguous"]:
            base["message"] = "仅归一化候选，命中多个商品需人工确认；已返回全部候选。"
        else:
            base["message"] = "归一化候选，原值未被修改。"
        if base["scan_truncated"]:
            base["message"] += " 商品数超过当前扫描上限，结果可能不完整。"
        if fresh_projection and len(candidates) > MAX_EXACT:
            base["candidates"] = candidates[:MAX_EXACT]
            base["truncated"] = base["has_more"] = True
            base["message"] = "低置信度候选超过安全上限，仅返回部分结果，请缩小查询范围并人工确认。"
        return base
    # Low-confidence fuzzy/pinyin candidates come last; they never claim exactness.
    fuzzy: list[dict[str, Any]] = []
    seen_fuzzy: set[str] = set()
    needle = text.casefold()
    if fresh_projection:
        matched_ids = projection.indexed_ids(session, organization_id, store_id,
            query=needle, limit=MAX_EXACT+1)
    else:
        matched_ids = []
        for row in projection._ephemeral_rows(session, organization_id, store_id):
            haystack = "\n".join((row.name_text, row.alias_text, row.pinyin_text)).casefold()
            if needle and needle in haystack:
                matched_ids.append(row.product_id)
    numeric=bool(re.search(r'\d',needle))
    numeric_aliases=set()
    if numeric and matched_ids:
        from backend.app.modules.search.source_clues import comparison_text,contains_term,has_source_term
        for offset in range(0,len(matched_ids),400):
            for identity,value in session.execute(select(ProductAlias.product_id,ProductAlias.alias_value).where(
                    ProductAlias.organization_id==organization_id,ProductAlias.store_id==store_id,
                    ProductAlias.status=='active',ProductAlias.product_id.in_(matched_ids[offset:offset+400]))):
                if contains_term(needle,value.casefold()) or contains_term(comparison_text(text),comparison_text(value)):
                    numeric_aliases.add(identity)
    raw_overflow=fresh_projection and len(matched_ids)>MAX_EXACT
    # Chunk IN values for SQLite parameter limits; query count remains bounded
    # by the explicit projection cap, never one statement per matched product.
    for offset in range(0, len(matched_ids), 500):
        for product, snapshot in repository.products_with_snapshots(
                session, organization_id, store_id, 500, matched_ids[offset:offset+500]):
            if product.id in seen_fuzzy:
                continue
            if numeric and product.id not in numeric_aliases and not has_source_term(snapshot.payload,raw_query):
                continue
            fuzzy.append(_match(product, snapshot, field="name",
                                match_type="fuzzy_candidate", original=raw_query))
            seen_fuzzy.add(product.id)
    if fuzzy:
        base["candidate_total"] = len(fuzzy)
        base["ambiguous"] = len(seen_fuzzy) > 1
        base["candidates"], base["has_more"] = _bounded(fuzzy, limit, base["ambiguous"])
        base["message"] = ("模糊/拼音候选，低置信度需人工确认；已返回全部候选。"
                           if base["ambiguous"] else "模糊/拼音候选，低置信度需人工确认。")
        if base["scan_truncated"]:
            base["message"] += " 商品数超过当前扫描上限，结果可能不完整。"
        if raw_overflow:
            base["candidates"] = fuzzy[:MAX_EXACT]
            base["truncated"] = base["has_more"] = True
            base["message"] = "低置信度候选超过安全上限，仅返回部分结果，请缩小查询范围并人工确认。"
        return base
    if raw_overflow:
        base['truncated']=base['has_more']=True
    base["message"] = "未找到精确匹配，未猜测商品或条码。"
    if raw_overflow:
        base['message']+=' 原始候选已触及上限，请缩小查询范围。'
    if base["scan_truncated"]:
        base["message"] += " 商品数超过当前扫描上限，结果可能不完整。"
    return base


def search_products(session: Session, *, organization_id: str, store_id: str,
                    query: str, limit: int = 20) -> dict[str, Any]:
    import os
    result = _search_products(session,organization_id=organization_id,store_id=store_id,query=query,limit=limit)
    index_path = os.environ.get("SEARCH_SOURCE_CLUES_PATH", "")
    if index_path and isinstance(query,str) and query.strip():
        from backend.app.modules.search.source_clues import augment_search
        result = augment_search(session,organization_id=organization_id,store_id=store_id,
            query=query,result=result,limit=limit,index_path=index_path)
    return result
