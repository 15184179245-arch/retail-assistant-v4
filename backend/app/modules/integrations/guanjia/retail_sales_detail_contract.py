"""Bounded, pure daily retail detail candidates; no IO or business authority.

Ordinals identify positions in supplied pages, never stable upstream lines.
Signed values, repeated rows and timestamp spellings remain source data.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

from .sales_ranking_contract import (
    MAX_BYTES, MAX_ROWS, MAX_PAGE_SIZE, RankingContractError,
    _bounded_json, _object, _decimal, _integer, _identity, _display_identity,
)


class RetailSalesDetailContractError(ValueError):
    """Fixed diagnostics exclude source values."""


def _deny():
    raise RetailSalesDetailContractError('retail_sales_detail_candidate_rejected') from None


def _text(value, *, required=False):
    _display_identity(value, required=required)
    if required and not value.strip():
        _deny()
    return value


def _id(value):
    _identity(value)
    return _text(value, required=True)


def _optional(value):
    return None if value is None else _text(value)


def _day(value):
    _text(value, required=True)
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value) is None:
        _deny()
    return date.fromisoformat(value)


def _timestamp(value):
    _text(value, required=True)
    # Accept explicit ISO date-time only, not Python's compact/week-date forms.
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}'
                    r'(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?', value) is None:
        _deny()
    # datetime permits offsets with overflowing minutes; reject those lexically.
    if re.search(r'[+-][0-9]{2}:[0-9]{2}$', value):
        if int(value[-5:-3]) > 23 or int(value[-2:]) > 59:
            _deny()
    return datetime.fromisoformat(value)


def _bill_day(value):
    return _day(value) if type(value) is str and len(value) == 10 else _timestamp(value).date()


def _number(value):
    if type(value) not in (int, Decimal):
        _deny()
    return _decimal(value)


def _json_int(value):
    if len(value) > 200:
        _deny()
    return int(value)


@dataclass(frozen=True)
class RetailSalesDetailRow:
    source_ordinal: int
    product_id: str
    product_code: str
    product_name: str
    standard_name: str
    bill_id: str
    bill_number: str
    bill_type: int
    date: str
    creation_time: str
    store_code: str
    store_name: str | None
    source_store_id: str | None
    spec: str | None
    aid_qty_display: str | None
    qty: Decimal
    original_total: Decimal
    final_total: Decimal
    discount_total: Decimal


@dataclass(frozen=True)
class RetailSalesDetailPage:
    skip: int
    take: int
    total_count: int
    expected_store_id: str
    expected_store_code: str
    day: str
    response_sha256: str
    rows: tuple[RetailSalesDetailRow, ...]


@dataclass(frozen=True)
class RetailSalesDetailCandidate:
    expected_store_id: str
    expected_store_code: str
    day: str
    rows: tuple[RetailSalesDetailRow, ...]
    total_count: int
    page_count: int
    response_sha256s: tuple[str, ...]
    classification: str = 'source_retail_detail_candidate'
    structurally_complete: bool = True
    business_semantics_verified: bool = False
    stable_source_line_identity_verified: bool = False


def _validate_row(row, page, ordinal):
    if type(row) is not RetailSalesDetailRow:
        _deny()
    _integer(row.source_ordinal, 0, MAX_ROWS)
    if row.source_ordinal != ordinal:
        _deny()
    for value in (row.product_id, row.bill_id, row.bill_number):
        _id(value)
    for value in (row.product_code, row.product_name, row.standard_name, row.store_code):
        _text(value, required=True)
    _optional(row.store_name)
    _optional(row.spec)
    _optional(row.aid_qty_display)
    if row.source_store_id is not None and _id(row.source_store_id) != page.expected_store_id:
        _deny()
    if row.store_code != page.expected_store_code or type(row.bill_type) is not int:
        _deny()
    if len(str(row.bill_type)) > 200 or _bill_day(row.date) != _day(page.day):
        _deny()
    _timestamp(row.creation_time)
    for value in (row.qty, row.original_total, row.final_total, row.discount_total):
        if type(value) is not Decimal:
            _deny()
        _decimal(value)


def _validate_page(page):
    if type(page) is not RetailSalesDetailPage:
        _deny()
    _integer(page.skip, 0, MAX_ROWS)
    _integer(page.take, 1, MAX_PAGE_SIZE)
    _integer(page.total_count, 0, MAX_ROWS)
    _id(page.expected_store_id)
    _text(page.expected_store_code, required=True)
    _day(page.day)
    if (type(page.response_sha256) is not str or
            re.fullmatch(r'[0-9a-f]{64}', page.response_sha256) is None or
            type(page.rows) is not tuple or page.skip > page.total_count or
            len(page.rows) != min(page.take, page.total_count - page.skip)):
        _deny()
    for index, row in enumerate(page.rows):
        _validate_row(row, page, page.skip + index)


def parse_page(raw: bytes, *, expected_store_id: str, expected_store_code: str,
               day: str, skip: int, take: int) -> RetailSalesDetailPage:
    """Parse one successful page; ignore bounded extras, not missing source facts."""
    try:
        _id(expected_store_id)
        _text(expected_store_code, required=True)
        _day(day)
        _integer(skip, 0, MAX_ROWS)
        _integer(take, 1, MAX_PAGE_SIZE)
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
            _deny()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                           parse_int=_json_int, parse_float=_decimal,
                           parse_constant=lambda _: _deny())
        _bounded_json(value)
        allowed = {'success', 'result', 'error', 'targetUrl', 'unAuthorizedRequest', '__abp'}
        if (type(value) is not dict or not {'success', 'result'} <= value.keys() or
                value.keys() - allowed or value['success'] is not True or
                value.get('error') is not None or
                'unAuthorizedRequest' in value and value['unAuthorizedRequest'] is not False or
                '__abp' in value and value['__abp'] is not True):
            _deny()
        target = value.get('targetUrl')
        if target is not None and (type(target) is not str or len(target) > 2048 or
                                  any(ord(c) < 32 or ord(c) == 127 for c in target)):
            _deny()
        result = value['result']
        if type(result) is not dict or set(result) != {'items', 'totalCount', 'pageTotal', 'sum', 'columns'}:
            _deny()
        total = _integer(result['totalCount'], 0, MAX_ROWS)
        _integer(result['pageTotal'], 0, MAX_ROWS)
        if (type(result['sum']) is not dict or
                result['columns'] is not None and type(result['columns']) is not list or
                type(result['items']) is not list or skip > total or
                len(result['items']) != min(take, total - skip)):
            _deny()
        required = {'productId', 'productCode', 'productName', 'standardName', 'billId',
                    'billNumber', 'billType', 'date', 'creationTime', 'storeCode',
                    'spec', 'aidQty', 'qty', 'originalTotal', 'finalTotal', 'discountTotal'}
        rows = []
        for index, item in enumerate(result['items']):
            if type(item) is not dict or not required <= item.keys():
                _deny()
            if 'storeId' in item:
                _id(item['storeId'])
            rows.append(RetailSalesDetailRow(
                skip + index, *[item[k] for k in ('productId', 'productCode', 'productName',
                    'standardName', 'billId', 'billNumber', 'billType', 'date', 'creationTime',
                    'storeCode')], item.get('storeName'), item.get('storeId'), item['spec'],
                item['aidQty'], *[_number(item[k]) for k in
                                ('qty', 'originalTotal', 'finalTotal', 'discountTotal')]))
        page = RetailSalesDetailPage(skip, take, total, expected_store_id, expected_store_code,
                                     day, sha256(raw).hexdigest(), tuple(rows))
        _validate_page(page)
        return page
    except (RankingContractError, ValueError, UnicodeError, RecursionError, InvalidOperation,
            TypeError, AttributeError, OverflowError):
        _deny()


def assemble_candidate(pages: tuple[RetailSalesDetailPage, ...]) -> RetailSalesDetailCandidate:
    """Revalidate contiguous one-day pages without deduplicating source rows.

SHA syntax and page consistency do not authenticate bytes or prove a stable
upstream snapshot. Host must retain raw responses and verify their hashes.
"""
    try:
        if type(pages) is not tuple or not pages or len(pages) > MAX_ROWS + 1:
            _deny()
        rows = []
        first = pages[0]
        _validate_page(first)
        for index, page in enumerate(pages):
            _validate_page(page)
            if (page.skip != len(rows) or index and len(rows) >= first.total_count or
                    (page.expected_store_id, page.expected_store_code, page.day, page.take, page.total_count) !=
                    (first.expected_store_id, first.expected_store_code, first.day, first.take, first.total_count)):
                _deny()
            rows.extend(page.rows)
        if len(rows) != first.total_count:
            _deny()
        return RetailSalesDetailCandidate(first.expected_store_id, first.expected_store_code,
            first.day, tuple(rows), first.total_count, len(pages), tuple(p.response_sha256 for p in pages))
    except (RankingContractError, ValueError, InvalidOperation, TypeError, AttributeError, OverflowError):
        _deny()
