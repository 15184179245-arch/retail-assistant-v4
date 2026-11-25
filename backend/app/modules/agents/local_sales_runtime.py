"""Explicit v3 local-sales model contract, not a grant or executable tool."""
from datetime import date, datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.modules.integrations.guanjia.local_sales_analysis import RULE

KEY = 'guanjia.sales_ranking.read'
NAME = 'v4_guanjia_local_sales_v1'
NAMES = {(KEY, '3'): NAME}
DESCRIPTION = ('查询当前有权门店的管家婆本地逐笔销售。coverage查看日期覆盖、缺日及采集水位；'
    'aggregate对完整明细按商品ID聚合、排序并分页，net_qty为排除显式赠品后源signed数量之和，'
    '负退货与红冲已计入，不可再扣一次。保留各商品基本单位，不是统一件数。'
    'sale_qty是扣自身红冲的销售出库量，return_qty为净退货量，gift_qty为显式赠品量。'
    'rows查看原始来源数量及行身份。日期省略时由Host选择本月到本地最新已发布日；'
    '缺日不能当零或出准确排行。采集时间与查询时间分开，当前日可能尚未结束；'
    '不从原金额或当前售价推净营业额，不把录单时间当顾客销售时段。')
_SHA = r'^[a-f0-9]{64}$'
_QTY = r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$'
_CHINA = ZoneInfo('Asia/Shanghai')


def _deny():
    raise ValueError('local_sales_contract_invalid') from None


def _day(value):
    if type(value) is not str:
        _deny()
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        _deny()
    return result


def _instant(value):
    if type(value) is not str or len(value) > 64:
        _deny()
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        _deny()
    return result


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        _deny()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)

    @model_validator(mode='before')
    @classmethod
    def literal_types(cls, value):
        if type(value) is dict:
            if 'version' in value and type(value['version']) is not int:
                _deny()
            for key in ('monetary_totals_included', 'atomic_snapshot_verified'):
                if key in value and type(value[key]) is not bool:
                    _deny()
        return value

    @model_validator(mode='after')
    def safe_text(self):
        for value in self.__dict__.values():
            if type(value) is str and any(ord(c)<32 or 127<=ord(c)<=159 or 0xD800<=ord(c)<=0xDFFF for c in value):
                _deny()
        return self


class Input(Strict):
    operation: Literal['coverage', 'aggregate', 'rows'] = 'coverage'
    start_date: str | None = Field(default=None, max_length=10)
    end_date: str | None = Field(default=None, max_length=10)
    metric: Literal['net_qty', 'sale_qty', 'return_qty', 'gift_qty'] = 'net_qty'
    order: Literal['asc', 'desc'] = 'desc'
    offset: int = Field(default=0, ge=0, le=1_000_000)
    limit: int = Field(default=100, ge=1, le=1000)
    product_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def consistent(self):
        if self.start_date is not None:
            _day(self.start_date)
        if self.end_date is not None:
            _day(self.end_date)
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            _deny()
        if self.product_id is not None and self.operation != 'rows':
            _deny()
        return self


class SourcePin(Strict):
    version: Literal[3]
    environment_sha256: str = Field(pattern=_SHA)
    descriptor_sha256: str = Field(pattern=_SHA)
    dataset_binding_sha256: str = Field(pattern=_SHA)
    analysis_contract_sha256: str = Field(pattern=_SHA)
    organization_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    store_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    requester_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    agent_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    machine_id: str = Field(min_length=1, max_length=120, pattern=r'^[A-Za-z0-9_-]+$')
    shop_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    source_namespace: Literal['erp_posted_sales_detail_v1']
    earliest_day: str = Field(max_length=10)

    @field_validator('earliest_day')
    @classmethod
    def valid_day(cls, value):
        _day(value)
        return value


def validate_source(pin, binding=None, *, now=None):
    # Dataset identity has no ERP-token expiry. Current SQL Work/user/Agent
    # authorization remains mandatory at Host execution and disclosure gates.
    value = SourcePin.model_validate(pin).model_dump(mode='json')
    if binding is not None and any(value[k] != binding.get(k) for k in
            ('organization_id', 'store_id', 'requester_id', 'agent_id')):
        _deny()
    return value


class DayVersion(Strict):
    day: str = Field(max_length=10)
    acquisition: str | None
    manifest_sha256: str | None = Field(pattern=_SHA)
    row_count: int | None = Field(ge=0, le=100000)
    captured_at: str | None

    @model_validator(mode='after')
    def consistent(self):
        _day(self.day)
        if self.acquisition is None:
            if any(v is not None for v in (self.manifest_sha256, self.row_count, self.captured_at)):
                _deny()
        else:
            _uuid(self.acquisition)
            if self.manifest_sha256 is None or self.row_count is None or self.captured_at is None:
                _deny()
            _instant(self.captured_at)
        return self


