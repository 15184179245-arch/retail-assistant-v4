"""Host-only fixed ranking HTTPS, using the existing bounded one-shot worker.

No URL, path, headers, arbitrary filters or token can be supplied at send time.
The inherited worker rejects redirects, encoding and ambiguous framing and
terminates the child on its total deadline. No retries exist.
"""
from datetime import datetime
import json
import os
import re
import threading
from zoneinfo import ZoneInfo

from .live_once_transport import _OneShotHttpsTransport, OnceTransportError, _text

HOST = 'erp.example.invalid'
RANKING_PATH = '/api/services/app/V1/Erp/Report/SaleBill/Ranking/Get'
BEIJING = ZoneInfo('Asia/Shanghai')
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z', re.ASCII)


def ranking_body(*, store_id, as_of, now, skip, take):
    if (type(store_id) is not str or not _ID.fullmatch(store_id) or
            type(as_of) is not datetime or as_of.tzinfo is None or
            type(now) is not datetime or now.tzinfo is None or
            type(skip) is not int or not 0 <= skip < 100_000 or
            type(take) is not int or not 1 <= take <= 500):
        raise OnceTransportError('sales_ranking_materials_invalid')
    instant, current = as_of.astimezone(BEIJING), now.astimezone(BEIJING)
    if (instant > current or (instant.year, instant.month) != (current.year, current.month)
            or instant.microsecond):
        raise OnceTransportError('sales_ranking_materials_invalid')
    start = instant.replace(day=1, hour=0, minute=0, second=0)
    return {'startDate': start.strftime('%Y-%m-%d'),
            'endDate': instant.strftime('%Y-%m-%d'),
            'shopIds': [store_id], 'statisticsTypeList': [1],
            'skipCount': skip, 'maxResultCount': take}


class RankingTransport(_OneShotHttpsTransport):
    """Protected construction only; inherited send is consumed even on failure."""
    def __init__(self, *, token, tenant_id, tenancy_name, store_id, as_of, now, skip, take):
        body = ranking_body(store_id=store_id, as_of=as_of, now=now, skip=skip, take=take)
        if (not _text(token) or not token.isascii() or any(ord(c) < 33 for c in token) or
                not _text(tenancy_name) or not tenancy_name.isascii() or
                type(tenant_id) is not int or not 0 < tenant_id <= 2**63 - 1):
            raise OnceTransportError('sales_ranking_materials_invalid')
        self._path = RANKING_PATH
        self._body = json.dumps(body, separators=(',', ':')).encode('ascii')
        self._headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                         'Accept-Encoding': 'identity', 'Connection': 'close',
                         'User-Agent': 'Retail-V4-Sales-Readonly',
                         'Authorization': 'Bearer ' + token, 'Abp.TenantId': str(tenant_id),
                         'ClientId': tenancy_name, 'OcelotRequestId': tenancy_name}
        self._deadline = 10.0
        self._lock = threading.Lock()
        self._consumed = False
        self._owner_pid = os.getpid()

    def descriptor(self):
        """Fresh non-secret descriptor decoded from the exact outgoing bytes.

        The Host persists this before send. No date/clock/body regeneration may
        occur between descriptor creation and the inherited one-shot dispatch.
        """
        if self._consumed:
            raise OnceTransportError('sales_ranking_materials_invalid')
        return {'action': 'ranking', 'body': json.loads(self._body)}
