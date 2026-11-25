"""Finite model contract only. A source integrity pin never grants authority."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

KEY = 'guanjia.sales_ranking.read'
NAME = 'v4_guanjia_sales_ranking_v1'
NAMES = {(KEY, '1'): NAME}
DESCRIPTION = ('读取当前授权门店本月商品销售排行候选，Host固定日期与完整分页，返回原来源顺序至多20行及采集摘要。'
               '数量/金额为来源十进制字符串；净销、退货、赠品、日期端点和原子快照语义未核，不得声称准确Top20。参数只能为空对象。')

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Row(Input):
    product_id: str = Field(min_length=1, max_length=200)
    standard_id: str = Field(min_length=1, max_length=200)
    store_id: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=200)
    product_code: str = Field(max_length=200)
    standard_name: str = Field(max_length=200)
    qty: str = Field(pattern=r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$', max_length=1200)
    total: str = Field(pattern=r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$', max_length=1200)

class Collection(Input):
    total_count: int = Field(ge=0, le=100000)
    page_count: int = Field(ge=1, le=100000)
    structurally_complete: Literal[True]
    independent_reads_match: Literal[True]
    projection_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    verification_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    as_of_utc: str
    report_start_date: str
    report_end_date: str
    report_timezone: Literal['Asia/Shanghai']
    @model_validator(mode='after')
    def consistent(self):
        if self.projection_sha256 != self.verification_sha256:
            raise ValueError('sales_ranking_output_invalid')
        value=datetime.fromisoformat(self.as_of_utc.replace('Z','+00:00'))
        if value.tzinfo is None or value.utcoffset().total_seconds()!=0:
            raise ValueError('sales_ranking_output_invalid')
        local=value.astimezone(ZoneInfo('Asia/Shanghai'))
        if (self.report_start_date,self.report_end_date)!=(local.replace(day=1).strftime('%Y-%m-%d'),local.strftime('%Y-%m-%d')):
            raise ValueError('sales_ranking_output_invalid')
        return self

class Output(Input):
    version: Literal[1]
    classification: Literal['source_report_candidate']
    row_granularity: Literal['product_id_and_standard_id']
    business_semantics_verified: Literal[False]
    date_endpoint_semantics_verified: Literal[False]
    atomic_snapshot_verified: Literal[False]
    net_sales_semantics: Literal['unverified']
    returns_semantics: Literal['unverified']
    gifts_semantics: Literal['unverified']
    store_id: str = Field(min_length=1, max_length=200)
    collection: Collection
    rows: list[Row] = Field(max_length=20)
    @model_validator(mode='after')
    def bounded(self):
        if len(self.rows)!=min(20,self.collection.total_count):
            raise ValueError('sales_ranking_output_invalid')
        seen=set(); previous=None
        for row in self.rows:
            key=(row.product_id,row.standard_id)
            if row.store_id!=self.store_id or key in seen or previous is not None and Decimal(row.qty)>previous:
                raise ValueError('sales_ranking_output_invalid')
            seen.add(key);previous=Decimal(row.qty)
            for text in (row.product_id,row.standard_id,row.store_id,row.product_name,row.product_code,row.standard_name):
                if any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in text):
                    raise ValueError('sales_ranking_output_invalid')
        return self

def validate_source(pin, binding=None, *, now=None):
    from backend.app.modules.integrations.guanjia.sales_ranking_source import validate_pin
    value=validate_pin(pin)
    instant=datetime.fromisoformat(value['expires_at'].replace('Z','+00:00'))
    current=now or datetime.now(timezone.utc)
    if current.tzinfo is None:current=current.replace(tzinfo=timezone.utc)
    if instant<=current:
        raise ValueError('sales_ranking_source_rejected')
    if binding is not None and any(value[k]!=binding.get(k) for k in ('organization_id','store_id','requester_id','agent_id')):
        raise ValueError('sales_ranking_source_rejected')
    return value

def project_receipt(receipt):
    """Host calls only after its authority/commit gate; never performs I/O."""
    from backend.app.modules.integrations.guanjia.sales_ranking_host import ReportCandidateReceipt
    if type(receipt) is not ReportCandidateReceipt:
        raise ValueError('sales_ranking_output_invalid')
    c=receipt.candidate
    if c.classification!='source_report_candidate' or c.business_semantics_verified is not False or len(c.rows)!=c.total_count:
        raise ValueError('sales_ranking_output_invalid')
    local=datetime.fromisoformat(receipt.as_of_utc.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai'))
    return Output(version=1, classification=receipt.classification,
        row_granularity='product_id_and_standard_id',
        business_semantics_verified=receipt.business_semantics_verified,
        date_endpoint_semantics_verified=receipt.date_endpoint_semantics_verified,
        atomic_snapshot_verified=receipt.atomic_snapshot_verified,
        net_sales_semantics='unverified', returns_semantics='unverified', gifts_semantics='unverified',
        store_id=c.store_id, collection=Collection(total_count=c.total_count,page_count=c.page_count,
            structurally_complete=c.structurally_complete,independent_reads_match=receipt.independent_reads_match,
            projection_sha256=receipt.projection_sha256,verification_sha256=receipt.verification_sha256,
            as_of_utc=receipt.as_of_utc, report_start_date=local.replace(day=1).strftime('%Y-%m-%d'),
            report_end_date=local.strftime('%Y-%m-%d'),report_timezone='Asia/Shanghai'), rows=[Row(**{k:getattr(row,k) for k in
                ('product_id','standard_id','store_id','product_name','product_code','standard_name')},
                qty=str(row.qty),total=str(row.total)) for row in c.rows[:20]]).model_dump(mode='json')

def response_schemas(binding, active_names):
    from . import tool_registry as registry
    frozen=registry._pi_binding_shape(binding,profile=registry._binding_profile(binding))
    if type(active_names) not in (list,tuple,set,frozenset):raise ValueError()
    names=set(active_names)
    if len(names)!=len(active_names) or not names or not names<={x['name'] for x in frozen['tools']}:raise ValueError()
    return {entry['name']:registry.resolve_tool(entry['tool_key'],entry['version']).input_model
            for entry in frozen['tools'] if entry['name'] in names}
