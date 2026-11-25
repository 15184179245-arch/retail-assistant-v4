"""Unwired L2c2 Host kernel, exercised only with anonymous, offline materials.

No loader, CLI, endpoint, credential cache, or disclosure adapter exists here.
Protected in-process configuration is not authority: the existing sx1/Agent,
mapping and fence helpers revalidate every use. A dedicated single-thread Host
and real source/tenant/shop semantics require separate acceptance. Never erase
consumed ledgers to retry. Even committed structures remain internal only.
"""
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import re

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import SingletonThreadPool, StaticPool

from backend.app.workers import guanjia_precheck as host
from backend.app.modules.integrations.guanjia.live_call_ledger import HostPermitBinding, LiveCallLedger
from backend.app.modules.integrations.guanjia.live_once_transport import _OneShotHttpsTransport
from backend.app.modules.integrations.guanjia.live_structure import inspect_response

_OPERATIONS = ('authenticate', 'live_prices', 'live_stock')
_BASE = 'https://erp.example.invalid'
_TOKEN = re.compile(r'[A-Za-z0-9._~+/-]+=*\Z', re.ASCII)
_AUTH_PROFILE_TEXT = frozenset({'userName', 'phoneNumber', 'tenancyName', 'companyName'})
_AUTH_PROFILE_BOOL = frozenset({'isVisibleCostPrice', 'allowNoStockSale',
    'isEnableSerialNumber', 'isVisibleWholePrice', 'isVisibleDeliveryPrice',
    'isHaveSupplyChain', 'isDockingSupplyChain'})
_AUTH_PROFILE_INT = frozenset({'userAttribute', 'companyVersion', 'scType'})


@dataclass(frozen=True, repr=False)
class LivePermit:
    scope: host.read_service.SourceScope
    product: host.read_service.ConfirmedProduct
    agent_subject_id: str
    permission_epoch: int
    binding_evidence_sha256: str
    time_evidence_sha256: str
    expires_at: datetime
    expected_tenant_id: int
    price_evidence_sha256: str | None = None
    stock_evidence_sha256: str | None = None


@dataclass(frozen=True, repr=False)
class ProtectedLiveRuntime:
    """Host-only anonymous test materials; never deserialize model/CLI inputs."""
    engine: Engine = field(repr=False)
    employee_token: str = field(repr=False)
    permit: LivePermit = field(repr=False)
    ledger_directory: Path = field(repr=False)
    nonce_sha256: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    tenancy_name: str = field(repr=False)
    deadline_seconds: float = 10.0


@dataclass(frozen=True, repr=False)
class _ScopeIdentity:
    scope_identity: str


def binding_digest(permit):
    """Integrity reference only; never a substitute for database authorization."""
    scope, product = permit.scope, permit.product
    facts = [scope.organization_id, scope.store_id, scope.tenant_fingerprint,
             scope.shop_id, scope.warehouse_id, product.product_id,
             product.upstream_product_id, product.snapshot_id, product.snapshot_sha256,
             product.source_system, product.source_version, product.confirmed_by,
             product.expires_at.isoformat(), permit.agent_subject_id,
             permit.permission_epoch, permit.expected_tenant_id]
    return sha256(json.dumps(facts, separators=(',', ':')).encode()).hexdigest()


def _material_gate(runtime, operation):
    if type(runtime) is not ProtectedLiveRuntime or type(runtime.permit) is not LivePermit:
        raise ValueError()
    engine, permit = runtime.engine, runtime.permit
    # This increment intentionally accepts only isolated file SQLite. This is
    # not a production DB loader or approval to point the kernel at real data.
    if (not isinstance(engine, Engine) or engine.dialect.name != 'sqlite'
            or engine.url.database in (None, '', ':memory:')
            or isinstance(engine.pool, (StaticPool, SingletonThreadPool))
            or type(runtime.employee_token) is not str
            or not isinstance(runtime.ledger_directory, Path)
            or not runtime.ledger_directory.is_absolute()
            or not host._digest_valid(runtime.nonce_sha256)
            or type(permit.scope) is not host.read_service.SourceScope
            or type(permit.product) is not host.read_service.ConfirmedProduct
            or not host._instant_valid(permit.expires_at)
            or type(permit.expected_tenant_id) is not int
            or not 0 < permit.expected_tenant_id <= 2**63 - 1
            or type(permit.permission_epoch) is not int or permit.permission_epoch < 1
            or not host._digest_valid(permit.time_evidence_sha256)
            or not host._digest_valid(permit.binding_evidence_sha256)
            or binding_digest(permit) != permit.binding_evidence_sha256
            or not host._digest_valid(permit.price_evidence_sha256 if operation == 'live_prices'
                                      else permit.stock_evidence_sha256)
            or type(runtime.deadline_seconds) not in (float, int)
            or not math.isfinite(runtime.deadline_seconds)
            or not 0.05 <= runtime.deadline_seconds <= 30):
        raise ValueError()
    # Reuse the one-shot constructor's credential/header checks without sending.
    _OneShotHttpsTransport('authenticate', username=runtime.username,
        password=runtime.password, tenancy_name=runtime.tenancy_name,
        deadline_seconds=runtime.deadline_seconds)
    identity = sha256((_BASE + '|' + runtime.tenancy_name).encode()).hexdigest()
    if identity != permit.scope.tenant_fingerprint:
        raise ValueError()
    # Validate bounded IDs before reserving/authenticating, with a dummy token.
    _business_transport(runtime, operation, 'shape-check')


