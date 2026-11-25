"""GuanJia HTTPS reads only. No write endpoint or persistent credential cache."""
from __future__ import annotations

import gzip
from hashlib import sha256
from io import BytesIO
import json
import ssl
import time
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from backend.app.modules.integrations.guanjia.client import (
    GuanJiaError, PageError, RateLimited, ReadOnlyViolation,
)

from backend.app.modules.integrations.guanjia.api_catalog import CATALOG, read_endpoint

API_BASE = "https://erp.example.invalid"
PATHS = {action: endpoint.path for action, endpoint in CATALOG.items() if endpoint.read_only}
MAX_RESPONSE = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReadOnlyViolation("guanjia_redirect_refused")


class HttpTransport:
    def __init__(self, *, username: str, password: str, tenancy_name: str,
                 base_url: str = API_BASE, timeout: float = 20, page_interval: float = 0.1):
        if base_url.rstrip("/") != API_BASE or urlsplit(base_url).username:
            raise ReadOnlyViolation("guanjia_host_denied")
        if not all(isinstance(v, str) and v and "\r" not in v and "\n" not in v
                   for v in (username, password, tenancy_name)):
            raise GuanJiaError("guanjia_credentials_required")
        self._username, self._password, self._tenancy = username, password, tenancy_name
        self._token, self._tenant_id = None, None
        self._expires_at = 0.0
        self._timeout = min(max(timeout, 1), 30)
        self._interval = min(max(page_interval, 0.1), 5)
        self._opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        self.requests = {"authenticate": 0, "product_page": 0}

    @property
    def scope_identity(self):
        return sha256((API_BASE + "|" + self._tenancy).encode()).hexdigest()

    def _post(self, action: str, body: dict, *, authenticated: bool):
        if action not in PATHS:
            raise ReadOnlyViolation("guanjia_action_denied")
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "Retail-V4-Readonly-Catalog"}
        if authenticated:
            headers.update({"Authorization": "Bearer " + self._token,
                "Abp.TenantId": str(self._tenant_id), "ClientId": self._tenancy,
                "OcelotRequestId": self._tenancy})
        endpoint = CATALOG[action]
        url = API_BASE + endpoint.path
        data = json.dumps(body).encode()
        if endpoint.method == "GET":
            url += "?" + urlencode(body, doseq=True)
            data = None
        request = Request(url, data=data, headers=headers, method=endpoint.method)
        self.requests[action] = self.requests.get(action, 0) + 1
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise PageError("guanjia_response_too_large")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.GzipFile(fileobj=BytesIO(raw)).read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise PageError("guanjia_response_too_large")
        except HTTPError as error:
            if error.code == 429:
                raise RateLimited(1) from None
            # Never include server body, credentials, request headers or URLs.
            raise GuanJiaError(f"guanjia_http_{error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise GuanJiaError("guanjia_connection_failed") from None
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise PageError("guanjia_invalid_json") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise GuanJiaError("guanjia_response_rejected")
        result = payload.get("result")
        if not isinstance(result, (dict, list, str)):
            raise PageError("guanjia_result_invalid")
        return result

    def fetch(self, action: str, params: dict):
        if action not in ("authenticate", "product_page"):
            raise ReadOnlyViolation("guanjia_action_denied")
        if action == "authenticate":
            if params:
                raise ReadOnlyViolation("guanjia_auth_params_denied")
            result = self._post(action, {"tenancyName": self._tenancy,
                "userNameOrEmailAddress": self._username, "password": self._password,
                "rememberClient": False}, authenticated=False)
            if not isinstance(result, dict):
                raise GuanJiaError("guanjia_auth_invalid")
            token, tenant = result.get("accessToken"), result.get("tenantId")
            if (not isinstance(token, str) or not token or any(c in token for c in "\r\n") or
                    type(tenant) is not int or tenant <= 0):
                raise GuanJiaError("guanjia_auth_invalid")
            duration = result.get("expireInSeconds", 3600)
            if type(duration) is not int or duration <= 0:
                raise GuanJiaError("guanjia_auth_invalid")
            self._token, self._tenant_id = token, tenant
            self._expires_at = time.monotonic() + duration - min(30, duration / 2)
            return {"authenticated": True}  # tokens never leave this transport
        if (set(params) != {"skip", "take"} or type(params["skip"]) is not int or params["skip"] < 0
                or type(params["take"]) is not int or not 1 <= params["take"] <= 500):
            raise PageError("guanjia_page_params_invalid")
        if not self._token or time.monotonic() >= self._expires_at:
            self.fetch("authenticate", {})
        time.sleep(self._interval)
        result = self._post(action, {"skipCount": params["skip"], "maxResultCount": params["take"]},
                            authenticated=True)
        if not isinstance(result, dict):
            raise PageError("guanjia_page_invalid")
        items, total = result.get("items"), result.get("totalCount")
        if (not isinstance(items, list) or any(not isinstance(item, dict) for item in items)
                or type(total) is not int or total < 0 or len(items) > params["take"]):
            raise PageError("guanjia_page_invalid")
        return {"items": items, "total": total}


    def query(self, action: str, params: dict):
        endpoint = read_endpoint(action, params)
        if endpoint.group == "auth":
            raise ReadOnlyViolation("guanjia_auth_not_business_query")
        if not self._token or time.monotonic() >= self._expires_at:
            self.fetch("authenticate", {})
        time.sleep(self._interval)
        return self._post(action, params, authenticated=True)


    def structure_once(self, action, params, *, opener, event, recheck):
        """New isolated one-shot path. Old fetch/query never call this method.

        An exact in-memory opener is mandatory while real requests are HOLD.
        There is no path to self._opener, implicit authentication, sleep or retry.
        """
        from backend.app.modules.integrations.guanjia.read_schemas import (
            decode_structure_json, project_structure, JsonNumberLexeme,
        )
        if (type(opener) is not SyntheticStructureOpener or action != 'live_stock'
                or any(type(v) is not bytes for v in (opener.authentication, opener.business))
                or type(opener.authenticated) is not bool
                or opener.auth_failure not in (None, 'http_401', 'http_429', 'timeout', 'redirect', 'connection_failed')
                or opener.business_failure not in (None, 'http_401', 'http_429', 'timeout', 'redirect', 'connection_failed')):
            raise ReadOnlyViolation('structure_synthetic_only')
        read_endpoint(action, params)
        if len(params['ProductIds']) != 1 or any(opener.calls.values()):
            raise ReadOnlyViolation('structure_single_product_only')

        def send(phase):
            recheck()
            event(phase, 'send_intent')
            recheck()  # Intent fsync is another revocation window, not a lease.
            entered = False
            def mark_entered():
                nonlocal entered
                entered = True
                event(phase, 'network_entered')
            try:
                raw = opener.enter(phase, mark_entered)
            except GuanJiaError:
                # A shared opener can reject before invoking the entry hook.
                # A generic failure is not evidence that this run entered I/O.
                event(phase, 'failed' if entered else 'entry_refused')
                raise
            event(phase, 'returned')
            recheck()
            return decode_structure_json(raw)

        if not opener.authenticated:
            auth = send('authentication')
            result = auth.get('result') if type(auth) is dict and auth.get('success') is True else None
            if type(result) is not dict:
                raise GuanJiaError('structure_auth_invalid')
            token, tenant = result.get('accessToken'), result.get('tenantId')
            if (type(token) is not str or not token or len(token) > 8192
                    or any(ord(c) < 32 for c in token)
                    or type(tenant) is not JsonNumberLexeme
                    or not tenant.token.isdigit() or int(tenant.token) <= 0):
                raise GuanJiaError('structure_auth_invalid')
        recheck()
        return project_structure(send('business'))


class SyntheticStructureOpener:
    """Closed in-memory network-boundary simulator, never a real opener.

    Responses and fixed failure codes are trusted test inputs. No callback,
    socket, URL loader or credential lookup exists in this object.
    """
    def __init__(self, *, authentication: bytes, business: bytes,
                 auth_failure=None, business_failure=None, authenticated=False):
        self.authentication = authentication
        self.business = business
        self.auth_failure = auth_failure
        self.business_failure = business_failure
        self.authenticated = authenticated
        self.calls = {'authentication': 0, 'business': 0}
        self._budget_lock = Lock()

    def enter(self, phase, entered):
        # Claim atomically, but release before the callback: test interleaving
        # may invoke another run and must not deadlock or double-consume.
        with self._budget_lock:
            if phase not in self.calls or self.calls[phase]:
                raise GuanJiaError('structure_budget_exhausted')
            self.calls[phase] += 1
        # This hook is AFTER entry into the simulated I/O boundary. If its
        # durable record fails, intent proves only [0,1], never exact zero.
        entered()
        failure = self.auth_failure if phase == 'authentication' else self.business_failure
        if failure is not None:
            raise GuanJiaError('structure_' + failure)
        return self.authentication if phase == 'authentication' else self.business
