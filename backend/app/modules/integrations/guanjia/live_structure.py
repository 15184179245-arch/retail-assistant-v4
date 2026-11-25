"""Bounded, redacted ERP wire structure inspection; no live semantic claims.

Only ``inspect_response`` returns a disclosure-safe structure. The private
decoder retains exact number spelling and Decimal values for lexical checks;
decoded data must never be logged or used as an observation contract.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 24
MAX_NODES = 20000
MAX_NUMBER = 256
MAX_NESTED_ROWS = 500
_ERROR = "guanjia_live_structure_invalid"
_OPERATIONS = ("live_prices", "live_stock")
_COMMON = frozenset({"productId", "shopId", "whsId", "unit", "unitId",
                     "source_updated_at", "source_version"})
_STOCK = _COMMON | {"qty", "quantity", "subUnitQty", "stockQty", "stockKind"}
_PRICE = _COMMON | {"price", "priceType", "currency", "taxBasis", "effectiveUntil",
                     "retailPrice", "retailPrice2", "retailPrice3", "retailPrice4",
                     "retailPrice5", "buyingPrice", "standardId"}
_RULE = frozenset({"price", "priceType", "currency", "taxBasis", "unit", "unitId",
                   "retailPrice", "buyingPrice"})


@dataclass(frozen=True, repr=False)
class _Number:
    token: str
    decimal: Decimal


def _decode(raw):
    def number(token):
        if len(token) > MAX_NUMBER:
            raise ValueError()
        value = Decimal(token)
        if not value.is_finite():
            raise ValueError()
        return _Number(token, value)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result or len(key) > 256:
                raise ValueError()
            key.encode("utf-8")
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    if type(raw) is not bytes or not raw or len(raw) > MAX_BYTES:
        raise ValueError()
    value = json.loads(raw.decode("utf-8"), parse_int=number, parse_float=number,
                       parse_constant=constant, object_pairs_hook=pairs)
    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise ValueError()
        if type(node) is dict:
            pending.extend((child, depth + 1) for child in node.values())
        elif type(node) is list:
            pending.extend((child, depth + 1) for child in node)
        elif type(node) is str:
            # Lone surrogates are legal to json.loads, but not UTF-8 text.
            node.encode("utf-8")
    return value


def _kind(value):
    return {_Number: "number", str: "string", bool: "boolean", type(None): "null",
            list: "array", dict: "object"}[type(value)]


def inspect_response(operation: str, raw: bytes) -> dict:
    """Return fixed field paths/types, row counts and raw response SHA only.

    Field labels are lexical candidates, never evidence of price, stock, store,
    currency or time semantics. Unknown keys and all scalar values are omitted.
    A single-product response may contain multiple standard/price-rule rows.
    Failures always raise one fixed code without the upstream exception chain.
    """
    try:
        if type(operation) is not str or operation not in _OPERATIONS:
            raise ValueError()
        payload = _decode(raw)
        if type(payload) is not dict or payload.get("success") is not True:
            raise ValueError()
        error = payload.get("error")
        # Only absent/null mean no error; empty values have no verified meaning.
        if error is not None:
            raise ValueError()
        rows = payload.get("result")
        prefix = "result[]"
        if type(rows) is dict:
            if "items" not in rows:
                raise ValueError()
            rows = rows["items"]
            prefix = "result.items[]"
        if type(rows) is not list or len(rows) > 1 or any(type(row) is not dict for row in rows):
            raise ValueError()
        fields = {}
        nested_counts = {}

        def mark(path, value):
            fields.setdefault(path, set()).add(_kind(value))

        def scalars(row, path, allowed):
            for key in sorted(allowed & row.keys()):
                value = row[key]
                if type(value) in (list, dict):
                    raise ValueError()
                mark(path + "." + key, value)

        for row in rows:
            scalars(row, prefix, _STOCK if operation == "live_stock" else _PRICE)
            if operation == "live_prices" and "standardPriceItems" in row:
                standards = row["standardPriceItems"]
                path = prefix + ".standardPriceItems"
                if type(standards) is not list or len(standards) > MAX_NESTED_ROWS:
                    raise ValueError()
                mark(path, standards)
                nested_counts[path] = len(standards)
                for standard in standards:
                    if type(standard) is not dict:
                        raise ValueError()
                    scalars(standard, path + "[]", _PRICE)
                    if "priceRule" in standard:
                        rule = standard["priceRule"]
                        rule_path = path + "[].priceRule"
                        if type(rule) is not dict:
                            raise ValueError()
                        mark(rule_path, rule)
                        scalars(rule, rule_path, _RULE)
            if not fields:
                raise ValueError()
        return {"version": "1", "operation": operation, "row_count": len(rows),
                "field_types": {path: sorted(kinds) for path, kinds in sorted(fields.items())},
                "nested_row_counts": nested_counts, "response_sha256": sha256(raw).hexdigest(),
                "source_status": "structure_captured_semantics_unknown" if rows else "empty_unclassified",
                "semantics_known": False, "disclosure_ready": False,
                "same_upstream_snapshot": False}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, InvalidOperation):
        raise ValueError(_ERROR) from None
