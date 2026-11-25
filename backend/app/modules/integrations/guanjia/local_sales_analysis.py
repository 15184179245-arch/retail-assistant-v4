"""Pure parameterized product quantities from complete projected local rows.

No IO or authority is provided here. Host must validate the snapshot, current
principal and complete row set before invoking it, and recheck before disclosure.
Money and customer sale hours are deliberately not inferred from source fields.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, Inexact, Rounded, localcontext
from hashlib import sha256
import json

from .erp_sales_detail_contract import ErpSalesDetailRow
from .sales_history_query import LocalSalesRow
from .sales_history_archive import _uuid
from .retail_sales_detail_contract import _id, _text, _bill_day
from .sales_ranking_contract import _decimal

RULE = 'posted-signed-basic-quantity-excluding-explicit-gifts/1'
METRICS = frozenset({'net_qty', 'sale_qty', 'return_qty', 'gift_qty'})
MAX_ROWS = 1_000_000


class LocalSalesAnalysisError(ValueError):
    """Fixed diagnostic, without source values or paths."""


def _deny():
    raise LocalSalesAnalysisError('local_sales_analysis_rejected') from None


@dataclass(frozen=True)
class ProductQuantity:
    product_id: str
    product_code: str
    product_name: str
    standard_id: str
    standard_name: str
    quantity: Decimal
    source_row_count: int
    source_refs_sha256: str
    label_variants: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProductAnalysis:
    metric: str
    order: str
    total_product_count: int
    source_row_count: int
    explicit_gift_rows: int
    offset: int
    next_offset: int | None
    rows: tuple[ProductQuantity, ...]
    quantity_rule: str = RULE
    monetary_totals_included: bool = False


def aggregate_products(rows, *, expected_shop_id, expected_row_count, metric='net_qty',
                       order='desc', offset=0, limit=100):
    """Full-set grouping before pagination; no preselected products or rank size.

net_qty sums signed source quantities excluding explicit gifts. sale_qty sums
non-gift sales documents including their reversals; return_qty negates signed
non-gift return documents (including return reversals). gift_qty reports signed
explicit gifts. Both sides of red-word pairs remain in the source arithmetic.
Different products retain their respective basic units; no conversion to pieces.
"""
    try:
        _id(expected_shop_id)
        if (type(rows) is not tuple or type(expected_row_count) is not int or
                not 0 <= expected_row_count <= MAX_ROWS or len(rows) != expected_row_count or
                type(metric) is not str or metric not in METRICS or order not in ('asc', 'desc') or
                type(order) is not str or type(offset) is not int or not 0 <= offset <= MAX_ROWS or
                type(limit) is not int or not 1 <= limit <= 1000):
            _deny()
        groups, seen, acquisitions, gifts = {}, set(), {}, 0
        with localcontext() as context:
            context.prec, context.Emax, context.Emin = 2400, 10000, -10000
            context.traps[Inexact] = context.traps[Rounded] = True
            for local in rows:
                if type(local) is not LocalSalesRow or type(local.row) is not ErpSalesDetailRow:
                    _deny()
                r = local.row
                if type(local.day) is not str or date.fromisoformat(local.day).isoformat() != local.day:
                    _deny()
                _uuid(local.acquisition)
                if local.day in acquisitions and acquisitions[local.day] != local.acquisition:
                    _deny()
                acquisitions[local.day] = local.acquisition
                if (r.shop_id != expected_shop_id or type(r.source_ordinal) is not int or r.source_ordinal < 0 or
                        type(r.bill_state) is not int or r.bill_state != 12 or
                        _bill_day(r.bill_date) != date.fromisoformat(local.day) or
                        type(r.is_basic_standard_id) is not bool or not r.is_basic_standard_id or
                        type(r.is_giveaway) is not bool or type(r.qty) is not Decimal or
                        type(r.giveaway_qty) is not Decimal):
                    _deny()
                for identity in (r.product_id, r.product_code, r.standard_id):
                    _id(identity)
                _text(r.product_name, required=True); _text(r.standard_name, required=True)
                _decimal(r.qty); _decimal(r.giveaway_qty)
                ref = (local.day, local.acquisition, r.source_ordinal)
                if ref in seen:
                    _deny()
                seen.add(ref)
                if (type(r.bill_type) is not int or type(r.business_type) is not int or
                        type(r.red_word) is not int or r.red_word not in (0, 1, 2) or
                        (r.bill_type, r.business_type) not in ((102, 2), (103, 3)) or
                        type(r.erp_bill_business_type) is not int or r.erp_bill_business_type not in (0, 1)):
                    _deny()
                positive = (r.bill_type == 102) != (r.red_word == 2)
                if positive and r.qty < 0 or not positive and r.qty > 0:
                    _deny()
                if not r.is_giveaway and r.giveaway_qty != 0:
                    _deny()
                if r.is_giveaway and r.qty != r.giveaway_qty:
                    # The source must identify a whole explicit gift row;
                    # an unresolved mixed sale/gift quantity is not guessed.
                    _deny()
                gifts += r.is_giveaway
                group = groups.setdefault(r.product_id, dict(unit=(r.standard_id, r.standard_name),
                    qty=Decimal(0), count=0, refs=[], labels=set(), label=None))
                if group['unit'] != (r.standard_id, r.standard_name):
                    _deny()
                label = (r.product_code, r.product_name)
                group['labels'].add(label)
                candidate = (local.day, r.source_ordinal, label)
                if group['label'] is None or candidate[:2] > group['label'][:2]:
                    group['label'] = candidate
                group['count'] += 1; group['refs'].append(ref)
                if metric == 'gift_qty':
                    value = r.qty if r.is_giveaway else Decimal(0)
                elif r.is_giveaway:
                    value = Decimal(0)
                elif metric == 'net_qty' or metric == 'sale_qty' and r.bill_type == 102:
                    value = r.qty
                elif metric == 'return_qty' and r.bill_type == 103:
                    value = context.subtract(Decimal(0), r.qty)
                else:
                    value = Decimal(0)
                group['qty'] += value
            result = []
            for identity, group in groups.items():
                code, name = group['label'][2]
                refs = json.dumps(sorted(group['refs']), separators=(',', ':'), ensure_ascii=True).encode()
                result.append(ProductQuantity(identity, code, name, *group['unit'], group['qty'],
                    group['count'], sha256(refs).hexdigest(), tuple(sorted(group['labels']))))
            # Stable second sort avoids unary-minus rounding under a caller's
            # ambient Decimal context and specifies product-ID tie ordering.
            result.sort(key=lambda r: r.product_id)
            result.sort(key=lambda r: r.quantity, reverse=order == 'desc')
        if offset > len(result):
            _deny()
        page = tuple(result[offset:offset+limit])
        return ProductAnalysis(metric, order, len(result), expected_row_count, gifts, offset,
            offset+len(page) if offset+len(page) < len(result) else None, page)
    except Exception:
        _deny()
