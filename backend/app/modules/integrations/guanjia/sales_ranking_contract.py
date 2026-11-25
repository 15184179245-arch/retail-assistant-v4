"""Pure, bounded ranking-page candidate parser; no business authority.

Structural completeness does not establish date, net-sales, unit or snapshot
semantics. Never use this receipt as an accurate Top20 or authorization.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import re

MAX_BYTES = 2_000_000
MAX_ROWS = 100_000
MAX_PAGE_SIZE = 500
MAX_DEPTH = 16
MAX_NODES = 100_000
MAX_STRING = 16_384
MAX_CONTAINER = 10_000


class RankingContractError(ValueError):
    """Deliberately excludes upstream values from diagnostic output."""


def _deny():
    raise RankingContractError('sales_ranking_candidate_rejected') from None


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _deny()
    return value


def _identity(value):
    if type(value) is not str or not 1 <= len(value) <= 200 or value.strip() != value or any(ord(c) < 32 for c in value):
        _deny()
    return value


def _decimal(value):
    if type(value) not in (int, str, Decimal) or isinstance(value, bool):
        _deny()
    if type(value) is str and (len(value) > 200 or re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value) is None):
        _deny()
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        _deny()
    if not result.is_finite() or len(result.as_tuple().digits) > 200 or abs(result.as_tuple().exponent) > 1000:
        _deny()
    return result


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _deny()
        result[key] = value
    return result


def _bounded_json(root):
    """Validate even ignored metadata; never coerce it to business fields."""
    pending = [(root, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            _deny()
        if type(value) is dict:
            if len(value) > MAX_CONTAINER:
                _deny()
            for key, child in value.items():
                if (type(key) is not str or not 1 <= len(key) <= 200
                        or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in key)):
                    _deny()
                pending.append((child, depth + 1))
        elif type(value) is list:
            if len(value) > MAX_CONTAINER:
                _deny()
            pending.extend((child, depth + 1) for child in value)
        elif type(value) is str:
            if len(value) > MAX_STRING or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                _deny()
        elif type(value) in (int, Decimal):
            _decimal(value)
        elif value is not None and type(value) is not bool:
            _deny()


def _summary(value):
    # Observed summary shape only. Its values have no ranking/count authority.
    if type(value) is not dict or set(value) != {'items', 'reportType'}:
        _deny()
    if type(value['reportType']) not in (str, int):
        _deny()
    if type(value['items']) is not list:
        _deny()
    for item in value['items']:
        if type(item) is not dict or set(item) != {'name', 'value'}:
            _deny()
        _identity(item['name'])
        if type(item['value']) in (dict, list):
            _deny()


def _assist_display(value):
    """Opaque display text, never a quantity or unit conversion evidence.

Null explicitly means no display value. Numbers are rejected rather than
silently acquiring quantity semantics or changing their representation.
"""
    if value is not None and (type(value) is not str or len(value) > 200
                             or any(ord(c) < 32 or ord(c) == 127
                                    or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _deny()
    return value


def _display_identity(value, *, required=False):
    """Untrusted source label, retained as data and never as authorization."""
    if (type(value) is not str or len(value) > 200 or required and not value
            or any(ord(c) < 32 or ord(c) == 127
                   or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _deny()
    return value


@dataclass(frozen=True)
class RankingRow:
    product_id: str
    standard_id: str
    store_id: str
    qty: Decimal
    assist_qty_display: str | None
    total: Decimal
    product_name: str
    product_code: str
    standard_name: str
    assist_qty_interpretation: str = 'uninterpreted_display_text'


@dataclass(frozen=True)
class RankingPage:
    skip: int
    take: int
    total_count: int
    store_id: str
    rows: tuple[RankingRow, ...]


@dataclass(frozen=True)
class RankingCandidate:
    store_id: str
    rows: tuple[RankingRow, ...]
    total_count: int
    page_count: int
    classification: str = 'source_report_candidate'
    structurally_complete: bool = True
    business_semantics_verified: bool = False


def parse_page(raw: bytes, *, expected_store: str, skip: int, take: int) -> RankingPage:
    """Decode only a successful ABP envelope; retain decimal lexical values.

