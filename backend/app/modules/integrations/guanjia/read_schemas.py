"""ERP-009 Host-only observation contract, not an inferred live ERP wire schema.

Only a synthetic-v1 source adapter is accepted initially. Real wire semantics,
including store attribution of live_prices, remain a separate HOLD_REAL_READ.
Decimal strings retain their original representation; floats are not exact data.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

Identifier = Annotated[StrictStr, Field(min_length=1, max_length=120)]
Operation = Literal['live_prices', 'live_stock']
EmptyReason = Literal['source_not_found', 'index_miss', 'permission_filtered', 'upstream_unavailable']
Status = Literal['usable', 'stale', 'missing_value', 'missing_metadata', 'source_not_found',
                 'index_miss', 'permission_filtered', 'upstream_unavailable', 'empty_unclassified',
                 'invalid_response', 'rate_limited', 'timeout', 'mapping_unconfirmed', 'hold_real_read']


def aware_instant(value: str) -> datetime:
    try:
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('timezone-aware source instant required') from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError('timezone-aware source instant required')
    return instant


class StrictContract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, revalidate_instances='always')


class ObservationInput(StrictContract):
    version: Literal['1']
    operation: Operation
    product_id: Identifier


class PriceInput(ObservationInput):
    operation: Literal['live_prices']


class StockInput(ObservationInput):
    operation: Literal['live_stock']


class SourceRow(StrictContract):
    """Normalized synthetic contract. Do not feed arbitrary live JSON into it."""
    upstream_product_id: Identifier
    shop_id: Identifier
    warehouse_id: Identifier
    raw_value: StrictStr | StrictInt | None
    unit: Identifier | None
    price_type: Identifier | None
    currency: Annotated[StrictStr, Field(pattern=r'^[A-Z]{3}$')] | None
    tax_basis: Identifier | None
    stock_kind: Identifier | None
    source_updated_at: StrictStr | None
    effective_until: StrictStr | None

    @field_validator('raw_value')
    @classmethod
    def exact_value(cls, value):
        if isinstance(value, str):
            if not value or value.strip() != value or len(value) > 100:
                raise ValueError('invalid decimal representation')
            try:
                number = Decimal(value)
            except InvalidOperation:
                raise ValueError('invalid decimal representation') from None
            if not number.is_finite():
                raise ValueError('nonfinite value')
        return value

    @field_validator('source_updated_at', 'effective_until')
    @classmethod
    def source_instant(cls, value):
        if value is not None:
            aware_instant(value)
        return value


class SourceResponse(StrictContract):
    version: Literal['1']
    operation: Operation
    source_version: Identifier
    rows: Annotated[list[SourceRow], Field(max_length=1)]
    complete: bool
    empty_reason: EmptyReason | None
    reason_evidence_sha256: Annotated[StrictStr, Field(pattern=r'^[0-9a-f]{64}$')] | None


class ObservationOutput(StrictContract):
    version: Literal['1'] = '1'
    operation: Operation
    product_id: Identifier
    organization_id: Identifier
    store_id: Identifier
    tenant_fingerprint: Identifier
    shop_id: Identifier
    warehouse_id: Identifier
    source_contract: Literal['synthetic-v1', 'unverified-live']
    status: Status
    usable: bool
    source_version: Identifier | None
    raw_value: StrictStr | StrictInt | None
    unit: Identifier | None
    price_type: Identifier | None
    currency: Annotated[StrictStr, Field(pattern=r'^[A-Z]{3}$')] | None
    tax_basis: Identifier | None
    stock_kind: Identifier | None
    source_updated_at: StrictStr | None
    effective_until: StrictStr | None
    observed_started_at: StrictStr
    observed_finished_at: StrictStr
    same_upstream_snapshot: Literal[False] = False
    # 'usable' describes source field quality, not a durable/public receipt.
    # The transaction owner must commit successfully before any later adapter
    # can consider disclosure. This card does not implement that public adapter.
    audit_state: Literal['staged_uncommitted'] = 'staged_uncommitted'
    disclosure_ready: Literal[False] = False

    @field_validator('observed_started_at', 'observed_finished_at', 'source_updated_at', 'effective_until')
    @classmethod
    def instant(cls, value):
        if value is not None:
            aware_instant(value)
        return value


# Separate wire-structure contract: never feed these values to SourceResponse.
from dataclasses import dataclass
import json as _json


@dataclass(frozen=True, repr=False)
class JsonNumberLexeme:
    """An exact JSON number token, not a float or a business quantity."""
    token: str


def decode_structure_json(raw: bytes):
    """Bounded, duplicate-free JSON preserving number type and spelling."""
    def number(token):
        if len(token) > 1024:
            raise ValueError('structure_json_invalid')
        return JsonNumberLexeme(token)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result or len(key) > 256:
                raise ValueError('structure_json_invalid')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('structure_json_invalid')

    def bounded(value, depth=0):
        if depth > 32:
            raise ValueError('structure_json_invalid')
        if type(value) is dict:
            for child in value.values():
                bounded(child, depth + 1)
        elif type(value) is list:
            for child in value:
                bounded(child, depth + 1)

    try:
        if type(raw) is not bytes or len(raw) > 1024 * 1024:
            raise ValueError()
        value = _json.loads(raw.decode('utf-8'), parse_int=number, parse_float=number,
                            parse_constant=constant, object_pairs_hook=pairs)
        bounded(value)
        return value
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise ValueError('structure_json_invalid') from None


# These are lexical field labels only, not evidence of the real tenant schema.
_STRUCTURE_KEYS = ('productId', 'shopId', 'whsId', 'quantity', 'unit',
                   'source_updated_at', 'source_version')


def project_structure(value):
    """Only fixed paths/types and provable row count escape this boundary."""
    if type(value) is not dict or value.get('success') is not True:
        raise ValueError('structure_response_rejected')
    result = value.get('result')
    prefix = 'result[]'
    if type(result) is dict:
        result = result.get('items')
        prefix = 'result.items[]'
    if type(result) is not list or len(result) > 1 or any(type(row) is not dict for row in result):
        raise ValueError('structure_result_invalid')
    fields = {}
    for row in result:
        for key in _STRUCTURE_KEYS:
            if key not in row:
                continue
            item = row[key]
            kind = ('number' if type(item) is JsonNumberLexeme else
                    'null' if item is None else 'boolean' if type(item) is bool else
                    'string' if type(item) is str else 'array' if type(item) is list else 'object')
            fields[prefix + '.' + key] = kind
    return {'row_count': len(result), 'field_types': fields,
            'source_status': 'structure_captured_semantics_unknown' if result else 'empty_unclassified',
            'semantics_known': False, 'disclosure_ready': False}
