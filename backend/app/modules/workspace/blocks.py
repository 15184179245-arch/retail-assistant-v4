"""WSP-002 server-side Generative UI block contract validation, ref resolution, capability filtering, and pin-to-artifact."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.identity.service import resolve_effective_capabilities
from backend.app.modules.locations.models import Location, PlacementAssertion
from backend.app.modules.pricing.models import PricingQuote
from backend.app.modules.purchasing.models import PurchaseEvidence, PurchaseIntent
from backend.app.modules.receiving.models import ReceivingBatch
from backend.app.modules.sales.models import SalesDraft
from backend.app.modules.stocktake.models import StocktakeSession
from backend.app.modules.tasks.models import Task
from backend.app.modules.workspace import service as workspace_service
from backend.app.modules.workspace.models import WorkspaceArtifact
from backend.app.modules.workspace.schemas import (
    ArtifactCreateRequest,
    BlockPinData,
    BlockPinRequest,
    BlockProvenance,
    ResolvedBlockRecord,
    StatusKind,
)

_SCHEMA_PATH = Path(__file__).resolve().parents[4] / "contracts" / "ui-blocks.schema.json"
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.\d+)*$")
_SUPPORTED_MAJORS = frozenset({0, 1})  # current major (v1) + previous major (v0)
_V1_VERSION_RE = re.compile(r"^v?1(\.[0-9]+)*$")
_SAFE_FIELD_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_UNSAFE_TEXT_RE = re.compile(
    r"<[a-zA-Z!/]|\bon(?:click|load|error|mouseover|focus|submit)[ \t]*=|javascript[ \t]*:",
    re.IGNORECASE,
)

_BLOCK_TO_ARTIFACT_KIND: dict[str, str] = {
    "quote_draft": "quote_draft",
    "purchase_draft": "purchase_draft",
    "stock_location": "stocktake_review",
    "work_item_list": "work_item_sheet",
}


class BlockValidationError(Exception):
    """Raised when strict JSON-schema contract validation rejects a UI block."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@lru_cache(maxsize=1)
def load_ui_blocks_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def get_block_whitelist() -> tuple[str, ...]:
    schema = load_ui_blocks_schema()
    return tuple(schema["properties"]["type"]["enum"])


