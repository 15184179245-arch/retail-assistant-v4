"""Dedicated source process for an already committed Pi native tool intent.

No model configuration, login impersonation, grant issuer, retry or publication.
The parent MUST release its preflight transaction before invoking this process
and commit/re-read the returned observation through observation_receipts.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import pwd

from sqlalchemy import create_engine, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.modules.agents import repository, tool_registry
from backend.app.modules.agents.models import AgentToolCall
from backend.app.modules.assistant.principal import WorkPrincipal
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.workers.agent_guard import guard_execution
from . import live_loader as protected
from . import observation_receipts as observations
from . import sqlite_identity
from .live_call_ledger import HostPermitBinding, LiveCallLedger
from .live_host import _authentication
from .live_once_transport import _OneShotHttpsTransport
from .live_projection import ProjectionBinding, project_observation

_ROOT = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.config/retail-v4/guanjia-work-source'
_ACTIONS = ('authenticate', 'live_prices', 'live_stock')
_MATERIAL_FIELDS = ('version', 'machine_token', 'username', 'password', 'tenancy_name',
    'expected_tenant_id', 'organization_id', 'store_id', 'agent_subject_id',
    'shop_id', 'warehouse_id', 'expires_at', 'product_details')


class SourceDenied(ValueError):
    def __init__(self):
        super().__init__('guanjia_work_source_unavailable')


def _deny():
    raise SourceDenied()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


@dataclass(frozen=True, repr=False)
class SourceCall:
    claim: repository.AgentClaim
    step_id: str
    run_version: int
    step_version: int
    native_call_id: str
    tool_key: str
    toolset_version: str
    selection: observations.SourceSelection

    def wire(self):
        claim = asdict(self.claim)
        del claim['runtime_identity']
        return {'version': 1, 'claim': claim, 'step_id': self.step_id,
            'run_version': self.run_version, 'step_version': self.step_version,
            'native_call_id': self.native_call_id, 'tool_key': self.tool_key,
            'toolset_version': self.toolset_version, 'selection': asdict(self.selection)}

    @property
    def nonce(self):
        # A changed epoch/step/product/operation cannot reissue this native call.
        return _digest(['v4.guanjia.native/1', self.claim.organization_id,
            self.claim.store_id, self.claim.run_id, self.native_call_id])


def decode_call(raw):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= 16384:
            _deny()
        value = protected._decode(raw)
        protected._shape(value, ('version', 'claim', 'step_id', 'run_version',
            'step_version', 'native_call_id', 'tool_key', 'toolset_version', 'selection'))
        if type(value['version']) is not int or value['version'] != 1:
            _deny()
        claim = protected._shape(value['claim'], ('run_id', 'organization_id', 'store_id', 'epoch', 'machine_id'))
        claim = repository.AgentClaim(**{k: protected._integer(v) if k == 'epoch' else protected._text(v)
            for k, v in claim.items()}, runtime_identity=repository.SDK_RUNTIME)
        selection = protected._shape(value['selection'], ('product_id', 'snapshot_id',
            'product_version', 'snapshot_sha256', 'content_sha256'))
        selection = observations.SourceSelection(**{k: protected._integer(v) if k == 'product_version'
            else protected._digest(v) if k.endswith('sha256') else protected._text(v)
            for k, v in selection.items()})
        if not observations.handles_tool(value['tool_key']):
            _deny()
        toolset = protected._text(value['toolset_version'])
        if not toolset.startswith('sha256:'):
            _deny()
        protected._digest(toolset[7:])
        return SourceCall(claim, protected._text(value['step_id']),
            protected._integer(value['run_version']), protected._integer(value['step_version']),
            protected._text(value['native_call_id']), value['tool_key'],
            toolset, selection)
    except (ValueError, TypeError, KeyError, RecursionError, protected.LoaderDenied):
        raise SourceDenied() from None


def _material(raw):
    value = protected._shape(protected._decode(raw), _MATERIAL_FIELDS)
    if type(value['version']) is not int or value['version'] != 2:
        _deny()
    for key in ('machine_token', 'username', 'password', 'tenancy_name'):
        protected._text(value[key], 4096)
    for key in ('organization_id', 'store_id', 'agent_subject_id', 'shop_id', 'warehouse_id'):
        protected._text(value[key])
    protected._integer(value['expected_tenant_id'])
    protected._instant(value['expires_at'])
    if type(value['product_details']) is not dict or not value['product_details']:
        _deny()
    for key, digest in value['product_details'].items():
        protected._text(key)
        protected._digest(digest)
    return value


@dataclass(repr=False)
class _Loaded:
    engine: object = field(repr=False)
    files: protected._Files = field(repr=False)
    material_raw: bytes = field(repr=False)
    details: dict = field(default_factory=dict, repr=False)

    def validate(self):
        self.files.validate()
        return _material(self.material_raw)

    def detail(self, snapshot_id):
        material = self.validate()
        digest = material['product_details'].get(snapshot_id)
        if digest is None:
            _deny()
        if digest not in self.details:
            raw = self.files.read('detail-' + digest + '.json')
            if sha256(raw).hexdigest() != digest:
                _deny()
            self.details[digest] = raw
        return self.details[digest], digest


@contextmanager
def load_source():
    """Fixed private files and actual SQLite FD identity; never from request."""
    files = engine = None
    try:
        protected._process_gate()
        files = protected._Files(_ROOT)
        raw = files.read('material.json')
        _material(raw)
        files.read('authority.sqlite3', database=True)
        info = next(info for name, _, info, database in files.files if database)

        def connect():
            files.validate()
            connection = sqlite_identity.connect(files.root / 'authority.sqlite3', (info.st_dev, info.st_ino))
            try:
                files.validate()
                connection.execute('PRAGMA foreign_keys=ON')
                return connection
            except BaseException:
                connection.close()
                raise

        engine = create_engine(URL.create('sqlite', database=str(files.root / 'authority.sqlite3')),
            creator=connect, poolclass=NullPool, hide_parameters=True)
        yield _Loaded(engine, files, raw)
    finally:
        if engine is not None:
            engine.dispose()
        if files is not None:
            files.close()


def _check(loaded, call):
    """Own and release each guard transaction; no stale auth across network."""
    material = loaded.validate()
    if (call.claim.organization_id, call.claim.store_id) != (material['organization_id'], material['store_id']):
        _deny()
    with Session(loaded.engine, autoflush=False, expire_on_commit=False) as session:
        ctx = guard_execution(session, material['machine_token'], call.claim, step_id=call.step_id,
            expected_run_version=call.run_version, expected_step_version=call.step_version,
            expected_runtime=repository.SDK_RUNTIME)
        if (type(ctx.person) is not WorkPrincipal or ctx.person.run_id != call.claim.run_id
                or ctx.agent.subject_id != material['agent_subject_id']
                or ctx.run.state != 'running' or ctx.execution.dispatch_state != 'dispatched'
                or ctx.step.kind != 'tool' or ctx.step.state != 'running'
                or ctx.run.toolset_version != call.toolset_version):
            _deny()
        frozen = ctx.execution.frozen_context['pi_readonly_binding']
        tool_registry.verify_pi_binding(session, ctx.person, ctx.agent, run_id=ctx.run.id,
            binding=frozen, expected_digest=call.toolset_version, now=ctx.now)
        operation = next(k for k, v in observations._TOOLS.items() if v[0] == call.tool_key)
        key, name, _, _ = observations._TOOLS[operation]
        from backend.app.modules.agents.capability_discovery import active_names
        if name not in active_names(session, ctx):
            _deny()
        arguments = {'product_id': call.selection.product_id}
        decision = {'tool_key': key, 'version': '1', 'arguments': arguments,
            'native_call_id': call.native_call_id, 'toolset_version': call.toolset_version}
        receipt = repository.require_pi_tool_receipt(session, call.claim, pi_name=name,
            decision=decision, state='dispatch_intent', tool_step_id=call.step_id)
        tool_call = session.scalar(select(AgentToolCall).where(
            AgentToolCall.organization_id == call.claim.organization_id,
            AgentToolCall.store_id == call.claim.store_id, AgentToolCall.run_id == call.claim.run_id,
            AgentToolCall.step_id == call.step_id))
        if (tool_call is None or tool_call.idempotency_key != 'pi:' + _digest([call.claim.run_id, call.native_call_id])
                or (tool_call.tool_key, tool_call.tool_version, tool_call.input_digest)
                != (key, '1', receipt.input_digest)):
            _deny()
        selected = observations.select_source(session, actor=ctx.person, agent=ctx.agent,
            tool_key=key, arguments=arguments)
        if selected != call.selection:
            _deny()
        snapshot = session.get(CatalogSnapshot, selected.snapshot_id)
        if session.get(CatalogProduct, selected.product_id).source_system != 'guanjia':
            _deny()
        raw, detail_sha = loaded.detail(selected.snapshot_id)
        # Original protected response bytes remain the projection input. This
        # comparison only proves they describe the selected persisted product.
        envelope = json.loads(raw)
        if (not isinstance(envelope, dict) or envelope.get('success') is not True
                or envelope.get('result') != snapshot.payload.get('raw')):
            _deny()
        product = envelope['result']
        projection = ProjectionBinding(product['id'], product['storeId'], material['shop_id'],
            material['warehouse_id'], detail_sha)
        # Validate detail/units before authentication or any business request.
        project_observation(operation, b'{"success":true,"result":[]}', product_raw=raw,
            binding=projection, observed_started_at=ctx.now, observed_finished_at=ctx.now)
        expiry = min(protected._instant(material['expires_at']), ctx.person.auth_expires_at, ctx.run.deadline_at)
        scope = _digest([material['organization_id'], material['store_id'],
            'https://erp.example.invalid', material['tenancy_name'], material['expected_tenant_id'],
            material['shop_id'], material['warehouse_id']])
        binding = HostPermitBinding(scope, _digest(call.wire()), call.nonce, ctx.person.work_epoch,
            expiry, frozenset({'authenticate', operation}))
        binding.digest()
        # No audit/authority mutation is committed by this read guard.
        return material, operation, raw, projection, binding


def execute(call, loaded):
    """Internal prepared result only. A durable reservation is never undone."""
    ledger = binding = None
    counts = None
    try:
        protected._process_gate()
        if type(call) is not SourceCall or type(loaded) is not _Loaded or decode_call(_json(call.wire()).encode()) != call:
            _deny()
        material = loaded.validate()
        # The dedicated process has one protected Agent identity. Never let
        # caller env select it; do not perform this in an application thread.
        previous_agent = os.environ.get('AGENT_SUBJECT_ID')
        os.environ['AGENT_SUBJECT_ID'] = material['agent_subject_id']
        try:
            material, operation, product_raw, projection, binding = _check(loaded, call)
            counts = {key: [0, 0] for key in _ACTIONS}
            ledger = LiveCallLedger.reserve(loaded.files.root / 'ledger', binding)

            def check():
                current = _check(loaded, call)
                if current[1:] != (operation, product_raw, projection, binding):
                    _deny()

            def send(action, transport):
                check()
                ledger.record(action, 'send_intent')
                counts[action] = [0, 1]

                def gate():
                    check()
                    ledger.record(action, 'network_entered')
                    check()

                response = transport._send_after_host_gate(gate)
                counts[action] = [1, 1]
                check()
                if sha256(response.raw).hexdigest() != response.sha256:
                    _deny()
                ledger.record(action, 'returned', response_sha256=response.sha256)
                return response

            auth = send('authenticate', _OneShotHttpsTransport('authenticate', username=material['username'],
                password=material['password'], tenancy_name=material['tenancy_name']))
            token = _authentication(auth.raw, material['expected_tenant_id'])
            params = dict(token=token, tenant_id=material['expected_tenant_id'], tenancy_name=material['tenancy_name'],
                product_id=projection.product_id, shop_id=projection.shop_id)
            if operation == 'live_stock':
                params['warehouse_id'] = projection.warehouse_id
            started = datetime.now(timezone.utc)
            response = send(operation, _OneShotHttpsTransport(operation, **params))
            finished = datetime.now(timezone.utc)
            token = None
            prepared = observations.prepare(call.claim.run_id, call.step_id, operation, response.raw,
                product_raw=product_raw, binding=projection, observed_started_at=started, observed_finished_at=finished)
            check()
            return {'version': 1, 'code': 'prepared', 'request_sha256': _digest(call.wire()),
                'call_bounds': counts, 'disclosure_ready': False, 'result': prepared.result()}
        finally:
            if previous_agent is None:
                os.environ.pop('AGENT_SUBJECT_ID', None)
            else:
                os.environ['AGENT_SUBJECT_ID'] = previous_agent
    except BaseException:
        # Unreadable/changed storage is unknown, never proof of zero sends.
        if binding is not None:
            try:
                counts = {key: list(value) for key, value in LiveCallLedger.inspect(loaded.files.root / 'ledger', binding).items()}
            except BaseException:
                counts = {key: [0, 1] for key in _ACTIONS}
        return {'version': 1, 'code': 'denied_or_failed', 'call_bounds': counts,
            'disclosure_ready': False, 'result': None}
    finally:
        if ledger is not None:
            ledger.close()


def run(raw):
    try:
        call = decode_call(raw)
        with load_source() as loaded:
            return execute(call, loaded)
    except BaseException:
        return {'version': 1, 'code': 'protected_source_unavailable', 'call_bounds': None,
            'disclosure_ready': False, 'result': None}
