"""CAT-003 source mapper: preserve original fields; never derive a barcode."""

from __future__ import annotations

from typing import Any


class MappingError(ValueError):
    pass


def map_product_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise MappingError("product item must be an object")
    code = item.get("code")
    if not isinstance(code, str) or not code.strip():
        raise MappingError("product code is required")
    standard = (item.get("baseProductStandardList") or [{}])[0] or {}
    return {
        "code": code,
        "barCode": item.get("barCode"),
        "name": item.get("name"),
        "spec": item.get("spec"),
        "model": item.get("model"),
        "unit": standard.get("standardName"),
        "category": item.get("productCategoryIdName"),
        "brand": item.get("brandName"),
        "cost_price": standard.get("buyingPrice"),
        "sell_price": standard.get("retailPrice"),
        "raw": item,
    }


def map_page(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [map_product_item(item) for item in items]
