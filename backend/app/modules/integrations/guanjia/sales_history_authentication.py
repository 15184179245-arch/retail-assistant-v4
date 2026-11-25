"""Fixed Host-only one-shot history authentication, with no material loading."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re

from .live_host import _authentication
from .live_once_transport import _OneShotHttpsTransport, _text, MAX_RESPONSE_BYTES
from .sales_history_batch import AuthenticatedRead
from .sales_history_collector import ReadCredentials
from .sales_history_transport import _header_material


class SalesHistoryAuthenticationError(ValueError):
    """No source text or authentication materials in diagnostics."""


def _deny():
    raise SalesHistoryAuthenticationError('sales_history_authentication_hold') from None


@dataclass(frozen=True, repr=False)
class AuthenticationMaterial:
    username: str
    password: str
    tenancy_name: str
    tenant_id: int
    shop_id: str

    def __repr__(self):
        return 'AuthenticationMaterial(<protected>)'


def authenticate_once(material, gate, *, _clock=lambda: datetime.now(timezone.utc),
                      _transport_factory=_OneShotHttpsTransport):
    """Pass the exact durable Host gate to one fixed transport; never retry.

Complete HTTP-200 responses with rejected auth schemas still return only their
non-sensitive metadata and no credentials. Batch persists those known outcomes
before rejecting. Network exceptions have no fabricated returned receipt.
"""
    try:
        if (type(material) is not AuthenticationMaterial or not callable(gate) or not callable(_clock) or
                not _text(material.username) or not _text(material.password) or
                not _header_material(material.tenancy_name, 4096) or
                type(material.tenant_id) is not int or not 0 < material.tenant_id <= 2**63-1 or
                type(material.shop_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', material.shop_id) is None):
            _deny()
        started = _clock()
        if type(started) is not datetime or started.tzinfo is None or started.utcoffset() is None:
            _deny()
        transport = _transport_factory('authenticate', username=material.username,
            password=material.password, tenancy_name=material.tenancy_name, deadline_seconds=30,
            _capture_http_status=True)
        response = transport._send_after_host_gate(gate)
        received = _clock()
        if (type(received) is not datetime or received.tzinfo is None or received.utcoffset() is None or
                type(response.raw) is not bytes or not 0 <= len(response.raw) <= MAX_RESPONSE_BYTES or
                type(response.http_status) is not int or not 100 <= response.http_status <= 599):
            _deny()
        credentials = None
        try:
            if response.http_status != 200 or response.json_body is not True or received < started:
                raise ValueError()
            token = _authentication(response.raw, material.tenant_id)
            # _authentication has already rejected duplicate keys and non-JSON
            # primitives; this second decode only retrieves the validated TTL.
            lifetime = json.loads(response.raw)['result'].get('expireInSeconds')
            if type(lifetime) is int and 1 <= lifetime <= 86400:
                expires = started+timedelta(seconds=lifetime)
                if expires > received:
                    credentials = ReadCredentials(token, material.tenant_id, material.tenancy_name,
                                                  material.shop_id, expires)
        except Exception:
            credentials = None
        return AuthenticatedRead(credentials, sha256(response.raw).hexdigest(), len(response.raw),
                                 response.http_status, received.isoformat())
    except Exception:
        _deny()
