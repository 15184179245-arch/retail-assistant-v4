"""Bounded pure retail-summary DTO parser, without business authority.

Preserves signed source numbers and source order. No arithmetic, unit conversion,
Top20, source IO or authorization takes place here.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json

from .sales_ranking_contract import (
    MAX_BYTES, MAX_ROWS, MAX_PAGE_SIZE, RankingContractError,
    _bounded_json, _object, _decimal, _identity, _display_identity, _integer,
)


class RetailSalesContractError(ValueError):
    """Diagnostics deliberately exclude source values."""


def _deny():
    raise RetailSalesContractError('retail_sales_candidate_rejected') from None


def _number(value):
    if type(value) not in (int, Decimal):
        _deny()
    return _decimal(value)


def _json_integer(text):
    if len(text) > 200:
        _deny()
    return int(text)


def _label(value, *, required=False):
    _display_identity(value, required=required)
    if required and not value.strip():
        _deny()
    return value


@dataclass(frozen=True)
class RetailSalesRow:
    product_id: str
    store_id: str
    product_name: str
    code: str
    standard_name: str
    qty_total: Decimal
    original_total: Decimal
    discount_total: Decimal
    final_total: Decimal
    back_qty_total: Decimal
    back_final_total: Decimal


@dataclass(frozen=True)
class RetailSalesPage:
    skip: int
    take: int
    total_count: int
    store_id: str
    rows: tuple[RetailSalesRow, ...]


@dataclass(frozen=True)
class RetailSalesCandidate:
    store_id: str
    rows: tuple[RetailSalesRow, ...]
    total_count: int
    page_count: int
    classification: str = 'source_retail_report_candidate'
    structurally_complete: bool = True
    business_semantics_verified: bool = False


def _validate_row(row, store):
    if type(row) is not RetailSalesRow or type(row.store_id) is not str or row.store_id != store:
        _deny()
    _identity(row.product_id)
    _identity(row.store_id)
    _label(row.product_id, required=True)
    _label(row.store_id, required=True)
    _label(row.product_name, required=True)
    _label(row.code)
    _label(row.standard_name, required=True)
    for value in (row.qty_total, row.original_total, row.discount_total,
                  row.final_total, row.back_qty_total, row.back_final_total):
        if type(value) is not Decimal:
            _deny()
        _decimal(value)


def parse_page(raw: bytes, *, expected_store: str, skip: int, take: int) -> RetailSalesPage:
    """Read successful ABP DTO; extras/metadata are bounded then discarded.

pageTotal is never used as a page count; sum is never statistical authority.
JSON numbers and backFinalTotal's observed string wire type remain distinct.
"""
    try:
        _identity(expected_store)
        _label(expected_store, required=True)
        _integer(skip, 0, MAX_ROWS)
        _integer(take, 1, MAX_PAGE_SIZE)
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
            _deny()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                           parse_int=_json_integer, parse_float=_decimal,
                           parse_constant=lambda _: _deny())
        _bounded_json(value)
        envelope_fields = {'success', 'result', 'error', 'targetUrl', 'unAuthorizedRequest', '__abp'}
        if (type(value) is not dict or not {'success', 'result'} <= value.keys()
                or value.keys() - envelope_fields or value['success'] is not True
                or value.get('error') is not None):
            _deny()
        target = value.get('targetUrl')
        if target is not None and (type(target) is not str or len(target) > 2048
                                  or any(ord(c) < 32 or ord(c) == 127 for c in target)):
            _deny()
        if ('unAuthorizedRequest' in value and value['unAuthorizedRequest'] is not False
                or '__abp' in value and value['__abp'] is not True):
            _deny()
        result = value['result']
        if type(result) is not dict or set(result) != {'items', 'totalCount', 'pageTotal', 'sum', 'columns'}:
            _deny()
        total = _integer(result['totalCount'], 0, MAX_ROWS)
        _integer(result['pageTotal'], 0, MAX_ROWS)
        if type(result['sum']) is not dict or result['columns'] is not None and type(result['columns']) is not list:
            _deny()
        items = result['items']
        if type(items) is not list or skip > total or len(items) != min(take, total - skip):
            _deny()
        rows, seen = [], set()
        required = {'productId', 'storeId', 'productName', 'code', 'standardName',
                    'qtyTotal', 'originalTotal', 'discountTotal', 'finalTotal',
                    'backQtyTotal', 'backFinalTotal'}
        for item in items:
            if type(item) is not dict or not required <= item.keys() or type(item['backFinalTotal']) is not str:
                _deny()
            row = RetailSalesRow(
                _identity(item['productId']), _identity(item['storeId']),
                _label(item['productName'], required=True), _label(item['code']),
                _label(item['standardName'], required=True),
                *(_number(item[k]) for k in ('qtyTotal', 'originalTotal', 'discountTotal',
                                            'finalTotal', 'backQtyTotal')),
                _decimal(item['backFinalTotal']),
            )
            _validate_row(row, expected_store)
            if row.product_id in seen:
                _deny()
            seen.add(row.product_id)
            rows.append(row)
        return RetailSalesPage(skip, take, total, expected_store, tuple(rows))
    except (RankingContractError, ValueError, UnicodeError, RecursionError, InvalidOperation, TypeError, AttributeError):
        _deny()


def assemble_candidate(pages: tuple[RetailSalesPage, ...]) -> RetailSalesCandidate:
    """Revalidate constructed pages; require complete contiguous same-store data.

Preserves all rows without sorting, aggregation or repeated return subtraction.
Does not prove upstream snapshot stability, time/unit or business semantics.
"""
    try:
        if type(pages) is not tuple or not pages or len(pages) > MAX_ROWS + 1:
            _deny()
        first = pages[0]
        if type(first) is not RetailSalesPage:
            _deny()
        rows, seen = [], set()
        for index, page in enumerate(pages):
            if type(page) is not RetailSalesPage:
                _deny()
            _integer(page.skip, 0, MAX_ROWS)
            _integer(page.take, 1, MAX_PAGE_SIZE)
            _integer(page.total_count, 0, MAX_ROWS)
            _identity(page.store_id)
            _label(page.store_id, required=True)
            if (page.skip != len(rows) or page.skip > page.total_count
                    or page.total_count != first.total_count or page.take != first.take
                    or page.store_id != first.store_id or type(page.rows) is not tuple
                    or len(page.rows) != min(page.take, page.total_count - page.skip)
                    or index and len(rows) >= first.total_count):
                _deny()
            for row in page.rows:
                _validate_row(row, page.store_id)
                if row.product_id in seen:
                    _deny()
                seen.add(row.product_id)
                rows.append(row)
        if len(rows) != first.total_count:
            _deny()
        return RetailSalesCandidate(first.store_id, tuple(rows), first.total_count, len(pages))
    except (RankingContractError, ValueError, InvalidOperation, TypeError, AttributeError):
        _deny()
