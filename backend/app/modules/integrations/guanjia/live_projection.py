"""Exact, bounded vendor field observations for the trusted Host.

This parser does not authorize, fetch, persist or disclose anything. The Host
must bind the source and persist an authorized receipt before returning these
values to a user or model. Observations are not quotations or stock promises.
Unknown source metadata stays unknown; observation time is never source time.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import re

from .live_structure import _decode, _Number, MAX_NESTED_ROWS

ERROR = 'guanjia_live_projection_invalid'
CONTRACT = 'guanjia-source-fields-v1'
RETAIL_FIELDS = ('retailPrice', 'retailPrice2', 'retailPrice3', 'retailPrice4', 'retailPrice5')
_MISSING = object()
_DECIMAL = re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z', re.ASCII)


@dataclass(frozen=True, repr=False)
class ProjectionBinding:
    # These are separate upstream namespaces, not interchangeable UUIDs.
    product_id: str
    product_source_store_id: str
    shop_id: str
    warehouse_id: str
    product_response_sha256: str


def _text(value, *, maximum=200, empty=False):
    if (type(value) is not str or len(value) > maximum or (not value and not empty)
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        raise ValueError()
    value.encode('utf-8')
    return value


def _instant(value):
    if (type(value) is not datetime or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError()
    return value.astimezone(timezone.utc).isoformat()


def _envelope(raw):
    data = _decode(raw)
    if (type(data) is not dict or data.get('success') is not True
            or data.get('error') is not None or 'result' not in data
            or ('unAuthorizedRequest' in data and data['unAuthorizedRequest'] is not False)
            or ('__abp' in data and data['__abp'] is not True)):
        raise ValueError()
    # targetUrl and other unselected metadata are inert and never returned.
    return data['result']


def _number(value):
    if type(value) is _Number:
        token, kind = value.token, 'number'
    elif type(value) is str:
        token, kind = value, 'string'
    else:
        raise ValueError()
    if not 0 < len(token) <= 100 or _DECIMAL.fullmatch(token) is None:
        raise ValueError()
    number = Decimal(token)
    # Bounded magnitude while retaining exact spelling (including signed zero).
    if not number.is_finite() or abs(number.adjusted()) > 100:
        raise ValueError()
    return token, kind, number


def _field(row, key):
    value = row.get(key, _MISSING)
    if value is _MISSING:
        return {'field': key, 'presence': 'missing', 'raw_value': None, 'wire_type': None}
    if value is None:
        return {'field': key, 'presence': 'null', 'raw_value': None, 'wire_type': 'null'}
    token, kind, _ = _number(value)
    return {'field': key, 'presence': 'value', 'raw_value': token, 'wire_type': kind}


def _product(raw, binding):
    if sha256(raw).hexdigest() != binding.product_response_sha256:
        raise ValueError()
    item = _envelope(raw)
    if (type(item) is not dict or item.get('id') != binding.product_id
            or item.get('storeId') != binding.product_source_store_id
            or item.get('isActive') is not True or item.get('isOwn') is not True):
        raise ValueError()
    label = {'product_id': binding.product_id, 'code': _text(item.get('code')),
             'name': _text(item.get('name')), 'source_store_id': binding.product_source_store_id,
             'identity_basis': 'source_id_and_pinned_detail', 'employee_confirmation': False}
    standards = item.get('baseProductStandardList')
    if type(standards) is not list or not 1 <= len(standards) <= MAX_NESTED_ROWS:
        raise ValueError()
    result, seen = {}, set()
    for standard in standards:
        if type(standard) is not dict:
            raise ValueError()
        sid = _text(standard.get('id'), maximum=120)
        if sid in seen or standard.get('productId') != binding.product_id or type(standard.get('isActive')) is not bool:
            raise ValueError()
        seen.add(sid)
        name = _text(standard.get('standardName'))
        relation, _, number = _number(standard.get('standardRelation'))
        if number <= 0:
            raise ValueError()
        if standard['isActive']:
            result[sid] = {'standard_id': sid, 'standard_name': name, 'relation': relation}
    if not result:
        raise ValueError()
    subunit = None if item.get('subUnitName') is None else (_text(item['subUnitName'], empty=True) or None)
    return label, result, subunit


def project_observation(operation, raw, *, product_raw, binding,
                        observed_started_at, observed_finished_at):
    """Project observed retail/quantity fields only, preserving exact values.

