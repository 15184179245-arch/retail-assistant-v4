"""Pure bounded ERP posted-detail candidates; no IO, statistics or authority.

Source fields remain distinct from retail fields. In particular total is not
retail originalTotal, and bill_time is not evidence of the customer sale hour.
Local ordinals preserve multiplicity; they are not stable upstream line IDs.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

from .sales_ranking_contract import (
    MAX_BYTES, MAX_ROWS, MAX_PAGE_SIZE,
    _bounded_json, _object, _decimal, _integer,
)
from .retail_sales_detail_contract import _id, _text, _optional, _day, _timestamp, _bill_day, _json_int

MAX_STANDARDS = 128
_NUMBERS = ('qty', 'price', 'total', 'discount', 'discountPrice', 'discountTotal', 'giveawayQty')
_TYPES = ('billType', 'businessType', 'erpBillBusinessType', 'redWord')
_LABELS = ('billTypeName', 'businessTypeName', 'erpBillBusinessTypeName')


class ErpSalesDetailContractError(ValueError):
    """Fixed diagnostics never include source values."""


def _deny():
    raise ErpSalesDetailContractError('erp_sales_detail_candidate_rejected') from None


def _source_int(value):
    if type(value) is not int or len(str(value)) > 200:
        _deny()
    return value


def _number(value):
    if type(value) not in (int, Decimal):
        _deny()
    return _decimal(value)


@dataclass(frozen=True)
class ErpSalesDetailStandard:
    id: str
    standard_id: str
    standard_name: str
    is_basic_standard: bool
    standard_relation: Decimal


@dataclass(frozen=True)
class ErpSalesDetailRow:
    source_ordinal: int
    bill_id: str
    bill_code: str
    bill_date: str
    bill_time: str
    bill_time_has_timezone: bool
    bill_state: int
    bill_type: int
    business_type: int
    erp_bill_business_type: int
    red_word: int
    shop_id: str
    shop_code: str
    product_id: str
    product_code: str
    product_name: str
    standard_id: str
    standard_name: str
    is_basic_standard_id: bool
    is_giveaway: bool
    qty: Decimal
    price: Decimal
    total: Decimal
    discount: Decimal
    discount_price: Decimal
    discount_total: Decimal
    giveaway_qty: Decimal
    standards: tuple[ErpSalesDetailStandard, ...]
    bill_type_name: str | None
    business_type_name: str | None
    erp_bill_business_type_name: str | None


@dataclass(frozen=True)
class ErpSalesDetailPage:
    skip: int
    take: int
    total_count: int
    expected_shop_id: str
    day: str
    response_sha256: str
    rows: tuple[ErpSalesDetailRow, ...]


@dataclass(frozen=True)
class ErpSalesDetailCandidate:
    expected_shop_id: str
    day: str
    rows: tuple[ErpSalesDetailRow, ...]
    total_count: int
    page_count: int
    response_sha256s: tuple[str, ...]
    classification: str = 'source_erp_posted_detail_candidate'
    structurally_complete: bool = True
    business_semantics_verified: bool = False
    stable_source_line_identity_verified: bool = False


def _validate_standard(standard):
    if type(standard) is not ErpSalesDetailStandard:
        _deny()
    _id(standard.id)
    _id(standard.standard_id)
    _text(standard.standard_name, required=True)
    if type(standard.is_basic_standard) is not bool or type(standard.standard_relation) is not Decimal:
        _deny()
    _decimal(standard.standard_relation)


def _validate_row(row, page, ordinal):
    if type(row) is not ErpSalesDetailRow:
        _deny()
    _integer(row.source_ordinal, 0, MAX_ROWS)
    if row.source_ordinal != ordinal:
        _deny()
    for value in (row.bill_id, row.bill_code, row.shop_id, row.product_id, row.standard_id):
        _id(value)
    for value in (row.shop_code, row.product_code, row.product_name, row.standard_name):
        _text(value, required=True)
    if row.shop_id != page.expected_shop_id or type(row.bill_state) is not int or row.bill_state != 12:
        _deny()
    for value in (row.bill_type, row.business_type, row.erp_bill_business_type, row.red_word):
        _source_int(value)
    if _bill_day(row.bill_date) != _day(page.day):
        _deny()
    has_timezone = _timestamp(row.bill_time).utcoffset() is not None
    if type(row.bill_time_has_timezone) is not bool or row.bill_time_has_timezone != has_timezone:
        _deny()
    if type(row.is_basic_standard_id) is not bool or type(row.is_giveaway) is not bool:
        _deny()
    for value in (row.qty, row.price, row.total, row.discount, row.discount_price,
                  row.discount_total, row.giveaway_qty):
        if type(value) is not Decimal:
            _deny()
        _decimal(value)
    if type(row.standards) is not tuple or len(row.standards) > MAX_STANDARDS:
        _deny()
    for standard in row.standards:
        _validate_standard(standard)
    for value in (row.bill_type_name, row.business_type_name, row.erp_bill_business_type_name):
        _optional(value)


def _validate_page(page):
    if type(page) is not ErpSalesDetailPage:
        _deny()
    _integer(page.skip, 0, MAX_ROWS)
    _integer(page.take, 1, MAX_PAGE_SIZE)
    _integer(page.total_count, 0, MAX_ROWS)
    _id(page.expected_shop_id)
    _day(page.day)
    if (type(page.rows) is not tuple or page.skip > page.total_count or
            len(page.rows) != min(page.take, page.total_count-page.skip) or
            type(page.response_sha256) is not str or
            re.fullmatch(r'[0-9a-f]{64}', page.response_sha256) is None):
        _deny()
    for index, row in enumerate(page.rows):
        _validate_row(row, page, page.skip+index)


def parse_page(raw: bytes, *, expected_shop_id: str, day: str, skip: int,
               take: int) -> ErpSalesDetailPage:
    """Validate one posted, explicitly scoped daily page; discard bounded PII/extras."""
    try:
        _id(expected_shop_id)
        _day(day)
        _integer(skip, 0, MAX_ROWS)
        _integer(take, 1, MAX_PAGE_SIZE)
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
            _deny()
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                           parse_int=_json_int, parse_float=_decimal, parse_constant=lambda _: _deny())
        _bounded_json(value)
        allowed = {'result', 'success', 'error', 'targetUrl', 'unAuthorizedRequest', '__abp'}
        if (type(value) is not dict or not {'result', 'success'} <= value.keys() or
                value.keys()-allowed or value['success'] is not True or value.get('error') is not None or
                'unAuthorizedRequest' in value and value['unAuthorizedRequest'] is not False or
                '__abp' in value and value['__abp'] is not True):
            _deny()
        target = value.get('targetUrl')
        if target is not None and (type(target) is not str or len(target) > 2048 or
                                  any(ord(ch) < 32 or ord(ch) == 127 for ch in target)):
            _deny()
        result = value['result']
        if type(result) is not dict or set(result) != {'items', 'totalCount', 'reportSummaryStat'}:
            _deny()
        total = _integer(result['totalCount'], 0, MAX_ROWS)
        # The observed schema certifies an object only; no invented nested
        # summary shape or statistical authority is imposed on this metadata.
        if (type(result['reportSummaryStat']) is not dict or type(result['items']) is not list or
                skip > total or len(result['items']) != min(take, total-skip)):
            _deny()
        required = {'billId', 'billCode', 'billDate', 'billTime', 'billState', 'shopId', 'shopCode',
                    'productId', 'productCode', 'productName', 'standardId', 'standardName',
                    'isBasicStandardId', 'isGiveaway', 'standards', *_TYPES, *_NUMBERS}
        rows = []
        for index, item in enumerate(result['items']):
            if type(item) is not dict or not required <= item.keys():
                _deny()
            standards = item['standards']
            if type(standards) is not list or len(standards) > MAX_STANDARDS:
                _deny()
            projected = []
            for entry in standards:
                if type(entry) is not dict or not {'id', 'standardId', 'standardName', 'isBasicStandard', 'standardRelation'} <= entry.keys():
                    _deny()
                projected.append(ErpSalesDetailStandard(entry['id'], entry['standardId'],
                    entry['standardName'], entry['isBasicStandard'], _number(entry['standardRelation'])))
            rows.append(ErpSalesDetailRow(skip+index,
                *[item[key] for key in ('billId', 'billCode', 'billDate', 'billTime')],
                _timestamp(item['billTime']).utcoffset() is not None, item['billState'],
                *[item[key] for key in _TYPES],
                *[item[key] for key in ('shopId', 'shopCode', 'productId', 'productCode', 'productName',
                                       'standardId', 'standardName', 'isBasicStandardId', 'isGiveaway')],
                *[_number(item[key]) for key in _NUMBERS], tuple(projected),
                *[item.get(key) for key in _LABELS]))
        page = ErpSalesDetailPage(skip, take, total, expected_shop_id, day,
                                  sha256(raw).hexdigest(), tuple(rows))
        _validate_page(page)
        return page
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError, InvalidOperation):
        _deny()


def assemble_candidate(pages: tuple[ErpSalesDetailPage, ...]) -> ErpSalesDetailCandidate:
    """Revalidate contiguous pages; SHA syntax does not authenticate original bytes.

Host must separately retain/verify response bytes. No deduplication, bridge to
retail, aggregation, unit conversion, or atomic-snapshot guarantee is provided.
"""
    try:
        if type(pages) is not tuple or not pages or len(pages) > MAX_ROWS+1:
            _deny()
        first = pages[0]
        _validate_page(first)
        rows = []
        for index, page in enumerate(pages):
            _validate_page(page)
            if (page.skip != len(rows) or index and len(rows) >= first.total_count or
                    (page.expected_shop_id, page.day, page.take, page.total_count) !=
                    (first.expected_shop_id, first.day, first.take, first.total_count)):
                _deny()
            rows.extend(page.rows)
        if len(rows) != first.total_count:
            _deny()
        return ErpSalesDetailCandidate(first.expected_shop_id, first.day, tuple(rows),
            first.total_count, len(pages), tuple(page.response_sha256 for page in pages))
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError, InvalidOperation):
        _deny()
