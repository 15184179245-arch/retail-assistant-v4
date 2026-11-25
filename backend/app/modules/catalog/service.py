"""CAT-002: preserve source truth, keep local extensions independent."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.modules.catalog import repository
from backend.app.modules.catalog.models import (CatalogConflict, CatalogExtension,
                                                CatalogProduct, CatalogSnapshot)

SCIENTIFIC = re.compile(r"^\d+(?:\.\d+)?[eE][+-]?\d+$")
SOURCE_FIELDS = ("code", "barCode", "name", "spec", "model", "unit",
                 "category", "brand", "cost_price", "sell_price")


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise HTTPException(400, "timezone_required")
    return current.astimezone(timezone.utc)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def classify_barcode(value: Any) -> tuple[str | None, tuple[str, ...]]:
    """Preserve the original string; only classify, never rewrite it."""
    text = _as_text(value)
    if text == "":
        return None, ("NO_BARCODE",)
    if SCIENTIFIC.match(text):
        return text, ("DIRTY_SCIENTIFIC",)
    if text.lstrip("0") == "":
        return text, ("DIRTY_ZERO",)
    if not text.isdigit():
        return text, ("DIRTY_NON_NUMERIC",)
    if len(text) < 6:
        return text, ("DIRTY_LEN_SHORT",)
    if len(text) > 18:
        return text, ("DIRTY_LEN_LONG",)
    return text, ()


def _price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure mapping: no barcode derivation, no strip of dirty values, 0 != None."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    external_code = _as_text(payload.get("code"))
    if not external_code.strip():
        raise ValueError("code is required")
    standard = (payload.get("baseProductStandardList") or [{}])[0] or {}
    barcode, flags = classify_barcode(payload.get("barCode"))
    return {
        "external_code": external_code,
        "barCode": barcode,
        "barcode_flags": list(flags),
        "name": payload.get("name"),
        "spec": payload.get("spec"),
        "model": payload.get("model"),
        "unit": standard.get("standardName") or payload.get("unit"),
        "category": payload.get("productCategoryIdName") or payload.get("category"),
        "brand": payload.get("brandName") or payload.get("brand"),
        "cost_price": _price(standard["buyingPrice"] if "buyingPrice" in standard
                             else payload.get("cost_price")),
        "sell_price": _price(standard["retailPrice"] if "retailPrice" in standard
                             else payload.get("sell_price")),
        "raw": payload,
    }


def _detect_conflicts(session: Session, *, product: CatalogProduct,
                      previous: CatalogSnapshot | None, new_payload: dict[str, Any],
                      organization_id: str, store_id: str,
                      timestamp: datetime) -> list[CatalogConflict]:
    conflicts: list[CatalogConflict] = []
    extension = repository.extension_for(session, product.id)
    if extension is None or previous is None:
        return conflicts
    local = (extension.payload or {}).get("overrides", {})
    for field_name, local_value in local.items():
        new_value = new_payload.get(field_name)
        old_value = (previous.payload or {}).get(field_name)
        if new_value != old_value and new_value != local_value:
            source_value = {"value": new_value}
            if repository.pending_conflict(session, product.id, field_name, source_value) is None:
                conflict = CatalogConflict(
                    id=str(uuid4()), product_id=product.id,
                    organization_id=organization_id, store_id=store_id,
                    field_name=field_name, source_value=source_value,
                    local_value={"value": local_value}, status="pending",
                    reason="source changed while a local extension exists; sync must not overwrite",
                    created_at=timestamp,
                )
                session.add(conflict)
                conflicts.append(conflict)
    session.flush()
    return conflicts


def record_snapshot(session: Session, *, organization_id: str, store_id: str,
                    source_system: str, source_version: str,
                    source_updated_at: datetime | None, payload: dict[str, Any],
                    now: datetime | None = None) -> tuple[CatalogProduct, CatalogSnapshot, list[CatalogConflict]]:
    timestamp = _now(now)
    mapped = map_source_payload(payload)
    # The digest covers the source observation (payload + version/time), so an
    # A -> B -> A return is a new observation even when the payload bytes match
    # an older snapshot.
    observation = {
        "payload": mapped,
        "source_version": source_version,
        "source_updated_at": source_updated_at.isoformat() if source_updated_at else None,
    }
    sha = hashlib.sha256(_canonical(observation).encode("utf-8")).hexdigest()
    product = repository.find_by_external(session, organization_id, store_id,
                                         source_system, mapped["external_code"])
    if product is None:
        product = CatalogProduct(
            id=str(uuid4()), organization_id=organization_id, store_id=store_id,
            source_system=source_system, external_code=mapped["external_code"],
            source_version=source_version, source_updated_at=source_updated_at,
            active=True, version=1, created_at=timestamp, updated_at=timestamp,
        )
        session.add(product)
        session.flush()
    raw_barcode = mapped.get("barCode")
    product.barcode_sha256 = (hashlib.sha256(raw_barcode.encode()).hexdigest()
                              if isinstance(raw_barcode, str) else None)
    previous = repository.latest_snapshot(session, product)
    existing = repository.snapshot_by_sha(session, product.id, sha)
    if existing is not None:
        if product.current_snapshot_id == existing.id:
            return product, existing, []  # exact replay of the current observation
        # Historical observation returned: restore the current projection and
        # re-run conflict checks against the previous current snapshot.
        conflicts = _detect_conflicts(
            session, product=product, previous=previous,
            new_payload=existing.payload, organization_id=organization_id,
            store_id=store_id, timestamp=timestamp)
        product.current_snapshot_id = existing.id
        product.source_version = source_version
        product.source_updated_at = source_updated_at
        product.version += 1
        product.updated_at = timestamp
        session.flush()
        return product, existing, conflicts
    snapshot = CatalogSnapshot(
        id=str(uuid4()), product_id=product.id, organization_id=organization_id,
        store_id=store_id, source_version=source_version,
        source_updated_at=source_updated_at, payload=mapped,
        payload_sha256=sha, captured_at=timestamp,
    )
    conflicts = _detect_conflicts(
        session, product=product, previous=previous, new_payload=mapped,
        organization_id=organization_id, store_id=store_id, timestamp=timestamp)
    product.current_snapshot_id = snapshot.id
    product.source_version = source_version
    product.source_updated_at = source_updated_at
    product.version += 1
    product.updated_at = timestamp
    session.add(snapshot)
    session.flush()
    return product, snapshot, conflicts


def add_extension(session: Session, *, organization_id: str, store_id: str,
                  product_id: str, payload: dict[str, Any], updated_by: str,
                  now: datetime | None = None) -> CatalogExtension:
    timestamp = _now(now)
    product = repository.find_product(session, organization_id, store_id, product_id)
    if product is None:
        raise HTTPException(404, "catalog_product_not_found")
    extension = repository.extension_for(session, product.id)
    if extension is None:
        extension = CatalogExtension(
            id=str(uuid4()), product_id=product.id,
            organization_id=organization_id, store_id=store_id,
            payload=payload, version=1, updated_by=updated_by, updated_at=timestamp,
        )
        session.add(extension)
    else:
        extension.payload = payload
        extension.version += 1
        extension.updated_by = updated_by
        extension.updated_at = timestamp
    session.flush()
    return extension


def list_products(session: Session, *, organization_id: str, store_id: str,
                  limit: int = 50) -> list[tuple[CatalogProduct, CatalogSnapshot | None]]:
    return [(product, repository.latest_snapshot(session, product))
            for product in repository.list_products(session, organization_id, store_id, limit)]


def get_product(session: Session, *, organization_id: str, store_id: str,
                product_id: str) -> tuple[CatalogProduct, CatalogSnapshot | None]:
    product = repository.find_product(session, organization_id, store_id, product_id)
    if product is None:
        raise HTTPException(404, "catalog_product_not_found")
    return product, repository.latest_snapshot(session, product)


def list_conflicts(session: Session, *, organization_id: str, store_id: str,
                   limit: int = 50) -> list[CatalogConflict]:
    return repository.list_conflicts(session, organization_id, store_id, limit)
