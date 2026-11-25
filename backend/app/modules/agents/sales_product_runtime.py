"""Pure V2 product candidate projection; source semantics remain unverified."""
from decimal import Decimal, DecimalException
from typing import Literal
import re
import json
from hashlib import sha256
from pydantic import Field, model_validator
from .sales_ranking_runtime import Input, Collection, validate_source
from backend.app.modules.integrations.guanjia.sales_ranking_contract import _decimal, _identity, _display_identity

KEY = 'guanjia.sales_ranking.read'
NAME = 'v4_guanjia_sales_ranking_v2'
NAMES = {(KEY, '2'): NAME}
# Reserve room within unchanged 131072-byte receipt/IPC limits for private metadata.
MAX_PROJECTION_BYTES = 65536


def _encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',', ':'),allow_nan=False).encode('utf-8')
DESCRIPTION = ('读取当前授权门店本月商品销售候选，Host完整双读全部规格后按商品ID汇总来源数量和金额，'
               '按汇总数量降序、并列按商品ID，至多20种商品；展示有界贡献规格前缀、完整规格数及有序ID摘要。'
               '数量与金额为十进制字符串，单位、净销、退货、赠品、日期端点及原子快照未核，不得声称准确净销Top20。参数只能为空对象。')

def standard_ids_digest(ids):
    """SHA-256 of ordered full IDs as compact UTF-8 JSON, without ASCII escaping."""
    return sha256(json.dumps(list(ids),ensure_ascii=False,separators=(',', ':'),allow_nan=False).encode('utf-8')).hexdigest()


class Row(Input):
    product_id: str = Field(min_length=1, max_length=200)
    store_id: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    product_code: str = Field(max_length=200)
    standard_ids: list[str] = Field(max_length=5)
    standard_count: int = Field(ge=1, le=100000)
    standard_ids_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    qty: str = Field(max_length=1200)
    total: str = Field(max_length=1200)
    @model_validator(mode='after')
    def valid(self):
        for value in (self.product_id, self.store_id, *self.standard_ids): _identity(value)
        _display_identity(self.product_name, required=True)
        _display_identity(self.product_code)
        if (len(set(self.standard_ids)) != len(self.standard_ids)
                or len(self.standard_ids) > self.standard_count
                or len(self.standard_ids) == self.standard_count
                   and standard_ids_digest(self.standard_ids) != self.standard_ids_sha256):
            raise ValueError('sales_product_output_invalid')
        for value in (self.qty, self.total):
            # The source parser's string bound is intentionally narrower than
            # valid exponent-expanded aggregate strings. Validate exact Decimal
            # representation and range after checking its finite grammar.
            if re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', value) is None:
                raise ValueError('sales_product_output_invalid')
            try:
                number=_decimal(Decimal(value))
            except DecimalException:
                raise ValueError('sales_product_output_invalid') from None
            # A source report may carry signed returns; do not discard them or
            # infer verified net-sales semantics from their presence.
        return self

class Output(Input):
    version: Literal[2]
    classification: Literal['source_report_product_candidate']
    row_granularity: Literal['product_id']
    business_semantics_verified: Literal[False]
    unit_semantics_verified: Literal[False]
    date_endpoint_semantics_verified: Literal[False]
    atomic_snapshot_verified: Literal[False]
    net_sales_semantics: Literal['unverified']
    returns_semantics: Literal['unverified']
    gifts_semantics: Literal['unverified']
    store_id: str = Field(min_length=1, max_length=200)
    product_count: int = Field(ge=0, le=100000)
    source_row_count: int = Field(ge=0, le=100000)
    collection: Collection
    rows: list[Row] = Field(max_length=20)
    @model_validator(mode='after')
    def valid(self):
        _identity(self.store_id)
        if (self.source_row_count != self.collection.total_count
                or self.product_count > self.source_row_count
                or (self.product_count == 0) != (self.source_row_count == 0)
                or len(self.rows) != min(20, self.product_count)):
            raise ValueError('sales_product_output_invalid')
        seen=set(); previous=None; contributions=0
        for row in self.rows:
            quantity=Decimal(row.qty)
            if (row.store_id != self.store_id or row.product_id in seen
                    or previous is not None and (quantity > previous[0]
                       or quantity == previous[0] and row.product_id <= previous[1])):
                raise ValueError('sales_product_output_invalid')
            seen.add(row.product_id); previous=(quantity,row.product_id)
            contributions += row.standard_count
        if (contributions > self.source_row_count or self.product_count <= 20 and contributions != self.source_row_count
                or self.product_count > 20 and contributions + self.product_count - 20 > self.source_row_count):
            raise ValueError('sales_product_output_invalid')
        if len(_encoded(self.model_dump(mode='json'))) > MAX_PROJECTION_BYTES:
            raise ValueError('sales_product_output_invalid')
        return self

def project_receipt(receipt):
    from backend.app.modules.integrations.guanjia.sales_ranking_host import ReportCandidateReceipt
    from backend.app.modules.integrations.guanjia.sales_product_candidate import aggregate_product_candidate
    from .sales_ranking_runtime import project_receipt as v1_projection
    if type(receipt) is not ReportCandidateReceipt: raise ValueError('sales_product_output_invalid')
    # Reuse V1's date/comparison summary validation, never its truncated rows.
    summary=v1_projection(receipt)
    candidate=aggregate_product_candidate(receipt.candidate)
    payload=dict(version=2, classification=candidate.classification, row_granularity='product_id',
        business_semantics_verified=False, unit_semantics_verified=False,
        date_endpoint_semantics_verified=receipt.date_endpoint_semantics_verified,
        atomic_snapshot_verified=receipt.atomic_snapshot_verified,
        net_sales_semantics='unverified', returns_semantics='unverified', gifts_semantics='unverified',
        store_id=candidate.store_id, product_count=candidate.product_count,
        source_row_count=candidate.source_row_count, collection=summary['collection'],
        rows=[Row(product_id=r.product_id,store_id=r.store_id,product_name=r.product_name,
                  product_code=r.product_code,standard_ids=list(r.standard_ids[:5]),standard_count=len(r.standard_ids),
                  standard_ids_sha256=standard_ids_digest(r.standard_ids),qty=str(r.qty),total=str(r.total)).model_dump(mode='json')
              for r in candidate.rows])
    previews=[row['standard_ids'] for row in payload['rows']]
    # A deterministic common prefix width keeps IDs intact, including UTF-8.
    # The immutable full journal and full ordered ID digest retain provenance.
    for width in range(5,-1,-1):
        for row,preview in zip(payload['rows'],previews): row['standard_ids']=preview[:width]
        if len(_encoded(payload)) <= MAX_PROJECTION_BYTES:
            return Output(**payload).model_dump(mode='json')
    raise ValueError('sales_product_output_invalid')