def get_forbidden_prop_names() -> frozenset[str]:
    schema = load_ui_blocks_schema()
    return frozenset(schema["$defs"]["forbiddenPropNames"]["not"]["enum"])


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _scan_forbidden_props(node: Any, forbidden_names: frozenset[str], path: str = "props") -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if not isinstance(key, str):
                raise BlockValidationError("invalid_props_key", f"{path} 包含非字符串键名")
            if key in forbidden_names:
                raise BlockValidationError(
                    f"forbidden_model_field:{key}",
                    f"模型不得自填权威价格/库存/金额或 HTML 字段 `{key}`",
                )
            _scan_forbidden_props(val, forbidden_names, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _scan_forbidden_props(item, forbidden_names, f"{path}[{idx}]")
    elif isinstance(node, str):
        if len(node) > 2000:
            raise BlockValidationError("text_too_long", f"{path} 文本超过最大长度限制 2000")
        if _UNSAFE_TEXT_RE.search(node):
            raise BlockValidationError(
                "unsafe_html_or_script",
                f"{path} 禁止包含原始 HTML 标签或脚本片段",
            )


def _validate_ref_item(ref: Any, schema: dict[str, Any]) -> None:
    if not isinstance(ref, dict):
        raise BlockValidationError("invalid_ref", "refs 元素必须为对象")
    ref_def = schema["$defs"]["blockRef"]
    allowed_keys = set(ref_def["properties"].keys())
    extra_keys = set(ref.keys()) - allowed_keys
    if extra_keys:
        raise BlockValidationError("invalid_ref_keys", f"refs 包含未允许字段: {sorted(extra_keys)}")
    for req_key in ref_def["required"]:
        if req_key not in ref:
            raise BlockValidationError("missing_ref_field", f"refs 缺少必填字段 {req_key}")
    allowed_kinds = set(ref_def["properties"]["kind"]["enum"])
    if ref.get("kind") not in allowed_kinds:
        raise BlockValidationError("invalid_ref_kind", f"不支持的 ref.kind: {ref.get('kind')}")
    ref_id = ref.get("id")
    if not isinstance(ref_id, str) or not (1 <= len(ref_id) <= 200):
        raise BlockValidationError("invalid_ref_id", "ref.id 必须为 1-200 字符的非空字符串")
    if "version" in ref and not (isinstance(ref["version"], (str, int)) and not isinstance(ref["version"], bool)):
        raise BlockValidationError("invalid_ref_version", "ref.version 必须为字符串或整数")
    if "field" in ref and (not isinstance(ref["field"], str) or len(ref["field"]) > 100):
        raise BlockValidationError("invalid_ref_field", "ref.field 必须为不超过 100 字符的字符串")
    if "label" in ref and not isinstance(ref["label"], str):
        raise BlockValidationError("invalid_ref_label", "ref.label 必须为字符串")


def _validate_variant_props(block_type: str, props: dict[str, Any], refs: list[Any], schema: dict[str, Any]) -> None:
    variant_key_map = {
        "product_card": "productCardBlock",
        "product_list": "productListBlock",
        "compare_table": "compareTableBlock",
        "quote_draft": "quoteDraftBlock",
        "purchase_draft": "purchaseDraftBlock",
        "stock_location": "stockLocationBlock",
        "work_item_list": "workItemListBlock",
        "evidence_panel": "evidencePanelBlock",
        "action_card": "actionCardBlock",
        "form_request": "formRequestBlock",
        "artifact_link": "artifactLinkBlock",
    }
    variant = schema["$defs"][variant_key_map[block_type]]
    min_refs = variant.get("properties", {}).get("refs", {}).get("minItems", 0)
    if len(refs) < min_refs:
        raise BlockValidationError("missing_refs", f"{block_type} 至少需要 {min_refs} 个引用 refs")

    props_schema = variant["properties"]["props"]
    allowed_prop_keys = set(props_schema.get("properties", {}).keys())
    if props_schema.get("additionalProperties") is False:
        extra = set(props.keys()) - allowed_prop_keys
        if extra:
            raise BlockValidationError(
                "invalid_props_fields",
                f"{block_type}.props 包含未允许字段: {sorted(extra)}",
            )
    for req_key in props_schema.get("required", []):
        if req_key not in props:
            raise BlockValidationError(
                "missing_required_prop",
                f"{block_type}.props 缺少必填字段 {req_key}",
            )

    prop_defs = props_schema.get("properties", {})
    for key, val in props.items():
        spec = prop_defs.get(key)
        if spec is None:
            continue
        expected_type = spec.get("type")
        if expected_type == "boolean":
            if not isinstance(val, bool):
                raise BlockValidationError("invalid_prop_type", f"props.{key} 必须为布尔值")
        elif expected_type == "integer":
            if not isinstance(val, int) or isinstance(val, bool):
                raise BlockValidationError("invalid_prop_type", f"props.{key} 必须为整数")
            if "minimum" in spec and val < spec["minimum"]:
                raise BlockValidationError("invalid_prop_range", f"props.{key} 不能小于 {spec['minimum']}")
            if "maximum" in spec and val > spec["maximum"]:
                raise BlockValidationError("invalid_prop_range", f"props.{key} 不能大于 {spec['maximum']}")
        elif expected_type == "array":
            if not isinstance(val, list):
                raise BlockValidationError("invalid_prop_type", f"props.{key} 必须为数组")
            if "minItems" in spec and len(val) < spec["minItems"]:
                raise BlockValidationError("invalid_prop_items", f"props.{key} 至少包含 {spec['minItems']} 项")
            if key == "line_refs":
                for item in val:
                    if not isinstance(item, dict):
                        raise BlockValidationError("invalid_line_ref", "line_refs 元素必须为对象")
                    if set(item.keys()) - {"ref_id", "qty", "unit", "note"}:
                        raise BlockValidationError("invalid_line_ref_keys", "line_refs 包含非法字段")
                    if not isinstance(item.get("ref_id"), str) or not item["ref_id"]:
                        raise BlockValidationError("invalid_line_ref_id", "line_ref.ref_id 无效")
                    qty = item.get("qty")
                    if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
                        raise BlockValidationError("invalid_line_ref_qty", "line_ref.qty 必须大于 0")
            elif key == "fields":
                for field_spec in val:
                    if not isinstance(field_spec, dict):
                        raise BlockValidationError("invalid_form_field", "form_request.fields 元素必须为对象")
                    if set(field_spec.keys()) - {"name", "label", "field_type", "required", "options", "placeholder"}:
                        raise BlockValidationError("invalid_form_field_keys", "表单字段包含未允许属性")
                    for req in ("name", "label", "field_type"):
                        if req not in field_spec:
                            raise BlockValidationError("missing_form_field_prop", f"表单字段缺少 {req}")
                    if not isinstance(field_spec["name"], str) or not _SAFE_FIELD_NAME_RE.match(field_spec["name"]):
                        raise BlockValidationError("invalid_form_field_name", "表单字段名格式非法")
                    if field_spec["field_type"] not in {"text", "number", "select", "boolean", "date"}:
                        raise BlockValidationError("invalid_form_field_type", "不支持的表单字段类型")
        elif expected_type == "object":
            if not isinstance(val, dict):
                raise BlockValidationError("invalid_prop_type", f"props.{key} 必须为对象")
        else:
            if not isinstance(val, str):
                raise BlockValidationError("invalid_prop_type", f"props.{key} 必须为字符串")
            if "enum" in spec and val not in spec["enum"]:
                raise BlockValidationError("invalid_prop_enum", f"props.{key} 值 {val!r} 不在允许枚举中")
            if "minLength" in spec and len(val) < spec["minLength"]:
                raise BlockValidationError("invalid_prop_length", f"props.{key} 长度不足")
            if "maxLength" in spec and len(val) > spec["maxLength"]:
                raise BlockValidationError("invalid_prop_length", f"props.{key} 超过最大长度")


def validate_block_contract(
    block: dict[str, Any],
    *,
    allow_previous_major: bool = False,
) -> dict[str, Any]:
    """Validate a raw block payload against contracts/ui-blocks.schema.json."""
    if not isinstance(block, dict):
        raise BlockValidationError("invalid_block_shape", "UI 块必须为 JSON 对象")

    schema = load_ui_blocks_schema()
    required = schema["required"]
    for key in required:
        if key not in block:
            raise BlockValidationError(f"missing_field:{key}", f"UI 块缺少必填字段 `{key}`")

    extra = set(block.keys()) - set(required)
    if extra:
        raise BlockValidationError("unexpected_block_fields", f"UI 块包含未允许字段: {sorted(extra)}")

    block_type = block["type"]
    whitelist = get_block_whitelist()
    if not isinstance(block_type, str) or block_type not in whitelist:
        raise BlockValidationError(f"unknown_block_type:{block_type}", f"不在白名单内的 UI 块类型: {block_type}")

    version = block["version"]
    if not isinstance(version, str):
        raise BlockValidationError("invalid_block_version", "UI 块 version 必须为字符串")
    if allow_previous_major:
        match = _VERSION_RE.match(version)
        if match is None or int(match.group(1)) not in _SUPPORTED_MAJORS:
            raise BlockValidationError(
                f"unsupported_block_version:{version}",
                f"不支持的 UI 块版本: {version}",
            )
    else:
        if _V1_VERSION_RE.match(version) is None:
            raise BlockValidationError(
                f"unsupported_block_version:{version}",
                f"当前 schema 仅接受 v1 UI 块版本: {version}",
            )

    props = block["props"]
    if not isinstance(props, dict):
        raise BlockValidationError("invalid_props", "props 必须为 JSON 对象")
    refs = block["refs"]
    if not isinstance(refs, list):
        raise BlockValidationError("invalid_refs", "refs 必须为数组")

    forbidden = get_forbidden_prop_names()
    _scan_forbidden_props(props, forbidden)
    for ref in refs:
        _validate_ref_item(ref, schema)
        _scan_forbidden_props(ref, forbidden, "refs")
    _validate_variant_props(block_type, props, refs, schema)
    return copy.deepcopy(block)


def _degraded_block(
    raw_block: Any,
    *,
    reason: str,
    fallback_text: str,
    stripped_fields: list[str] | None = None,
) -> ResolvedBlockRecord:
    raw_dict = raw_block if isinstance(raw_block, dict) else {}
    safe_type = raw_dict.get("type") if isinstance(raw_dict.get("type"), str) else "unknown"
    safe_version = raw_dict.get("version") if isinstance(raw_dict.get("version"), str) else "unknown"
    return ResolvedBlockRecord(
        type=safe_type,
        version=safe_version,
        props={},
        refs=[],
        resolved_data={},
        provenance=BlockProvenance(
            source="server_guard",
            synced_at=None,
            verified_by="workspace.blocks",
            status_kind="unverified",
            stale_refs=[],
            stripped_fields=stripped_fields or [],
        ),
        status_kind="unverified",
        degraded=True,
        fallback_text=fallback_text,
        reason=reason,
    )


def _version_matches(expected_version: str | int | None, *actual_candidates: object) -> bool:
    if expected_version is None:
        return True
    expected_str = str(expected_version).strip()
    for candidate in actual_candidates:
        if candidate is not None and str(candidate).strip() == expected_str:
            return True
    return False


def _resolve_single_ref(
    session: Session,
    actor: AuthenticatedSubject,
    ref: dict[str, Any],
    cap_set: set[str],
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]], set[str], list[datetime]]:
    """Resolve one ref inside caller's store scope and apply capability-based field filtering."""
    kind = str(ref["kind"])
    ref_id = str(ref["id"])
    expected_ver = ref.get("version")
    stripped: list[str] = []
    stale_refs: list[dict[str, Any]] = []
    sources: set[str] = set()
    timestamps: list[datetime] = []

    def mark_stale(actual_ver: Any, label: str = "待核实") -> None:
        stale_refs.append(
            {
                "kind": kind,
                "id": ref_id,
                "expected_version": expected_ver,
                "actual_version": actual_ver,
                "label": label,
            }
        )

    if kind == "product":
        product = session.execute(
            select(CatalogProduct).where(CatalogProduct.id == ref_id)
        ).scalar_one_or_none()
        if product is None or product.organization_id != actor.organization_id or product.store_id != actor.store_id:
            mark_stale(None, "待核实")
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("catalog")
        timestamps.append(product.updated_at)
        if not _version_matches(expected_ver, product.version, product.source_version):
            mark_stale(product.version)

        item: dict[str, Any] = {
            "kind": kind,
            "id": product.id,
            "found": True,
            "version": product.version,
            "source_version": product.source_version,
        }
        if "catalog.read" in cap_set:
            snap = None
            if product.current_snapshot_id:
                snap = session.execute(
                    select(CatalogSnapshot).where(CatalogSnapshot.id == product.current_snapshot_id)
                ).scalar_one_or_none()
            snap_payload = dict(snap.payload or {}) if snap else {}
            if snap is not None:
                timestamps.append(snap.captured_at)
            item["external_code"] = product.external_code
            item["name"] = snap_payload.get("name") or snap_payload.get("title") or product.external_code
            item["spec"] = snap_payload.get("spec")
            item["unit"] = snap_payload.get("unit")
            if "stocktake.read" in cap_set or "locations.read" in cap_set or "inventory.stocktake" in cap_set or "guanjia.inventory.read" in cap_set:
                if "stock_qty" in snap_payload:
                    item["stock_qty"] = snap_payload["stock_qty"]
            else:
                if "stock_qty" in snap_payload:
                    stripped.append("stock_qty")
        else:
            stripped.extend(["product.name", "product.external_code", "product.spec", "product.unit"])

        quote = session.execute(
            select(PricingQuote)
            .where(
                PricingQuote.organization_id == actor.organization_id,
                PricingQuote.store_id == actor.store_id,
                PricingQuote.product_id == product.id,
            )
            .order_by(PricingQuote.created_at.desc())
        ).scalars().first()
        if quote is not None:
            sources.add("pricing")
            timestamps.append(quote.created_at)
            if "sales.read" in cap_set or "pricing.quote" in cap_set:
                item["unit_price"] = _json_safe(quote.unit_price)
                item["list_price"] = _json_safe(quote.list_price)
                item["final_amount"] = _json_safe(quote.final_amount)
            else:
                stripped.extend(["unit_price", "list_price", "final_amount"])
            if "pricing.cost.read" in cap_set and quote.cost_amount is not None:
                item["cost_amount"] = _json_safe(quote.cost_amount)
            elif quote.cost_amount is not None:
                stripped.append("cost_amount")

        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "price":
        quote = session.execute(
            select(PricingQuote).where(
                PricingQuote.id == ref_id,
                PricingQuote.organization_id == actor.organization_id,
                PricingQuote.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if quote is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("pricing")
        timestamps.append(quote.created_at)
        now = datetime.now(timezone.utc)
        expires_at = quote.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not _version_matches(expected_ver, quote.rule_version, quote.object_version) or expires_at <= now or quote.status not in ("quoted", "active"):
            mark_stale(quote.rule_version)
        item = {"kind": kind, "id": quote.id, "found": True, "product_id": quote.product_id, "rule_version": quote.rule_version}
        if "sales.read" in cap_set or "pricing.quote" in cap_set:
            item["unit_price"] = _json_safe(quote.unit_price)
            item["list_price"] = _json_safe(quote.list_price)
            item["final_amount"] = _json_safe(quote.final_amount)
            item["discount_bps"] = quote.discount_bps
        else:
            stripped.extend(["unit_price", "list_price", "final_amount"])
        if "pricing.cost.read" in cap_set and quote.cost_amount is not None:
            item["cost_amount"] = _json_safe(quote.cost_amount)
        elif quote.cost_amount is not None:
            stripped.append("cost_amount")
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "quote":
        draft = session.execute(
            select(SalesDraft).where(
                SalesDraft.id == ref_id,
                SalesDraft.organization_id == actor.organization_id,
                SalesDraft.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if draft is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("sales")
        timestamps.append(draft.updated_at)
        if not _version_matches(expected_ver, draft.version):
            mark_stale(draft.version)
        item = {"kind": kind, "id": draft.id, "found": True, "version": draft.version}
        if "sales.read" in cap_set:
            item["stage"] = draft.stage
            item["status"] = draft.status
            item["currency"] = draft.currency
            item["total_amount"] = _json_safe(draft.total_amount)
        else:
            stripped.extend(["stage", "status", "total_amount"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "purchase":
        intent = session.execute(
            select(PurchaseIntent).where(
                PurchaseIntent.id == ref_id,
                PurchaseIntent.organization_id == actor.organization_id,
                PurchaseIntent.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if intent is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("purchasing")
        timestamps.append(intent.updated_at)
        if not _version_matches(expected_ver, intent.version):
            mark_stale(intent.version)
        item = {"kind": kind, "id": intent.id, "found": True, "version": intent.version}
        if "purchasing.read" in cap_set:
            item["status"] = intent.status
            item["evidence_id"] = intent.evidence_id
        else:
            stripped.extend(["status", "evidence_id"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind in ("location", "stock"):
        location = session.execute(
            select(Location).where(
                Location.id == ref_id,
                Location.organization_id == actor.organization_id,
                Location.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if location is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("locations")
        timestamps.append(location.updated_at)
        item = {"kind": kind, "id": location.id, "found": True}
        if "locations.read" in cap_set or "stocktake.read" in cap_set or "inventory.stocktake" in cap_set:
            item["name"] = location.name
            item["location_kind"] = location.kind
            item["active"] = location.active
            assertions = session.execute(
                select(PlacementAssertion).where(
                    PlacementAssertion.organization_id == actor.organization_id,
                    PlacementAssertion.store_id == actor.store_id,
                    PlacementAssertion.location_id == location.id,
                )
            ).scalars().all()
            item["placements"] = [
                {"product_id": a.product_id, "kind": a.kind, "asserted_at": a.asserted_at.isoformat()}
                for a in assertions
            ]
        else:
            stripped.extend(["name", "location_kind", "placements"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "stocktake":
        session_row = session.execute(
            select(StocktakeSession).where(
                StocktakeSession.id == ref_id,
                StocktakeSession.organization_id == actor.organization_id,
                StocktakeSession.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if session_row is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("stocktake")
        timestamps.append(session_row.created_at)
        if not _version_matches(expected_ver, session_row.version):
            mark_stale(session_row.version)
        item = {"kind": kind, "id": session_row.id, "found": True, "version": session_row.version}
        if "stocktake.read" in cap_set or "inventory.stocktake" in cap_set:
            item["coverage_kind"] = session_row.coverage_kind
            item["status"] = session_row.status
            item["coverage_note"] = session_row.coverage_note
        else:
            stripped.extend(["coverage_kind", "status", "coverage_note"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "receiving":
        batch = session.execute(
            select(ReceivingBatch).where(
                ReceivingBatch.id == ref_id,
                ReceivingBatch.organization_id == actor.organization_id,
                ReceivingBatch.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if batch is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("receiving")
        timestamps.append(batch.created_at)
        if not _version_matches(expected_ver, batch.version):
            mark_stale(batch.version)
        item = {"kind": kind, "id": batch.id, "found": True, "version": batch.version}
        if "receiving.read" in cap_set:
            item["status"] = batch.status
            item["intent_id"] = batch.intent_id
        else:
            stripped.extend(["status", "intent_id"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "work_item":
        task = session.execute(
            select(Task).where(
                Task.id == ref_id,
                Task.organization_id == actor.organization_id,
                Task.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if task is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("tasks")
        timestamps.append(task.created_at)
        can_read_any = "tasks.read" in cap_set or "tasks.manage" in cap_set
        can_read_self = "tasks.read_self" in cap_set and (
            task.created_by == actor.subject_id or task.assignee_id == actor.subject_id
        )
        item = {"kind": kind, "id": task.id, "found": True}
        if can_read_any or can_read_self:
            item["title"] = task.title
            item["state"] = task.state
            item["decision_state"] = task.decision_state
        else:
            stripped.extend(["title", "state", "decision_state"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "evidence":
        ev = session.execute(
            select(PurchaseEvidence).where(
                PurchaseEvidence.id == ref_id,
                PurchaseEvidence.organization_id == actor.organization_id,
                PurchaseEvidence.store_id == actor.store_id,
            )
        ).scalar_one_or_none()
        if ev is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("purchasing_evidence")
        timestamps.append(ev.created_at)
        item = {"kind": kind, "id": ev.id, "found": True}
        if "purchasing.read" in cap_set or "agents.purchasing.read_evidence" in cap_set:
            item["source_kind"] = ev.source_kind
            item["summary"] = ev.summary
        else:
            stripped.extend(["source_kind", "summary"])
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    if kind == "artifact":
        art = session.execute(
            select(WorkspaceArtifact).where(
                WorkspaceArtifact.id == ref_id,
                WorkspaceArtifact.organization_id == actor.organization_id,
                WorkspaceArtifact.store_id == actor.store_id,
                WorkspaceArtifact.owner_subject_id == actor.subject_id,
            )
        ).scalar_one_or_none()
        if art is None:
            mark_stale(None)
            return {"kind": kind, "id": ref_id, "found": False, "verification_label": "待核实"}, stripped, stale_refs, sources, timestamps
        sources.add("workspace_artifact")
        timestamps.append(art.updated_at)
        if not _version_matches(expected_ver, art.version):
            mark_stale(art.version)
        item = {
            "kind": kind,
            "id": art.id,
            "found": True,
            "artifact_kind": art.kind,
            "title": art.title,
            "status": art.status,
            "version": art.version,
        }
        kind_read_caps = workspace_service._KIND_PERMISSION_MAP[art.kind]["read"]
        if any(p in cap_set for p in kind_read_caps):
            payload_copy = copy.deepcopy(art.payload or {})
            if "pricing.cost.read" not in cap_set:
                for cost_key in ("cost_price", "cost_amount"):
                    if cost_key in payload_copy:
                        payload_copy.pop(cost_key, None)
                        stripped.append(cost_key)
            item["payload"] = payload_copy
        else:
            stripped.append("payload")
        item["verification_label"] = "待核实" if stale_refs else "已核实"
        return item, stripped, stale_refs, sources, timestamps

    # command or other ref kind with no backing store table: treated as verified command link unless explicitly versioned
    sources.add("command")
    return {"kind": kind, "id": ref_id, "found": True, "verification_label": "已核实"}, stripped, stale_refs, sources, timestamps


def resolve_block(
    session: Session,
    actor: AuthenticatedSubject,
    raw_block: dict[str, Any],
) -> ResolvedBlockRecord:
    """Validate and resolve a Generative UI block with server truth, provenance, and RBAC filtering."""
    workspace_service._authorize_store_member(session, actor)
    try:
        validated = validate_block_contract(raw_block, allow_previous_major=True)
    except BlockValidationError as exc:
        fallback_title = ""
        if isinstance(raw_block, dict):
            raw_props = raw_block.get("props")
            if isinstance(raw_props, dict) and isinstance(raw_props.get("title"), str):
                candidate = raw_props["title"]
                if not _UNSAFE_TEXT_RE.search(candidate):
                    fallback_title = f"{candidate}："
        return _degraded_block(
            raw_block,
            reason=exc.reason,
            fallback_text=f"{fallback_title}该智能卡片已降级展示（原因：{exc.message}）",
        )

    caps, _ = resolve_effective_capabilities(session, actor)
    cap_set = set(caps)

    resolved_items: list[dict[str, Any]] = []
    all_stripped: list[str] = []
    all_stale: list[dict[str, Any]] = []
    all_sources: set[str] = set()
    all_timestamps: list[datetime] = []

    for ref in validated["refs"]:
        item, stripped, stale, sources, timestamps = _resolve_single_ref(session, actor, ref, cap_set)
        resolved_items.append(item)
        all_stripped.extend(stripped)
        all_stale.extend(stale)
        all_sources.update(sources)
        all_timestamps.extend(timestamps)

    unique_stripped = list(dict.fromkeys(all_stripped))
    latest_sync = max(all_timestamps) if all_timestamps else None
    primary_denied = any(
        f in unique_stripped
        for f in (
            "product.name",
            "stage",
            "status",
            "coverage_kind",
            "title",
            "payload",
            "intent_id",
        )
    )

    if not validated["refs"]:
        status_kind: StatusKind = "suggestion"
        source_name = validated["type"]
    elif all_stale or primary_denied or any(not it.get("found", True) for it in resolved_items):
        status_kind = "unverified"
        source_name = "+".join(sorted(all_sources)) if all_sources else "server_resolver"
    else:
        status_kind = "verified"
        source_name = "+".join(sorted(all_sources)) if all_sources else "server_resolver"

    resolved_data: dict[str, Any] = {
        "items": resolved_items,
        "verification_label": "待核实" if status_kind == "unverified" else "已核实",
    }
    if validated["type"] in ("quote_draft", "purchase_draft") and validated["props"].get("line_refs"):
        by_id = {str(it.get("id")): it for it in resolved_items}
        resolved_lines = []
        for lref in validated["props"]["line_refs"]:
            target = by_id.get(lref["ref_id"], {})
            line_entry = {
                "ref_id": lref["ref_id"],
                "qty": lref["qty"],
                "unit": lref.get("unit") or target.get("unit"),
                "note": lref.get("note"),
                "name": target.get("name"),
                "verification_label": target.get("verification_label", "待核实"),
            }
            if "unit_price" in target:
                line_entry["unit_price"] = target["unit_price"]
            if "cost_amount" in target:
                line_entry["cost_amount"] = target["cost_amount"]
            resolved_lines.append(line_entry)
        resolved_data["lines"] = resolved_lines

    provenance = BlockProvenance(
        source=source_name,
        synced_at=latest_sync,
        verified_by="server_registry",
        status_kind=status_kind,
        stale_refs=all_stale,
        stripped_fields=unique_stripped,
    )
    return ResolvedBlockRecord(
        type=validated["type"],
        version=validated["version"],
        props=validated["props"],
        refs=validated["refs"],
        resolved_data=resolved_data,
        provenance=provenance,
        status_kind=status_kind,
        degraded=False,
        fallback_text=None,
        reason=None,
    )


def pin_block_to_artifact(
    session: Session,
    actor: AuthenticatedSubject,
    req: BlockPinRequest,
    *,
    header_idempotency_key: str | None = None,
) -> BlockPinData:
    """Resolve a Generative UI block server-side and pin it as a persistent WorkspaceArtifact."""
    resolved = resolve_block(session, actor, req.block)
    if resolved.degraded:
        raise workspace_service.WorkspaceError(
            status_code=422,
            code="DEGRADED_BLOCK_CANNOT_PIN",
            error=resolved.reason or "degraded_block",
            user_message=f"降级或未通过校验的 UI 块不能固定为工作区对象：{resolved.fallback_text}",
        )

    artifact_kind = req.kind or _BLOCK_TO_ARTIFACT_KIND.get(resolved.type, "work_item_sheet")
    props = resolved.props
    title = req.title or props.get("title") or f"{resolved.type} 固定草稿"

    payload: dict[str, Any] = {
        "pinned_from_block_type": resolved.type,
        "status_kind": resolved.status_kind,
        "provenance": resolved.provenance.model_dump(mode="json"),
        "resolved_items": resolved.resolved_data.get("items", []),
    }
    if artifact_kind == "quote_draft":
        payload["stage"] = props.get("stage", "draft")
        payload["status"] = props.get("status", "pending")
        payload["lines"] = resolved.resolved_data.get("lines") or resolved.resolved_data.get("items", [])
        if props.get("customer_label"):
            payload["customer_label"] = props["customer_label"]
        if props.get("note"):
            payload["note"] = props["note"]
    elif artifact_kind == "purchase_draft":
        payload["lines"] = resolved.resolved_data.get("lines") or resolved.resolved_data.get("items", [])
        if props.get("supplier_label"):
            payload["supplier_label"] = props["supplier_label"]
        if props.get("note"):
            payload["note"] = props["note"]
    else:
        payload["items"] = resolved.resolved_data.get("items", [])

    mutation = workspace_service.create_artifact(
        session,
        actor,
        ArtifactCreateRequest(
            thread_id=req.thread_id,
            kind=artifact_kind,  # type: ignore[arg-type]
            title=str(title),
            payload=payload,
            source_refs=copy.deepcopy(resolved.refs),
            actor_type="human",
            idempotency_key=req.idempotency_key,
        ),
        header_idempotency_key=header_idempotency_key,
    )

    workspace_service._emit_thread_event(
        session,
        organization_id=actor.organization_id,
        store_id=actor.store_id,
        thread_id=req.thread_id,
        subject_id=actor.subject_id,
        artifact_id=mutation.artifact.id,
        event_type="block_pinned",
        payload={
            "artifact_id": mutation.artifact.id,
            "block_type": resolved.type,
            "kind": artifact_kind,
            "status_kind": resolved.status_kind,
        },
        now=mutation.artifact.updated_at,
    )
    return BlockPinData(
        artifact=mutation.artifact,
        resolved_block=resolved,
        op=mutation.op,
    )