class Collection(Strict):
    start_date: str
    end_date: str
    queried_at: str
    captured_through: str | None
    snapshot_sha256: str = Field(pattern=_SHA)
    dataset_binding_sha256: str = Field(pattern=_SHA)
    source_namespace: Literal['erp_posted_sales_detail_v1']
    calendar_basis: Literal['source_bill_date']
    source_row_count: int = Field(ge=0, le=1_000_000)
    structural_coverage_complete: bool
    contains_open_day: bool
    missing_days: list[str] = Field(max_length=366)
    days: list[DayVersion] = Field(min_length=1, max_length=366)

    @model_validator(mode='after')
    def consistent(self):
        start, end, queried = _day(self.start_date), _day(self.end_date), _instant(self.queried_at)
        if not start <= end <= queried.astimezone(_CHINA).date() or (end-start).days >= 366:
            _deny()
        expected = [(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
        missing = [d.day for d in self.days if d.acquisition is None]
        if ([d.day for d in self.days] != expected or missing != self.missing_days or
                self.structural_coverage_complete != (not missing) or
                self.source_row_count != sum(d.row_count for d in self.days if d.row_count is not None) or
                self.contains_open_day != (end == queried.astimezone(_CHINA).date())):
            _deny()
        times = [_instant(d.captured_at) for d in self.days if d.captured_at is not None]
        if any(t > queried for t in times):
            _deny()
        if not times:
            if self.captured_through is not None:
                _deny()
        elif self.captured_through is None or _instant(self.captured_through) != max(times):
            _deny()
        return self


class ProductRow(Strict):
    product_id: str = Field(min_length=1, max_length=200)
    product_code: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    standard_id: str = Field(min_length=1, max_length=200)
    standard_name: str = Field(min_length=1, max_length=200)
    quantity: str = Field(pattern=_QTY, max_length=2400)
    source_row_count: int = Field(ge=1, le=1_000_000)
    source_refs_sha256: str = Field(pattern=_SHA)

    @field_validator('quantity')
    @classmethod
    def finite_quantity(cls, value):
        try:
            number=Decimal(value)
            if (not number.is_finite() or len(number.as_tuple().digits)>2400 or
                    not -1000<=number.as_tuple().exponent<=1206 or number.adjusted()>1206):
                _deny()
        except DecimalException:
            _deny()
        return value


class DetailRow(Strict):
    day: str
    acquisition: str
    source_ordinal: int = Field(ge=0, le=100000)
    product_id: str = Field(min_length=1, max_length=200)
    product_code: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    standard_id: str = Field(min_length=1, max_length=200)
    standard_name: str = Field(min_length=1, max_length=200)
    source_quantity: str = Field(pattern=_QTY, max_length=2400)
    is_giveaway: bool
    bill_type: int
    business_type: int
    red_word: int

    @field_validator('source_quantity')
    @classmethod
    def finite_quantity(cls, value):
        return ProductRow.finite_quantity(value)

    @model_validator(mode='after')
    def identity(self):
        _day(self.day); _uuid(self.acquisition)
        return self


class Output(Strict):
    version: Literal[3]
    classification: Literal['local_posted_sales_analysis']
    operation: Literal['coverage', 'aggregate', 'rows']
    collection: Collection
    quantity_rule: Literal[RULE]
    monetary_totals_included: Literal[False]
    atomic_snapshot_verified: Literal[False]
    metric: Literal['net_qty', 'sale_qty', 'return_qty', 'gift_qty'] | None = None
    order: Literal['asc', 'desc'] | None = None
    offset: int = Field(default=0, ge=0, le=1_000_000)
    next_offset: int | None = Field(default=None, ge=1, le=1_000_000)
    total_product_count: int | None = Field(default=None, ge=0, le=1_000_000)
    explicit_gift_rows: int | None = Field(default=None, ge=0, le=1_000_000)
    total_matching_rows: int | None = Field(default=None, ge=0, le=1_000_000)
    products: list[ProductRow] = Field(default_factory=list, max_length=1000)
    rows: list[DetailRow] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def consistent(self):
        if self.operation == 'coverage':
            if (self.products or self.rows or self.offset or self.next_offset is not None or
                    any(v is not None for v in (self.metric,self.order,self.total_product_count,self.explicit_gift_rows,self.total_matching_rows))):
                _deny()
        elif self.operation == 'aggregate':
            if (not self.collection.structural_coverage_complete or self.rows or self.metric is None or
                    self.order is None or self.total_product_count is None or self.explicit_gift_rows is None or self.total_matching_rows is not None or
                    not 0 <= self.offset <= self.total_product_count <= self.collection.source_row_count or
                    self.explicit_gift_rows > self.collection.source_row_count or
                    len(self.products) > self.total_product_count-self.offset):
                _deny()
            ids = [r.product_id for r in self.products]
            if len(set(ids)) != len(ids):
                _deny()
            for prior, current in zip(self.products, self.products[1:]):
                a, b = Decimal(prior.quantity), Decimal(current.quantity)
                if ((self.order == 'desc' and a < b) or (self.order == 'asc' and a > b) or
                        a == b and prior.product_id >= current.product_id):
                    _deny()
            if sum(r.source_row_count for r in self.products) > self.collection.source_row_count:
                _deny()
            end=self.offset+len(self.products)
            if ((self.offset<self.total_product_count and not self.products) or
                    self.next_offset != (end if end<self.total_product_count else None) or
                    self.offset==0 and end==self.total_product_count and
                    sum(r.source_row_count for r in self.products)!=self.collection.source_row_count):
                _deny()
        else:
            if self.products or any(v is not None for v in (self.metric,self.order,self.total_product_count,self.explicit_gift_rows)):
                _deny()
            if (self.total_matching_rows is None or self.total_matching_rows>self.collection.source_row_count or
                    self.offset>self.total_matching_rows or len(self.rows)>self.total_matching_rows-self.offset):
                _deny()
            days = {d.day:d for d in self.collection.days}
            seen = set()
            for row in self.rows:
                day = days.get(row.day)
                ref = row.day,row.acquisition,row.source_ordinal
                if (day is None or day.acquisition != row.acquisition or day.row_count is None or
                        row.source_ordinal >= day.row_count or ref in seen):
                    _deny()
                seen.add(ref)
            end=self.offset+len(self.rows)
            if ((self.offset<self.total_matching_rows and not self.rows) or
                    self.next_offset != (end if end<self.total_matching_rows else None)):
                _deny()
        return self
