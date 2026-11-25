"""One fixed endpoint per tool: pure contracts, never network or authority.

Host callers must perform current dual-subject authorization and audited dispatch.
A prepared request is not an execution receipt or permission grant. Unverified
DTOs remain discoverable as implementation status, not executable tool schemas.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
import json
import hashlib
from typing import Annotated, Any, ClassVar, Generic, TypeVar
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter, RootModel, model_validator
from .api_catalog import ENDPOINTS, Endpoint, read_endpoint
from .client import PAGE_SIZE_LIMIT

# Existing V4 Host policy, NOT a verified upstream maximum.
HOST_PAGE_LIMIT = PAGE_SIZE_LIMIT

class EndpointToolError(ValueError):
    pass

class SalesQueryInput(BaseModel):
    """V3 xzg_client.fetch_sales_bill_page exact request contract."""
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    startDate: Annotated[StrictStr, Field(pattern=r'^\d{4}-\d{2}-\d{2}$',
        description='Source business start date YYYY-MM-DD; source timestamp-filter semantics are unverified, not inferred from billTime.')]
    endDate: Annotated[StrictStr, Field(pattern=r'^\d{4}-\d{2}-\d{2}$',
        description='Source business end date YYYY-MM-DD, not earlier than startDate. Choose any date period explicitly.')]
    skipCount: Annotated[StrictInt, Field(ge=0, description='Source row offset; continue using the actual number returned.')]
    maxResultCount: Annotated[StrictInt, Field(ge=1, le=HOST_PAGE_LIMIT,
        description='Rows for this page; existing Host policy caps pages at 500, not a claim of the upstream maximum. No implicit full-history fetch.')]

    @model_validator(mode='after')
    def ordered_period(self):
        try:
            values=[]
            for value in (self.startDate,self.endDate):
                if value.strip()!=value:raise ValueError
                values.append(date.fromisoformat(value) if len(value)==10
                    else datetime.fromisoformat(value.replace('Z','+00:00')))
            if type(values[0]) is not type(values[1]) or values[1]<values[0]:raise ValueError
            if isinstance(values[0],datetime) and ((values[0].utcoffset() is None)!=(values[1].utcoffset() is None)):raise ValueError
        except (ValueError,TypeError):
            raise ValueError('source_date_range_invalid') from None
        return self

class SalesLinesInput(BaseModel):
    """One posted ERP-detail day/page; compose dates freely across calls."""
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    day: Annotated[StrictStr,Field(pattern=r'^\d{4}-\d{2}-\d{2}$',
        description='Source bill date YYYY-MM-DD. This verified adapter covers posted billState=12 only, not every state. Query multiple dates by composing calls.')]
    skipCount: Annotated[StrictInt,Field(ge=0,le=100000,description='Source row offset; advance by actual returned rows.')]
    maxResultCount: Annotated[StrictInt,Field(ge=1,le=100,
        description='One-page size; existing sales-history Host policy1..100, not a verified upstream maximum.')]

    @model_validator(mode='after')
    def real_day(self):
        date.fromisoformat(self.day)
        return self

@dataclass(frozen=True)
class SalesReadScope:
    """Trusted Host selection/clock, not a JSON tool argument or authority."""
    shop_id: str
    now: datetime


@dataclass(frozen=True)
class EndpointToolDescriptor:
    action: str
    key: str
    name: str
    endpoint: Endpoint
    requester_permissions: tuple[str,...]
    input_model: type[BaseModel] | None
    evidence: str

    @property
    def request_ready(self) -> bool:
        return self.input_model is not None

    def metadata(self) -> dict[str,Any]:
        return {'tool_key':self.key,'name':self.name,'version':'1','action':self.action,
            'description':_DESCRIPTIONS.get(self.action,self.action+': request DTO is unverified and not executable.'),
            'method':self.endpoint.method,'path':self.endpoint.path,
            'side_effect':'read_only','required_requester_permissions':list(self.requester_permissions),
            'input_schema':self.input_model.model_json_schema() if self.input_model else None,
            'output_schema':response_schema(self.action),
            'request_ready':self.request_ready,'response_projection_ready':self.action in ('sales_lines','sales_query'),
            'source_semantics_verified':False,
            'runtime_registered':False,
            'host_page_limit':(self.input_model.model_json_schema()['properties']['maxResultCount']['maximum'] if self.input_model else None),
            'upstream_page_limit_verified':False,
            'status':'request_contract_ready' if self.request_ready else 'request_dto_unverified',
            'evidence':self.evidence}

_DESCRIPTIONS={
    'sales_query':'Query one sales-bill-list page for an explicit business period; caller controls paging, not an automatic all-history fetch.',
    'sales_lines':'Read one posted ERP sales-detail page for a chosen source bill day. Verified adapter fixes billState=12, not all bill states; compose days/pages as needed.',
}

_MODELS={'sales_query':SalesQueryInput,'sales_lines':SalesLinesInput}
_EVIDENCE={'sales_lines':'sales_history_transport.history_body(erp_detail) exact request and erp_sales_detail_contract.parse_page posted daily response; not all states', 'sales_query':'V3 src/sync/xzg_client.py fetch_sales_bill_page: startDate/endDate/skipCount/maxResultCount; V4 api_catalog read validation'}
_DESCRIPTORS=tuple(EndpointToolDescriptor(e.action,'guanjia.api.'+e.action,
    'v4_guanjia_api_'+e.action+'_v1',e,
    ('guanjia.'+e.group+'.read',) + (() if e.action=='live_stock' else ('guanjia.company.read',)),
    _MODELS.get(e.action),_EVIDENCE.get(e.action,'Full exact request DTO is not yet established; static API inventory is not sufficient.'))
    for e in ENDPOINTS if e.read_only and e.group!='auth')

def endpoint_descriptors() -> tuple[EndpointToolDescriptor,...]:
    return _DESCRIPTORS

def describe_tool(action: str) -> EndpointToolDescriptor:
    for descriptor in _DESCRIPTORS:
        if descriptor.action==action:return descriptor
    raise EndpointToolError('endpoint_tool_unknown_or_not_readonly')

def request_schemas() -> list[dict[str,Any]]:
    """Only verified request schemas; not a claim that Host/runtime is wired."""
    return [d.metadata() for d in _DESCRIPTORS if d.request_ready]

@dataclass(frozen=True)
class PreparedRead:
    """Immutable preparation only, no credentials, no transport and no receipt."""
    descriptor: EndpointToolDescriptor
    parameters_json: str

    @property
    def parameters(self) -> dict[str,Any]:
        return json.loads(self.parameters_json)

def prepare_read(action: str, arguments: object, *, scope: SalesReadScope | None = None) -> PreparedRead:
    descriptor=describe_tool(action)
    if descriptor.input_model is None:
        raise EndpointToolError('endpoint_request_dto_unverified')
    if type(arguments) is not dict:
        raise EndpointToolError('endpoint_arguments_invalid')
    # Avoid schema coercion and avoid arbitrary kwargs/route/context overrides.
    value=descriptor.input_model.model_validate(arguments).model_dump(mode='json')
    if action=='sales_lines':
        if type(scope) is not SalesReadScope:
            raise EndpointToolError('endpoint_trusted_shop_scope_required')
        from .sales_history_transport import history_body
        day=date.fromisoformat(value['day'])
        value=history_body(operation='erp_detail',store_id=scope.shop_id,start=day,end=day,
            now=scope.now,skip=value['skipCount'],take=value['maxResultCount'])
    if read_endpoint(descriptor.action,value)!=descriptor.endpoint:
        raise EndpointToolError('endpoint_contract_changed')
    return PreparedRead(descriptor,json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))


def available_tools(permission_set: frozenset[str]) -> list[dict[str,Any]]:
    """Display-only candidates; caller-supplied sets NEVER authorize dispatch."""
    if type(permission_set) is not frozenset or any(type(p) is not str for p in permission_set):
        raise EndpointToolError('endpoint_permission_projection_invalid')
    return [d.metadata() for d in _DESCRIPTORS if d.request_ready
            and set(d.requester_permissions)<=permission_set]


class ReadResultEnvelope(BaseModel):
    """Typed transport shell; actual fields need an audited Host projection.

    This formatter supplies no ACL, row filtering or truth certification.
    No endpoint currently gains runtime registration from this module.
    """
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)
    tool_key: StrictStr
    source_receipt_ref: Annotated[StrictStr,Field(pattern=r'^sha256:[0-9a-f]{64}$')]
    projection_schema_sha256: Annotated[StrictStr,Field(pattern=r'^[0-9a-f]{64}$')]
    projected_data: dict[str,Any]


def pack_projected_result(action: str, *, source_receipt_ref: str,
                          projection: BaseModel) -> ReadResultEnvelope:
    """Host-only composition seam. Raw source dictionaries are not accepted.

    The future endpoint adapter owns a named, extra-forbid output model and
    current field-disclosure checks. A Pydantic type is not itself authority.
    """
    descriptor=describe_tool(action)
    if descriptor.input_model is None:
        raise EndpointToolError('endpoint_request_dto_unverified')
    if not isinstance(projection,BaseModel) or projection.model_config.get('extra')!='forbid':
        raise EndpointToolError('endpoint_typed_projection_required')
    schema=type(projection).model_json_schema()
    schema_hash=hashlib.sha256(json.dumps(schema,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return ReadResultEnvelope(tool_key=descriptor.key,source_receipt_ref=source_receipt_ref,
        projection_schema_sha256=schema_hash,projected_data=projection.model_dump(mode='json'))


def parse_sales_lines_page(prepared: PreparedRead, raw: bytes):
    """Existing strict posted-detail DTO; no raw-row disclosure or new parser.

    Source field semantics remain candidate-level. This does not attest a
    user-session permission or that every page of a report was read.
    """
    if type(prepared) is not PreparedRead or prepared.descriptor != describe_tool('sales_lines'):
        raise EndpointToolError('endpoint_sales_lines_prepared_required')
    body=prepared.parameters
    # Reconstruct the exact adapter-produced request, not an arbitrary dict in
    # a caller-constructed PreparedRead. The source parser enforces one shop/day.
    from .sales_history_transport import history_body
    from .erp_sales_detail_contract import parse_page
    day=date.fromisoformat(body['startDate'])
    # For result decoding there is no new current-time request. Validate the
    # frozen request against a clock at the end of its source day.
    from datetime import timezone
    expected=history_body(operation='erp_detail',store_id=body['shopIds'][0],start=day,end=day,
        now=datetime.combine(day,datetime.max.time(),tzinfo=timezone.utc),
        skip=body['skipCount'],take=body['maxResultCount'])
    if body!=expected:
        raise EndpointToolError('endpoint_sales_lines_request_changed')
    return parse_page(raw,expected_shop_id=body['shopIds'][0],day=body['startDate'],
        skip=body['skipCount'],take=body['maxResultCount'])


def sales_lines_progress(page) -> dict[str,Any]:
    """Single-page progress only. A tail page is not proof of prefix coverage.

    Host must verify continuous same-source receipts from offset0 to total to
    establish report completeness; this function never makes that claim.
    """
    from .erp_sales_detail_contract import ErpSalesDetailPage
    if type(page) is not ErpSalesDetailPage:
        raise EndpointToolError('endpoint_sales_lines_page_required')
    end=page.skip+len(page.rows)
    return {'source_state':'posted_billState_12','source_day':page.day,
        'returned_count':len(page.rows),'total_count':page.total_count,
        'next_offset':end if end<page.total_count else None,
        'page_exhausted':end>=page.total_count,'has_more':end<page.total_count,
        'all_pages_verified':False,'source_response_sha256':page.response_sha256}


# Re-export the existing continuity/multiplicity checker, not a second planner.
from .erp_sales_detail_contract import assemble_candidate as assemble_sales_lines_pages


@dataclass(frozen=True)
class SalesBillRow:
    source_ordinal: int
    bill_id: str
    bill_code: str
    bill_date: str
    bill_time: str
    bill_type: int
    bill_state: int
    shop_id: str
    warehouse_id: str
    quantity: Decimal
    total: Decimal

@dataclass(frozen=True)
class SalesBillPage:
    start: str
    end: str
    skip: int
    take: int
    total_count: int
    response_sha256: str
    rows: tuple[SalesBillRow,...]


def _validate_bill_page(page: SalesBillPage) -> SalesBillPage:
    """One structural/typed invariant for decoded and reassembled pages."""
    from .sales_ranking_contract import MAX_ROWS,_integer,_decimal
    from .retail_sales_detail_contract import _id,_text,_timestamp,_bill_day
    try:
        if type(page) is not SalesBillPage:raise ValueError
        args=SalesQueryInput.model_validate(dict(startDate=page.start,endDate=page.end,
            skipCount=page.skip,maxResultCount=page.take))
        total=_integer(page.total_count,0,MAX_ROWS)
        if (type(page.response_sha256) is not str or len(page.response_sha256)!=64
                or any(c not in '0123456789abcdef' for c in page.response_sha256)
                or page.skip>total or type(page.rows) is not tuple
                or len(page.rows)!=min(page.take,total-page.skip)):raise ValueError
        for index,row in enumerate(page.rows):
            if type(row) is not SalesBillRow or type(row.source_ordinal) is not int or row.source_ordinal!=page.skip+index:raise ValueError
            for value in (row.bill_id,row.shop_id,row.warehouse_id):_id(value)
            for value in (row.bill_code,row.bill_date,row.bill_time):_text(value,required=True)
            if not _bill_day(args.startDate)<=_bill_day(row.bill_date)<=_bill_day(args.endDate):raise ValueError
            _timestamp(row.bill_time)
            _integer(row.bill_type,0,2147483647);_integer(row.bill_state,0,2147483647)
            for value in (row.quantity,row.total):
                if type(value) is not Decimal:raise ValueError
                _decimal(value)
        return page
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        raise EndpointToolError('endpoint_sales_query_page_invalid') from None


def response_schema(action: str) -> dict[str,Any] | None:
    from pydantic import TypeAdapter
    if action=='sales_query':return TypeAdapter(SalesBillPage).json_schema(mode='serialization')
    if action=='sales_lines':
        from .erp_sales_detail_contract import ErpSalesDetailPage
        return TypeAdapter(ErpSalesDetailPage).json_schema(mode='serialization')
    return None


def parse_sales_query_page(prepared: PreparedRead, raw: bytes) -> SalesBillPage:
    """Source bill-list fields only; no customer/cost/operator/free-text data.

    Source dates/states are preserved, not translated to a business settlement
    verdict. The query is company-wide under the existing company-read grant.
    A page hash/pin is not authority, an atomic snapshot or complete-report proof.
    """
    from .sales_ranking_contract import MAX_BYTES,MAX_ROWS,_object,_bounded_json,_decimal,_integer
    try:
        if type(prepared) is not PreparedRead or prepared.descriptor != describe_tool('sales_query'):
            raise ValueError
        args=SalesQueryInput.model_validate(prepared.parameters)
        if type(raw) is not bytes or not 0<len(raw)<=MAX_BYTES:raise ValueError
        def no_constant(_):raise ValueError
        root=json.loads(raw.decode('utf-8'),object_pairs_hook=_object,parse_float=_decimal,parse_constant=no_constant)
        _bounded_json(root)
        if (type(root) is not dict or root.get('success') is not True or root.get('error') is not None
                or root.get('unAuthorizedRequest',False) is not False):raise ValueError
        result=root['result']
        if type(result) is not dict or set(result)!={'sum','pageTotal','columns','totalCount','items'}:raise ValueError
        if type(result['sum']) is not dict or type(result['pageTotal']) is not int or result['columns'] is not None:raise ValueError
        total=_integer(result['totalCount'],0,MAX_ROWS)
        items=result['items']
        if type(items) is not list or args.skipCount>total or len(items)!=min(args.maxResultCount,total-args.skipCount):raise ValueError
        rows=[]
        for ordinal,row in enumerate(items,args.skipCount):
            if type(row) is not dict:raise ValueError
            for key in ('qty','total'):
                if type(row[key]) not in (int,Decimal):raise ValueError
            rows.append(SalesBillRow(ordinal,row['billId'],row['billCode'],row['billDate'],row['billTime'],
                row['billType'],row['billState'],row['shopId'],row['whsId'],_decimal(row['qty']),_decimal(row['total'])))
        return _validate_bill_page(SalesBillPage(args.startDate,args.endDate,args.skipCount,args.maxResultCount,total,
            hashlib.sha256(raw).hexdigest(),tuple(rows)))
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        raise EndpointToolError('endpoint_sales_query_response_invalid') from None


def assemble_sales_query_pages(pages: tuple[SalesBillPage,...]) -> tuple[SalesBillRow,...]:
    """Contiguous coverage only, never deduplicate identical bills or rows.

    Requires source total unchanged across pages, not a claim of atomic source
    snapshot. Host must separately bind/retain original response receipts.
    """
    try:
        if type(pages) is not tuple or not pages:raise ValueError
        first=_validate_bill_page(pages[0]);rows=[]
        for index,page in enumerate(pages):
            _validate_bill_page(page)
            if (type(page) is not SalesBillPage or page.skip!=len(rows)
                    or (page.start,page.end,page.take,page.total_count)!=(first.start,first.end,first.take,first.total_count)
                    or index and len(rows)>=first.total_count
                    or len(page.rows)!=min(page.take,page.total_count-page.skip)
                    or any(row.source_ordinal!=page.skip+i for i,row in enumerate(page.rows))):raise ValueError
            rows.extend(page.rows)
        if len(rows)!=first.total_count:raise ValueError
        return tuple(rows)
    except (ValueError,TypeError,AttributeError):
        raise EndpointToolError('endpoint_sales_query_pages_incomplete') from None



def _page_type(action: str):
    if action=='sales_query':return SalesBillPage
    if action=='sales_lines':
        from .erp_sales_detail_contract import ErpSalesDetailPage
        return ErpSalesDetailPage
    raise EndpointToolError('endpoint_output_model_unimplemented')


def _canonical_output(action: str, value: Any):
    """Reuse the typed page codec; reject coercion, extra fields and bad facts."""
    page_type=_page_type(action)
    try:
        if type(value) is page_type:
            page=value
        elif type(value) is dict:
            def decimal_only(obj):
                if type(obj) is Decimal:return str(obj)
                raise TypeError
            encoded=json.dumps(value,default=decimal_only,ensure_ascii=False,
                sort_keys=True,separators=(',',':'),allow_nan=False)
            adapter=TypeAdapter(page_type)
            page=adapter.validate_json(encoded,strict=True)
            canonical=adapter.dump_python(page,mode='json')
            # Pydantic dataclasses otherwise ignore extra keys. Do not silently
            # drop private fields, accept alternate encodings or coerce facts.
            if encoded!=json.dumps(canonical,ensure_ascii=False,sort_keys=True,
                                   separators=(',',':'),allow_nan=False):raise ValueError
        else:raise ValueError
        if action=='sales_query':_validate_bill_page(page)
        else:
            from .erp_sales_detail_contract import _validate_page
            _validate_page(page)
        return page
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        raise EndpointToolError('endpoint_output_invalid') from None


_PageT=TypeVar('_PageT')
class _EndpointOutput(RootModel[_PageT], Generic[_PageT]):
    """BaseModel-shaped registry adapter with no new wire wrapper/schema."""
    model_config=ConfigDict(strict=True,frozen=True)
    ACTION: ClassVar[str]

    @model_validator(mode='before')
    @classmethod
    def _strict_page(cls,value):
        return _canonical_output(cls.ACTION,value)

    @classmethod
    def model_json_schema(cls,*args,**kwargs):
        # Registry and Node bind the existing serialization schema, not a
        # second RootModel title/$defs arrangement or a {root: ...} envelope.
        return response_schema(cls.ACTION)


class SalesQueryOutput(_EndpointOutput[SalesBillPage]):
    ACTION: ClassVar[str]='sales_query'

from .erp_sales_detail_contract import ErpSalesDetailPage as _ErpSalesDetailPage
class SalesLinesOutput(_EndpointOutput[_ErpSalesDetailPage]):
    ACTION: ClassVar[str]='sales_lines'


def output_model(action: str) -> type[BaseModel]:
    if action=='sales_query':return SalesQueryOutput
    if action=='sales_lines':return SalesLinesOutput
    raise EndpointToolError('endpoint_output_model_unimplemented')


def endpoint_definition(action: str) -> dict[str,Any]:
    """Executable contract inputs for the Host's fixed-action adapter.

    Does not register, authorize, dispatch or invent an Agent permission. The
    Host binds the current principals/tenant and its registered handler here.
    """
    descriptor=describe_tool(action)
    if descriptor.input_model is None:raise EndpointToolError('endpoint_request_dto_unverified')
    return {'tool_key':descriptor.key,'name':descriptor.name,'version':'1',
        'description':descriptor.metadata()['description'],
        'input_model':descriptor.input_model,'output_model':output_model(action),
        'side_effect':'read_only','read_only':True,
        'requester_permissions':descriptor.requester_permissions,
        'action':descriptor.action,'method':descriptor.endpoint.method,'path':descriptor.endpoint.path}
