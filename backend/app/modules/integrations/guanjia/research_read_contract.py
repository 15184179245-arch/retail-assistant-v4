"""Host-approved bounded read contracts, separate from untrusted G1 research.

No manifest issuer, registration, grant, login or business write is provided.
A trusted Host alone selects a reviewed private manifest AND its expected SHA.
Hashes identify bytes; they are not authority. No model-controlled path/URL.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import re
import os

from backend.app.modules.agents import research_contracts as c, research_catalog as catalog
from .live_structure import _decode, _Number

HOST = "erp.example.invalid"
AGENT_PERMISSION = "agents.guanjia.research.read"
# Reviewed paths and lexical field subsets, not proof of a particular account's
# report/field rights. Issuing a Host manifest requires independent scope proof.
SPECS = {
    "sale_lines": {
        "candidate": "gj.sales.bill-lines", "domain": "sales",
        "path": "/api/services/app/V1/Erp/Report/SaleBill/Summary/Detail/Get",
        "scope_request": "shopIds", "scope_row": "shopId",
        "text": ("billId", "productId", "standardId", "shopId", "whsId", "unit", "unitId"),
        "number": ("qty", "price", "total"), "boolean": ("havePermissoin",),
    },
    "retail_lines": {
        "candidate": "gj.sales.retail-product-lines", "domain": "sales",
        "path": "/api/services/app/ReportSaleAnalysis/GetAllProductSaleDetail",
        "scope_request": "storeIdList", "scope_row": "storeId",
        "text": ("productId", "standardId", "storeId", "unit", "unitId"),
        "number": ("qty", "price", "total"), "boolean": (),
    },
    "stock_condition": {
        "candidate": "gj.inventory.stock-condition", "domain": "inventory",
        "path": "/api/services/app/V1/Erp/Report/Stock/StockCondition/Get",
        "scope_request": "shopId", "scope_row": "shopId",
        "text": ("productId", "standardId", "shopId", "whsId", "unit", "unitId"),
        "number": ("stockQty", "subUnitQty", "assitQty"), "boolean": (),
    },
    "product_page": {
        "candidate": "gj.catalog.product-page", "domain": "catalog",
        "path": "/api/services/app/BaseProduct/GetAll",
        "scope_request": "affiliatedStoreIds", "scope_row": "storeId",
        "text": ("id", "storeId", "productCategoryId", "code", "name"),
        "number": (), "boolean": ("isActive",),
    },
}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)


class ReadDenied(ValueError):
    def __init__(self):
        super().__init__("guanjia_research_read_denied")


def deny():
    raise ReadDenied() from None


def identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        deny()
    return value


def integer(value, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        deny()
    return value


def utcnow():
    return datetime.now(timezone.utc)


def permissions(operation):
    if type(operation) is not str or operation not in SPECS:
        deny()
    return ("guanjia." + SPECS[operation]["domain"] + ".read", "guanjia.company.read")


@dataclass(frozen=True, repr=False)
class ApprovedReadContract:
    """Host-only location/pin, NOT a user or model tool argument."""
    directory: str
    manifest_sha256: str

    def load(self):
        root = None
        try:
            root = catalog._open_root(self.directory)
            info = os.fstat(root)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                deny()
            raw = catalog._read_exact(root, "approved-read.json", self.manifest_sha256, 16384)
            value = c.decode(raw, 16384)
            validate_manifest(value)
            return value
        except Exception:
            deny()
        finally:
            if root is not None:
                os.close(root)


def validate_manifest(value):
    c.exact(value, ("version", "contract_id", "operation", "semantics", "corpus_digest",
        "candidate_digest", "request_schema_digest", "result_schema_digest", "evidence_digests",
        "organization_id", "store_id", "owner_id", "agent_id", "tenant_id", "tenancy",
        "scope_ids", "fields", "scope_verified", "field_visibility_verified",
        "observed_at", "expires_at", "max_pages", "max_rows", "page_size",
        "max_response_bytes", "timeout_seconds"))
    if type(value["version"]) is not int or value["version"] != 1:
        deny()
    op = value["operation"]
    if type(op) is not str or op not in SPECS or value["semantics"] != "verified_read":
        deny()
    for key in ("contract_id", "organization_id", "store_id", "owner_id", "agent_id", "tenant_id", "tenancy"):
        identifier(value[key])
    for key in ("corpus_digest", "candidate_digest", "request_schema_digest", "result_schema_digest"):
        c.fingerprint(value[key])
    c.strings(value["evidence_digests"], maximum=16)
    if not value["evidence_digests"]:
        deny()
    for value_sha in value["evidence_digests"]:
        c.fingerprint(value_sha)
    c.strings(value["scope_ids"], maximum=20, limit=128)
    if not value["scope_ids"]:
        deny()
    for item in value["scope_ids"]:
        identifier(item)
    spec = SPECS[op]
    allowed = set(spec["text"] + spec["number"] + spec["boolean"])
    c.strings(value["fields"], maximum=20, limit=64)
    if not value["fields"] or not set(value["fields"]) <= allowed:
        deny()
    if value["scope_verified"] is not True or value["field_visibility_verified"] is not True:
        deny()
    now = utcnow()
    observed, expiry = c.instant(value["observed_at"]), c.instant(value["expires_at"])
    if not observed <= now < expiry or (expiry - observed).total_seconds() > 86400:
        deny()
    for key, high in (("max_pages", 10), ("max_rows", 500), ("page_size", 100),
                      ("max_response_bytes", 65536), ("timeout_seconds", 30)):
        integer(value[key], 1, high)
    return value


def validate_binding(value, proposal, config, requester, agent):
    validate_manifest(value)
    spec = SPECS[value["operation"]]
    if ((value["organization_id"], value["store_id"], value["owner_id"], value["agent_id"]) !=
            (requester.organization_id, requester.store_id, requester.subject_id, agent.subject_id)
            or (value["organization_id"], value["store_id"], value["owner_id"]) !=
            (config.organization_id, config.store_id, config.owner_id)
            or value["corpus_digest"] != config.manifest_sha256):
        deny()
    if proposal.get("read_write_semantics") != "read" or proposal.get("candidate_id") != spec["candidate"]:
        deny()
    for key in ("corpus_digest", "candidate_digest", "request_schema_digest", "result_schema_digest"):
        if proposal.get(key) != value[key]:
            deny()
    if sorted(value["evidence_digests"]) != sorted(set(
            ref["content_sha256"] for ref in proposal["evidence"])):
        deny()
    return value


def _dates(parameters, start):
    a, b = parameters.get(start), parameters.get("endDate")
    try:
        if (type(a) is not str or type(b) is not str or len(a) != 10 or len(b) != 10
                or date.fromisoformat(a).isoformat() != a or date.fromisoformat(b).isoformat() != b):
            deny()
        delta = (date.fromisoformat(b) - date.fromisoformat(a)).days
        if not 0 <= delta <= 366:
            deny()
    except Exception:
        deny()


def request_body(operation, parameters, scope_ids, offset, page_size):
    """Exact business subset. Scope comes from Host, not G1/model parameters."""
    try:
        if operation not in SPECS or type(parameters) is not dict:
            deny()
        integer(offset, 0, 1000000)
        integer(page_size, 1, 100)
        c.strings(scope_ids, maximum=20, limit=128)
        if not scope_ids:
            deny()
        for scope in scope_ids:
            identifier(scope)
        params = c.decode(c.canonical(parameters), 8192)
        integer(params.get("skipCount"), 0, 1000000)
        integer(params.get("maxResultCount"), 1, 100)
        common = {"skipCount", "maxResultCount"}
        if operation == "sale_lines":
            allowed = common | {"startDate", "endDate", "billState", "businessType", "erpBillBusinessType",
                                "productCategoryIds", "productIds"}
            _dates(params, "startDate")
            for key, default, choices, cls in (
                ("billState", 12, (11, 12), int), ("businessType", "0", ("0", "2", "3"), str),
                ("erpBillBusinessType", 0, (0, 1), int)):
                current = params.setdefault(key, default)
                if type(current) is not cls or current not in choices:
                    deny()
        elif operation == "retail_lines":
            # Unverified billType/brand/name semantics are deliberately not admitted.
            allowed = common | {"beginDate", "endDate", "productId", "productCategoryIds"}
            _dates(params, "beginDate")
        elif operation == "stock_condition":
            allowed = common | {"statisticsTypeList", "showOnlyHasStock", "showVirtualStock",
                                "keyword", "productCategoryIds", "productIds"}
            if (params.get("statisticsTypeList") != [7] or
                    type(params["statisticsTypeList"][0]) is not int or
                    params.get("showOnlyHasStock") is not False or params.get("showVirtualStock") is not False):
                deny()
        else:
            allowed = common | {"keyword", "productCategoryId", "status"}
            status = params.setdefault("status", -1)
            if type(status) is not int or status not in (-1, 0, 1):
                deny()
        if not set(params) <= allowed:
            deny()
        for key in ("productIds", "productCategoryIds"):
            if key in params:
                c.strings(params[key], maximum=100, limit=128)
                for item in params[key]:
                    identifier(item)
        for key in ("productId", "productCategoryId"):
            if key in params:
                identifier(params[key])
        if "keyword" in params:
            c.text(params["keyword"], 100, empty=True)
            if any(x in params["keyword"].casefold() for x in ("://", "bearer ", "\r", "\n")):
                deny()
        params["skipCount"], params["maxResultCount"] = offset, page_size
        params[SPECS[operation]["scope_request"]] = list(scope_ids)
        return params
    except Exception:
        deny()


def _scalar(value, kind, missing=False):
    if missing:
        return {"presence": "missing"}
    if value is None:
        return {"presence": "null"}
    if kind == "number":
        if type(value) is _Number:
            return {"presence": "value", "wire_type": "number", "raw_value": value.token}
        # String decimals stay strings, and must really be finite decimal literals.
        if type(value) is str and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", value) and len(value) <= 128:
            from decimal import Decimal
            if Decimal(value).is_finite():
                return {"presence": "value", "wire_type": "string", "raw_value": value}
        deny()
    if kind == "boolean":
        if type(value) is not bool:
            deny()
        return {"presence": "value", "wire_type": "boolean", "value": value}
    if type(value) is _Number:
        # Numeric IDs retain original spelling; scope matching never coerces them.
        return {"presence": "value", "wire_type": "number", "raw_value": value.token}
    if type(value) is not str or len(value) > 256 or any(ord(ch) < 32 for ch in value):
        deny()
    return {"presence": "value", "wire_type": "string", "value": value}


def _scope(value):
    if type(value) is str:
        return identifier(value)
    if type(value) is _Number and re.fullmatch(r"(?:0|[1-9]\d*)", value.token):
        return identifier(value.token)
    deny()


def project(value, raw, offset, requested):
    """Private raw -> minimal lexical projection; never recursively copy a DTO."""
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= value["max_response_bytes"]:
            deny()
        payload = _decode(raw)
        if type(payload) is not dict or payload.get("success") is not True or payload.get("error") is not None:
            deny()
        result = payload.get("result")
        if type(result) is not dict or type(result.get("items")) is not list:
            deny()
        rows, total = result["items"], result.get("totalCount")
        if (type(total) is not _Number or not re.fullmatch(r"(?:0|[1-9]\d*)", total.token)
                or len(total.token) > 12 or len(rows) > requested or any(type(row) is not dict for row in rows)):
            deny()
        spec = SPECS[value["operation"]]
        projected = []
        for index, row in enumerate(rows):
            if _scope(row.get(spec["scope_row"])) not in value["scope_ids"]:
                deny()
            if value["operation"] == "sale_lines" and row.get("havePermissoin") is not True:
                deny()
            fields = {}
            for name in value["fields"]:
                kind = "number" if name in spec["number"] else "boolean" if name in spec["boolean"] else "text"
                fields[name] = _scalar(row.get(name), kind, name not in row)
            projected.append({"source_offset": offset + index, "fields": fields})
        # Fingerprint raw rows independent of total metadata. Never deduplicate
        # legitimate repeated rows or infer a line key from bill+product.
        def lexical(node):
            if type(node) is _Number:
                return {"$number_lexeme": node.token}
            if type(node) is dict:
                return {key: lexical(item) for key, item in node.items()}
            if type(node) is list:
                return [lexical(item) for item in node]
            return node
        return {"items": projected, "raw_item_count": len(rows), "total_count": int(total.token),
                "raw_rows_sha256": c.digest(lexical(rows)), "response_sha256": sha256(raw).hexdigest()}
    except Exception:
        deny()