def _business_transport(runtime, operation, token):
    permit = runtime.permit
    materials = dict(token=token, tenant_id=permit.expected_tenant_id,
        tenancy_name=runtime.tenancy_name, product_id=permit.product.upstream_product_id,
        shop_id=permit.scope.shop_id)
    if operation == 'live_stock':
        materials['warehouse_id'] = permit.scope.warehouse_id
    return _OneShotHttpsTransport(operation, deadline_seconds=runtime.deadline_seconds, **materials)


def _binding(runtime, operation):
    permit, scope = runtime.permit, runtime.permit.scope
    digest = sha256(json.dumps([scope.organization_id, scope.store_id,
        scope.tenant_fingerprint, scope.shop_id, scope.warehouse_id],
        separators=(',', ':')).encode()).hexdigest()
    return HostPermitBinding(digest, permit.binding_evidence_sha256,
        runtime.nonce_sha256, permit.permission_epoch, permit.expires_at,
        frozenset({'authenticate', operation}))


def _authentication(raw, expected_tenant):
    """Strict ABP envelope. No decoded token or upstream text escapes the Host."""
    def reject(*_):
        raise ValueError()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result

    if (type(raw) is not bytes or not 0 < len(raw) <= 16384
            or type(expected_tenant) is not int or not 0 < expected_tenant <= 2**63 - 1):
        reject()
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_float=reject, parse_constant=reject)
    if (type(value) is not dict or value.get('success') is not True
            or not {'success', 'result'} <= value.keys()
            or value.keys() - {'success', 'result', 'error', 'targetUrl',
                               'unAuthorizedRequest', '__abp'}
            or value.get('error') is not None
            or ('unAuthorizedRequest' in value and value['unAuthorizedRequest'] is not False)
            or ('__abp' in value and value['__abp'] is not True)):
        reject()
    # This is ABP response metadata, not an HTTP redirect. Never dereference it:
    # all subsequent requests retain their fixed vendor host/path allowlist.
    target = value.get('targetUrl')
    if target is not None:
        if (type(target) is not str or len(target) > 2048
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in target)):
            reject()
        target.encode('utf-8')
    result = value['result']
    if (type(result) is not dict or not {'accessToken', 'tenantId'} <= result.keys()
            or result.keys() - ({'accessToken', 'tenantId', 'expireInSeconds',
                                'encryptedAccessToken', 'userId'}
                               | _AUTH_PROFILE_TEXT | _AUTH_PROFILE_BOOL | _AUTH_PROFILE_INT)):
        reject()
    token, tenant = result['accessToken'], result['tenantId']
    if (type(token) is not str or not 0 < len(token) <= 4096
            or _TOKEN.fullmatch(token) is None or type(tenant) is not int
            or tenant != expected_tenant):
        reject()
    if 'expireInSeconds' in result and (type(result['expireInSeconds']) is not int
                                      or result['expireInSeconds'] <= 0):
        reject()
    if 'userId' in result:
        user_id = result['userId']
        if type(user_id) is str:
            if re.fullmatch(r'[1-9][0-9]{0,18}', user_id, re.ASCII) is None:
                reject()
            user_id = int(user_id)
        if type(user_id) is not int or not 0 < user_id <= 2**63 - 1:
            reject()
    if ('encryptedAccessToken' in result and result['encryptedAccessToken'] is not None
            and type(result['encryptedAccessToken']) is not str):
        reject()
    # Observed profile metadata is validated, then discarded. In particular,
    # upstream display flags never issue or broaden any V4 authorization.
    for key in _AUTH_PROFILE_TEXT & result.keys():
        text = result[key]
        if (type(text) is not str or len(text) > 1024
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text)):
            reject()
        text.encode('utf-8')
    for key in _AUTH_PROFILE_BOOL & result.keys():
        if type(result[key]) is not bool:
            reject()
    for key in _AUTH_PROFILE_INT & result.keys():
        if type(result[key]) is not int or not 0 <= result[key] <= 2**31 - 1:
            reject()
    return token