Empty means unknown, not zero. Price shop attribution is request-bound; stock
requires matching returned shop/warehouse IDs. Detail storeId is independently
bound and NEVER compared or equated with a shopId. No price/unit conversions.
Failures deliberately expose neither raw payloads nor upstream exception text.
"""
    try:
        if type(binding) is not ProjectionBinding or operation not in ('live_prices', 'live_stock'):
            raise ValueError()
        for value in (binding.product_id, binding.product_source_store_id, binding.shop_id, binding.warehouse_id):
            _text(value, maximum=120)
        if (type(binding.product_response_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', binding.product_response_sha256) is None):
            raise ValueError()
        start, end = _instant(observed_started_at), _instant(observed_finished_at)
        if observed_finished_at < observed_started_at:
            raise ValueError()
        label, standards, subunit = _product(product_raw, binding)
        rows = _envelope(raw)
        if type(rows) is not list or len(rows) > 1 or any(type(row) is not dict for row in rows):
            raise ValueError()
        result = {'contract': CONTRACT, 'operation': operation, 'product': label,
            'product_response_sha256': binding.product_response_sha256,
            'response_sha256': sha256(raw).hexdigest(),
            'requested_shop_id': binding.shop_id,
            'requested_warehouse_id': binding.warehouse_id if operation == 'live_stock' else None,
            'observed_started_at': start, 'observed_finished_at': end,
            'source_updated_at': None, 'currency': None, 'tax_basis': None,
            'source_metadata_status': 'not_verified',
            'same_upstream_snapshot': False, 'authority': 'source_field_observation_only',
            'status': 'observed_fields' if rows else 'empty_unclassified',
            'scope_evidence': 'request_bound', 'price_standards': [], 'stock_fields': [],
            'missing_price_standard_ids': sorted(standards) if operation == 'live_prices' else [],
            'stock_quantity_unit': None, 'stock_subunit_name': None}
        if not rows:
            return result
        row = rows[0]
        if row.get('productId') != binding.product_id:
            raise ValueError()
        if operation == 'live_stock':
            if row.get('shopId') != binding.shop_id or row.get('whsId') != binding.warehouse_id:
                raise ValueError()
            result['scope_evidence'] = 'response_product_shop_warehouse_matched'
            result['stock_fields'] = [_field(row, key) for key in ('qty', 'subUnitQty')]
            # No implicit qty->basic unit assumption; subunit has a separate,
            # explicitly echoed name, checked against the pinned detail.
            if 'subUnitName' in row:
                actual = None if row['subUnitName'] is None else (_text(row['subUnitName'], empty=True) or None)
                if actual != subunit:
                    raise ValueError()
                result['stock_subunit_name'] = actual or None
        else:
            if 'shopId' in row and row['shopId'] != binding.shop_id:
                raise ValueError()
            items = row.get('standardPriceItems')
            if type(items) is not list or len(items) > MAX_NESTED_ROWS:
                raise ValueError()
            seen, basic_count = set(), 0
            for item in items:
                if type(item) is not dict:
                    raise ValueError()
                sid = _text(item.get('standardId'), maximum=120)
                if sid not in standards or sid in seen:
                    raise ValueError()
                seen.add(sid)
                standard = standards[sid]
                relation, _, numeric = _number(item.get('standardRelation'))
                basic = item.get('isBasicStandard')
                if (item.get('standardName') != standard['standard_name']
                        or numeric != Decimal(standard['relation']) or type(basic) is not bool
                        or (basic and numeric != 1) or type(item.get('priceRule')) is not dict):
                    raise ValueError()
                basic_count += int(basic)
                if basic_count > 1:
                    raise ValueError()
                result['price_standards'].append(dict(standard, observed_relation=relation,
                    source_is_basic_standard=basic, fields=[_field(item['priceRule'], key) for key in RETAIL_FIELDS]))
            result['price_standards'].sort(key=lambda x: x['standard_id'])
            result['missing_price_standard_ids'] = sorted(set(standards) - seen)
        return result
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, ArithmeticError):
        raise ValueError(ERROR) from None
