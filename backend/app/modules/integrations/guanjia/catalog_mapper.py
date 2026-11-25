"""CAT-003 source mapper: preserve original fields; never derive a barcode."""

from __future__ import annotations

from typing import Any


class MappingError(ValueError):
    pass


def source_barcode(item: dict[str, Any], standard: dict[str, Any]) -> Any:
    """Prefer the original main barcode; empty main may use the actual standard.

    Never derive from code or normalize the selected upstream value.
    """
    value = item.get("barCode")
    if value is None or (isinstance(value, str) and not value.strip()):
        return standard.get("barCode")
    return value


def source_active(item: dict[str, Any]) -> bool | None:
    """Explicit upstream state only; missing is not permission to reactivate."""
    if "isActive" not in item:
        item = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    if "isActive" not in item:
        return None
    value = item["isActive"]
    if type(value) is not bool:
        raise MappingError("isActive must be a boolean when supplied")
    return value


def map_product_item(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise MappingError("product item must be an object")
    code = item.get("code")
    if not isinstance(code, str) or not code.strip():
        raise MappingError("product code is required")
    source_active(item)  # reject invalid source state before any page writes
    standard = (item.get("baseProductStandardList") or [{}])[0] or {}
    return {
        "code": code,
        "barCode": source_barcode(item, standard),
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