def _receipt(command, operation, code, counts, committed=False):
    return dict(version='1', command=command, operation=operation,
        environment='offline_host_kernel', code=code,
        call_bounds={key: list(bounds) for key, bounds in counts.items()},
        call_bounds_scope='permit', root_commit_succeeded=committed,
        disclosure_ready=False, real_source_status='HOLD_REAL_READ')


def run(command, operation, *, runtime=None):
    """One reserve, one authentication, one action; no automatic retry."""
    counts = {action: (0, 0) for action in _OPERATIONS}
    ledger = binding = None
    committed = False
    if type(command) is not str or command not in ('check', 'once') or type(operation) is not str or operation not in _OPERATIONS[1:]:
        return _receipt('invalid', 'invalid', 'invalid_arguments', counts)
    try:
        if type(runtime) is not ProtectedLiveRuntime:
            raise ValueError()
        # A revoked/changed runtime must not erase a previous consumption fact.
        if not host._digest_valid(runtime.nonce_sha256) or not isinstance(runtime.ledger_directory, Path):
            raise ValueError()
        counts = {action: (0, 1) for action in _OPERATIONS}
        try:
            (runtime.ledger_directory / runtime.nonce_sha256).lstat()
        except FileNotFoundError:
            counts = {action: (0, 0) for action in _OPERATIONS}
        binding = _binding(runtime, operation)
        binding.digest()
        if any(bounds[1] for bounds in counts.values()):
            counts = LiveCallLedger.inspect(runtime.ledger_directory, binding)
        _material_gate(runtime, operation)
        permit = runtime.permit
        bound = host.read_service.ObservationHost(_ScopeIdentity(permit.scope.tenant_fingerprint),
            permit.scope, permit.product, permit.permission_epoch, source_contract='unverified-live')
        payload = host.ObservationInput(version='1', operation=operation, product_id=permit.product.product_id)

        def check():
            _material_gate(runtime, operation)
            return host._recheck(runtime, bound, payload)

        check()
        if command == 'check':
            return _receipt(command, operation, 'checked_offline_host', counts)
        ledger = LiveCallLedger.reserve(runtime.ledger_directory, binding)

        def send(action, transport):
            check()
            ledger.record(action, 'send_intent')
            counts[action] = (0, 1)

            def gate():
                check()
                ledger.record(action, 'network_entered')
                check()  # fsync cannot act as an authorization lease.

            try:
                response = transport._send_after_host_gate(gate)
            except BaseException:
                # No 'failed': L2a failed means a proven completed HTTP entry.
                # A proven transport zero cannot narrow durable permit facts.
                raise
            counts[action] = (1, 1)
            check()
            if sha256(response.raw).hexdigest() != response.sha256:
                raise ValueError()
            ledger.record(action, 'returned', response_sha256=response.sha256)
            return response

        authentication = send('authenticate', _OneShotHttpsTransport('authenticate',
            username=runtime.username, password=runtime.password, tenancy_name=runtime.tenancy_name,
            deadline_seconds=runtime.deadline_seconds))
        token = _authentication(authentication.raw, permit.expected_tenant_id)
        response = send(operation, _business_transport(runtime, operation, token))
        token = None
        summary = inspect_response(operation, response.raw)
        if summary['response_sha256'] != response.sha256:
            raise ValueError()
        requester, agent = check()
        with Session(runtime.engine, autoflush=False, expire_on_commit=False) as root:
            try:
                host.read_service.audit_structure_precheck(root, requester, agent,
                    bound, payload, summary, run_id=binding.nonce_sha256)
                check()
                host._commit_root(root)
                committed = True
            except BaseException:
                root.rollback()
                raise
        check()
        result = _receipt(command, operation, 'structure_captured', counts, committed)
        result.update(summary)
        return result
    except BaseException:
        # Retain consumption across DB rollback, restart, fsync-after-write
        # exceptions, changed material and unreadable storage. Never retry.
        if binding is not None:
            try:
                (runtime.ledger_directory / binding.nonce_sha256).lstat()
                durable = LiveCallLedger.inspect(runtime.ledger_directory, binding)
                counts = {action: (max(counts[action][0], durable[action][0]),
                    max(counts[action][1], durable[action][1])) for action in _OPERATIONS}
            except FileNotFoundError:
                pass
            except BaseException:
                counts = {action: (0, 1) for action in _OPERATIONS}
        return _receipt(command, operation, 'denied_or_failed', counts, committed)
    finally:
        if ledger is not None:
            try:
                ledger.close()
            except BaseException:
                pass
