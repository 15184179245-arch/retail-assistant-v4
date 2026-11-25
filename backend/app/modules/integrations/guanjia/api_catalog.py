"""V3 interface contracts, rewritten as a V4 allowlist. No legacy code/credentials.

Read contracts are available to the permission/audit service. The two historical
write contracts are recorded for completeness and are always disabled here.
"""
from dataclasses import dataclass
from datetime import datetime
from backend.app.modules.integrations.guanjia.client import ReadOnlyViolation, PageError


@dataclass(frozen=True)
class Endpoint:
    action: str
    method: str
    path: str
    group: str
    required: tuple[str, ...] = ()
    read_only: bool = True


ENDPOINTS = (
    Endpoint('authenticate','POST','/api/TokenAuth/Authenticate','auth'),
    Endpoint('product_page','POST','/api/services/app/BaseProduct/GetAll','catalog',('skipCount','maxResultCount')),
    Endpoint('category_flat','POST','/api/services/app/V1/Erp/Selector/Product/Category/GetAll','catalog',('skipCount','maxResultCount','keyword')),
    Endpoint('category_tree','GET','/api/services/app/V1/Erp/Selector/Product/Category/GetTreeNew','catalog',('skipCount','maxResultCount')),
    Endpoint('product_get','GET','/api/services/app/BaseProduct/Get','catalog',('id',)),
    Endpoint('product_standards','POST','/api/services/app/BaseProduct/GetAllProductStandard4Tc','catalog',('productId',)),
    Endpoint('selector_standards','POST','/api/services/app/V1/Erp/Selector/Product/Standard/GetAll','catalog'),
    Endpoint('stock_condition','POST','/api/services/app/V1/Erp/Report/Stock/StockCondition/Get','inventory'),
    Endpoint('stock_condition_export','POST','/api/services/app/V1/Erp/Report/Stock/StockCondition/Export','inventory'),
    Endpoint('live_stock','POST','/api/services/app/V1/Erp/Stock/GetByProductIds','inventory',('ProductIds','whsId','shopId')),
    Endpoint('live_prices','POST','/api/services/app/V1/Erp/Selector/Price/GetAll','pricing',('ProductIds','billModule')),
    Endpoint('sales_query','POST','/api/services/app/V1/Erp/Report/SaleBill/Sale/Query','sales',('startDate','endDate')),
    Endpoint('sales_export','POST','/api/services/app/V1/Erp/Report/SaleBill/ExportSaleBillList','sales',('startDate','endDate')),
    Endpoint('sales_lines','POST','/api/services/app/V1/Erp/Report/SaleBill/Summary/Detail/Get','sales'),
    Endpoint('sales_product_detail','POST','/api/services/app/ReportSaleAnalysis/GetAllProductSaleDetail','sales'),
    Endpoint('sales_returns','POST','/api/services/app/V1/Erp/Report/SaleBill/SaleBack/Query','sales'),
    Endpoint('gross_profit','POST','/api/services/app/V1/Erp/Report/SaleBill/GrossProfit/Get','finance'),
    Endpoint('stock_changes','POST','/api/services/app/V1/Erp/Report/Stock/StockChangeSummary/Get','inventory'),
    Endpoint('customers','POST','/api/services/app/V1/Erp/Selector/Customer/GetAll','customers'),
    Endpoint('customer_receivables','POST','/api/services/app/V1/Erp/Report/Capital/Customer/Rec/Get','finance'),
    Endpoint('customer_balances','POST','/api/services/app/V1/Erp/Report/Capital/Customer/CustomerARTotalPreARTotal/Get','finance'),
    Endpoint('product_update','PUT','/api/services/app/BaseProduct/Update','writes',read_only=False),
    Endpoint('image_upload','POST','/api/services/app/CommonUploadImg/UpLoadImg','writes',read_only=False),
)
CATALOG = {endpoint.action: endpoint for endpoint in ENDPOINTS}


def read_endpoint(action, params):
    endpoint = CATALOG.get(action)
    if endpoint is None or not endpoint.read_only:
        raise ReadOnlyViolation('guanjia_write_or_unknown_action_denied')
    if not isinstance(params, dict) or any(key not in params for key in endpoint.required):
        raise PageError('guanjia_required_parameters_missing')
    if any(params[key] in (None, '', []) for key in endpoint.required
           if not (action == 'category_flat' and key == 'keyword')):
        raise PageError('guanjia_required_parameters_missing')
    for key in ('skipCount','maxResultCount'):
        if key in params and (type(params[key]) is not int or params[key] < (1 if key == 'maxResultCount' else 0)):
            raise PageError('guanjia_pagination_invalid')
    if params.get('maxResultCount', 1) > 500:
        raise PageError('guanjia_pagination_invalid')
    if action == 'product_page' and set(params) != {'skipCount','maxResultCount'}:
        raise PageError('guanjia_product_parameters_invalid')
    if action in ('category_flat','category_tree'):
        required_keys = {'skipCount','maxResultCount','keyword'} if action == 'category_flat' else {'skipCount','maxResultCount'}
        if set(params) != required_keys:
            raise PageError('guanjia_category_parameters_invalid')
        if action == 'category_flat' and (type(params['keyword']) is not str or len(params['keyword']) > 200
                or any(ord(char) < 32 for char in params['keyword'])):
            raise PageError('guanjia_category_parameters_invalid')
    if 'ProductIds' in endpoint.required:
        values = params.get('ProductIds')
        if not isinstance(values, list) or not 1 <= len(values) <= 500 or any(not isinstance(v,str) or not v for v in values):
            raise PageError('guanjia_product_ids_invalid')
    if action == 'live_prices' and params['billModule'] != 2:
        raise PageError('guanjia_price_module_invalid')
    if 'startDate' in endpoint.required:
        try:
            start,end = (datetime.fromisoformat(params[k].replace('Z','+00:00')) for k in ('startDate','endDate'))
            if end < start: raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PageError('guanjia_date_range_invalid') from None
    return endpoint
