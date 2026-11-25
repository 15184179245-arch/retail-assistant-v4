"""Unwired Host-only, fixed retail/posted-ERP detail HTTPS primitives.

Construction validates material shape, never authority. A separate Host must
reserve a fresh operation, persist intent/entered and recheck permission before
each one-shot dispatch. Unknown outcomes must not be replayed.
"""
from datetime import date, datetime
import json
import os
import re
import threading
from zoneinfo import ZoneInfo

from .live_once_transport import _OneShotHttpsTransport, OnceTransportError

HOST = 'erp.example.invalid'
RETAIL_DETAIL_PATH = '/api/services/app/ReportSaleAnalysis/GetAllProductSaleDetail'
ERP_DETAIL_PATH = '/api/services/app/V1/Erp/Report/SaleBill/Summary/Detail/Get'
_PATHS = {'retail_detail': RETAIL_DETAIL_PATH, 'erp_detail': ERP_DETAIL_PATH}
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z', re.ASCII)
_BEIJING = ZoneInfo('Asia/Shanghai')


def _deny():
    raise OnceTransportError('sales_history_materials_invalid') from None


def history_body(*, operation, store_id, start, end, now, skip, take):
    """Fixed defaults only. Year one is a protocol probe, not server acceptance."""
    try:
        if (type(operation) is not str or operation not in _PATHS or
                type(store_id) is not str or _ID.fullmatch(store_id) is None or
                type(start) is not date or type(end) is not date or
                type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None or
                type(skip) is not int or not 0 <= skip <= 100000 or
                type(take) is not int or not 1 <= take <= 100 or
                not start <= end <= now.astimezone(_BEIJING).date()):
            _deny()
        # isoformat retains zero-padding for years below 1000 on every platform.
        begin, finish = start.isoformat(), end.isoformat()
        if operation == 'retail_detail':
            return dict(beginDate=begin, endDate=finish, storeIdList=[store_id],
                billType='0', productId='0', productName='', memberUserId='0',
                memberKeywords='', businessManId='0', businessManName='',
                creatorId='0', creatorName='', billRemark='', productCategoryIds=None,
                brandIds=None, cardTypeIds=[], storeIdListData=[],
                current=skip // take + 1, skipCount=skip, maxResultCount=take)
        return dict(startDate=begin, endDate=finish, shopIds=[store_id], businessType='0',
            billCode='', billRemark='', comment='', orderProductRemark='', productIds=[],
            companyIds=None, billState=12, handlerIds=None, erpBillBusinessType=None,
            productCategoryIds=None, skipCount=skip, maxResultCount=take, isGift=None,
            whsIds=None, brandIds=None, dueStartDate=None, dueEndDate=None,
            productionEndDate=None, productionStartDate=None, batchNo=None, redWord=None)
    except Exception:
        _deny()


def _header_material(value, maximum):
    return (type(value) is str and 0 < len(value) <= maximum and
            all(33 <= ord(ch) <= 126 for ch in value))


class SalesHistoryTransport(_OneShotHttpsTransport):
    """One source/page per instance; inherited TLS, 1MiB bound, no retry.

The main-thread single-threaded fork restriction and 30s total deadline include
DNS/TLS/write/read. Headers/materials are erased after consumption, including
gate rejection. No runtime, file loading, authentication or scheduling here.
"""

    def __init__(self, *, operation, token, tenant_id, tenancy_name,
                 store_id, start, end, now, skip, take):
        body = history_body(operation=operation, store_id=store_id, start=start,
                            end=end, now=now, skip=skip, take=take)
        if (not _header_material(token, 4096) or not _header_material(tenancy_name, 4096) or
                type(tenant_id) is not int or not 0 < tenant_id <= 2**63 - 1):
            _deny()
        self._history_action = operation
        self._path = _PATHS[operation]
        self._body = json.dumps(body, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        self._headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
            'Accept-Encoding': 'identity', 'Connection': 'close',
            'User-Agent': 'Retail-V4-Sales-History-Readonly',
            'Authorization': 'Bearer ' + token, 'Abp.TenantId': str(tenant_id),
            'ClientId': tenancy_name, 'OcelotRequestId': tenancy_name}
        self._deadline = 30.0
        self._lock = threading.Lock()
        self._consumed = False
        self._owner_pid = os.getpid()
        # Unlike legacy callers, history journaling retains complete HTTP
        # failure outcomes. This confers no authority and never retries.
        self._capture_http_status = True

    def descriptor(self):
        """Fresh non-secret copy of the exact frozen outgoing body, not a grant."""
        if os.getpid() != self._owner_pid or self._consumed:
            _deny()
        return {'method': 'POST', 'action': self._history_action,
                'body': json.loads(self._body)}
