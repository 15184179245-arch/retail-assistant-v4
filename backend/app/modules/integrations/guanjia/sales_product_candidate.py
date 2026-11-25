"""Pure full-report product grouping; quantities retain unverified source units.

This is neither net-sales accounting nor permission to disclose a report.
The caller must supply an authorized, complete, double-read source candidate.
"""
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, Inexact, Rounded, localcontext

from .sales_ranking_contract import (
    MAX_ROWS, RankingCandidate, RankingRow, _assist_display, _decimal,
    _display_identity, _identity,
)


class ProductCandidateError(ValueError):
    """Finite diagnostic without source identities or values."""


def _deny():
    raise ProductCandidateError('sales_product_candidate_rejected') from None


@dataclass(frozen=True)
class ProductRow:
    product_id: str
    store_id: str
    product_name: str
    product_code: str
    standard_ids: tuple[str, ...]
    qty: Decimal
    total: Decimal


@dataclass(frozen=True)
class ProductCandidate:
    store_id: str
    rows: tuple[ProductRow, ...]
    source_row_count: int
    product_count: int
    classification: str = field(default='source_report_product_candidate', init=False)
    business_semantics_verified: bool = field(default=False, init=False)
    unit_semantics_verified: bool = field(default=False, init=False)


def aggregate_product_candidate(candidate: RankingCandidate) -> ProductCandidate:
    """Sum every source row before selecting at most twenty distinct products.

    No conversion of units, additional return subtraction, authorization, IO or rounding.
    Source signed values are retained; their business meaning stays unverified.
    Source and aggregate decimals share the parser's 200-digit / exponent-1000
    limits. Arithmetic uses enough precision for their full exponent span, and
    refuses any inexact result rather than inheriting ambient Decimal rounding.
    """
    try:
        if (type(candidate) is not RankingCandidate
                or candidate.classification != 'source_report_candidate'
                or candidate.structurally_complete is not True
                or candidate.business_semantics_verified is not False
                or type(candidate.total_count) is not int
                or not 0 <= candidate.total_count <= MAX_ROWS
                or type(candidate.rows) is not tuple
                or len(candidate.rows) != candidate.total_count
                or type(candidate.page_count) is not int
                or not 1 <= candidate.page_count <= max(1, candidate.total_count)):
            _deny()
        _identity(candidate.store_id)
        groups = {}
        seen = set()
        with localcontext() as context:
            context.prec = 2206
            context.Emax = 10000
            context.Emin = -10000
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            for row in candidate.rows:
                if type(row) is not RankingRow or row.store_id != candidate.store_id:
                    _deny()
                for value in (row.product_id, row.standard_id, row.store_id):
                    _identity(value)
                _display_identity(row.product_name, required=True)
                _display_identity(row.product_code)
                _display_identity(row.standard_name)
                _assist_display(row.assist_qty_display)
                if row.assist_qty_interpretation != 'uninterpreted_display_text':
                    _deny()
                identity = (row.product_id, row.standard_id)
                if identity in seen:
                    _deny()
                seen.add(identity)
                for value in (row.qty, row.total):
                    if type(value) is not Decimal or not _decimal(value).is_finite():
                        _deny()
                display = (row.store_id, row.product_name, row.product_code)
                if row.product_id not in groups:
                    groups[row.product_id] = [display, [row.standard_id], row.qty, row.total]
                    continue
                group = groups[row.product_id]
                if group[0] != display:
                    _deny()
                group[1].append(row.standard_id)
                group[2] = _decimal(group[2] + row.qty)
                group[3] = _decimal(group[3] + row.total)
            rows = [ProductRow(product, *group[0], tuple(group[1]), group[2], group[3])
                    for product, group in groups.items()]
            rows.sort(key=lambda row: (-row.qty, row.product_id))
        return ProductCandidate(candidate.store_id, tuple(rows[:20]),
                                candidate.total_count, len(groups))
    except (ValueError, TypeError, AttributeError, DecimalException):
        _deny()