Envelope/result fields are finite. Rich item extras are bounded JSON and are
discarded; only the six explicit business fields influence this candidate.
"""
    _identity(expected_store)
    _integer(skip, 0, MAX_ROWS)
    _integer(take, 1, MAX_PAGE_SIZE)
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        _deny()
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                           parse_float=Decimal, parse_constant=lambda _: _deny())
    except (ValueError, UnicodeError, RecursionError):
        _deny()
    _bounded_json(value)
    if type(value) is not dict or not {'success', 'result'} <= value.keys() or value.keys() - {'success', 'result', 'error', 'targetUrl', 'unAuthorizedRequest', '__abp'}:
        _deny()
    if value['success'] is not True or value.get('error') is not None:
        _deny()
    # Upstream includes a display/navigation target on successful reports.
    # Treat it solely as ignored metadata: never follow or authorize with it.
    target = value.get('targetUrl')
    if target is not None and (type(target) is not str or len(target) > 2048
                               or any(ord(c) < 32 or ord(c) == 127 for c in target)):
        _deny()
    if 'unAuthorizedRequest' in value and value['unAuthorizedRequest'] is not False:
        _deny()
    if '__abp' in value and value['__abp'] is not True:
        _deny()
    result = value['result']
    if type(result) is not dict or not {'items', 'totalCount'} <= result.keys() or result.keys() - {'items', 'totalCount', 'pageTotal', 'sum', 'columns', 'reportSummaryStat'}:
        _deny()
    total = _integer(result['totalCount'], 0, MAX_ROWS)
    # These optional metadata shapes are candidate-only, never completeness or
    # sum evidence. Refuse unrecognized types rather than interpreting them.
    if 'pageTotal' in result:
        _integer(result['pageTotal'], 0, MAX_ROWS)
    if 'sum' in result and type(result['sum']) is not dict:
        _deny()
    if 'columns' in result and type(result['columns']) is not list:
        _deny()
    if 'reportSummaryStat' in result:
        _summary(result['reportSummaryStat'])
    items = result['items']
    if type(items) is not list or len(items) > take or skip > total or len(items) != min(take, total - skip):
        _deny()
    rows = []
    for item in items:
        if type(item) is not dict or not {'productId', 'standardId', 'storeId', 'qty', 'assistQty', 'total',
                                          'productName', 'productCode', 'standardName'} <= item.keys():
            _deny()
        product, standard, store = (_identity(item[key]) for key in ('productId', 'standardId', 'storeId'))
        if store != expected_store:
            _deny()
        row = RankingRow(product, standard, store, _decimal(item['qty']),
                         _assist_display(item['assistQty']), _decimal(item['total']),
                         _display_identity(item['productName'], required=True),
                         _display_identity(item['productCode']),
                         _display_identity(item['standardName']))
        if rows and row.qty > rows[-1].qty:
            _deny()
        rows.append(row)
    identities = [(r.product_id, r.standard_id) for r in rows]
    if len(set(identities)) != len(identities):
        _deny()
    return RankingPage(skip, take, total, expected_store, tuple(rows))


def assemble_candidate(pages: tuple[RankingPage, ...]) -> RankingCandidate:
    """Accept contiguous complete pages only; preserve server order and units.

Does not aggregate variants, subtract returns, infer units or compute Top20.
"""
    if type(pages) is not tuple or not pages or len(pages) > MAX_ROWS + 1:
        _deny()
    rows = []
    seen = set()
    first = pages[0]
    if type(first) is not RankingPage:
        _deny()
    for index, page in enumerate(pages):
        if type(page) is not RankingPage or page.skip != len(rows) or page.total_count != first.total_count or page.store_id != first.store_id or page.take != first.take:
            _deny()
        _identity(page.store_id)
        _integer(page.skip, 0, MAX_ROWS)
        _integer(page.take, 1, MAX_PAGE_SIZE)
        _integer(page.total_count, 0, MAX_ROWS)
        if type(page.rows) is not tuple or len(page.rows) != min(page.take, page.total_count - page.skip):
            _deny()
        if index and len(rows) >= first.total_count:
            _deny()
        for row in page.rows:
            if type(row) is not RankingRow or row.store_id != page.store_id:
                _deny()
            _identity(row.product_id)
            _identity(row.standard_id)
            _assist_display(row.assist_qty_display)
            _display_identity(row.product_name, required=True)
            _display_identity(row.product_code)
            _display_identity(row.standard_name)
            if row.assist_qty_interpretation != 'uninterpreted_display_text':
                _deny()
            for number in (row.qty, row.total):
                if type(number) is not Decimal:
                    _deny()
                _decimal(number)
            identity = (row.product_id, row.standard_id)
            if identity in seen or rows and row.qty > rows[-1].qty:
                _deny()
            seen.add(identity)
            rows.append(row)
    if len(rows) != first.total_count:
        _deny()
    return RankingCandidate(first.store_id, tuple(rows), first.total_count, len(pages))
