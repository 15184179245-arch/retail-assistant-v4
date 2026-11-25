"""Stage a source observation in the Pi result transaction; read committed facts.

No HTTP route, credentials, transport, replay or approval is provided here.
The Host owns source transport verification and its final execution guard.
The only outward read uses a fresh connection and current dual authorization.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool, SingletonThreadPool

from backend.app.application import commit_guard
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import repository, tool_registry
from backend.app.modules.agents.models import AgentRun, AgentExecution, AgentStep, AgentToolCall, AgentPiToolReceipt
from backend.app.modules.audit.service import record_event
from backend.app.modules.catalog.models import CatalogProduct, CatalogSnapshot
from backend.app.modules.catalog.service import _canonical as catalog_canonical
from backend.app.modules.identity.service import authorize_scope
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority
from .live_projection import project_observation, CONTRACT
from .observation_models import GuanjiaObservationReceipt as Receipt

_TOOLS = {'live_prices': ('guanjia.read_prices', 'v4_guanjia_read_prices_v1',
                         'guanjia.pricing.read', 'agents.guanjia.prices.read'),
          'live_stock': ('guanjia.read_stock', 'v4_guanjia_read_stock_v1',
                        'guanjia.inventory.read', 'agents.guanjia.stock.read')}


class ObservationReceiptDenied(ValueError):
    def __init__(self):
        super().__init__('guanjia_observation_receipt_unavailable')


def _deny():
    raise ObservationReceiptDenied()


def _json(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(data.encode('utf-8')) > 131072:
        _deny()
    return data


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _id(run_id, step_id):
    return str(uuid5(NAMESPACE_URL, 'retail/guanjia-observation/' + _json([run_id, step_id])))


@dataclass(frozen=True, repr=False)
class PreparedObservation:
    """Host-local immutable value; neither construction nor a hash grants rights."""
    payload_json: str

    def result(self):
        return json.loads(self.payload_json)


@dataclass(frozen=True, repr=False)
class SourceSelection:
    product_id: str
    snapshot_id: str
    product_version: int
    snapshot_sha256: str
    content_sha256: str


def prepare(run_id, step_id, operation, raw, **projection_arguments):
    projection = project_observation(operation, raw, **projection_arguments)
    return PreparedObservation(_json({'observation_id': _id(run_id, step_id), 'observation': projection}))


def _engine(engine):
    if not isinstance(engine, Engine) or isinstance(engine.pool, (StaticPool, SingletonThreadPool)):
        _deny()


def _scope_query(model, actor, **conditions):
    return select(model).where(model.organization_id == actor.organization_id,
        model.store_id == actor.store_id, *(getattr(model, k) == v for k, v in conditions.items())).execution_options(populate_existing=True)


def _authorize(session, actor, agent, operation, *, now=None):
    if operation not in _TOOLS:
        _deny()
    tool_registry._person_scope(session, actor, now=now)
    tool_registry._agent_scope(session, actor, agent, _TOOLS[operation][3], now=now)
    for permission in ('agents.run', 'catalog.read', _TOOLS[operation][2],
                       *(('guanjia.company.read',) if operation == 'live_prices' else ())):
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)


def _content_digest(product, snapshot):
    # The old source digest includes the original timestamp spelling, which
    # cannot always be reconstructed from normalized DB datetimes. Retain it
    # separately and pin the actual loaded content without re-signing history.
    value = {'source_system':product.source_system,'external_code':product.external_code,
        'payload':snapshot.payload,'source_version':snapshot.source_version,
        'source_updated_at':snapshot.source_updated_at.isoformat() if snapshot.source_updated_at else None}
    return sha256(catalog_canonical(value).encode('utf-8')).hexdigest()


def _catalog(session, actor, product_id, snapshot_id, projection, *, version=None, digest=None, content_digest=None):
    product = session.scalar(_scope_query(CatalogProduct, actor, id=product_id))
    snapshot = session.scalar(_scope_query(CatalogSnapshot, actor, id=snapshot_id))
    if (product is None or snapshot is None or not product.active or product.current_snapshot_id != snapshot_id
            or snapshot.product_id != product.id or snapshot.source_version != product.source_version
            or (version is not None and product.version != version)
            or (digest is not None and snapshot.payload_sha256 != digest)
            or (content_digest is not None and _content_digest(product, snapshot) != content_digest)):
        _deny()
    raw = snapshot.payload.get('raw')
    if (type(raw) is not dict or raw.get('id') != projection['product']['product_id']
            or raw.get('storeId') != projection['product']['source_store_id']):
        _deny()
    return product, snapshot


def _facts(session, actor, agent, *, run_id, step_id, pi_receipt_id, product_id, result):
    if (type(result) is not dict or set(result) != {'observation_id','observation'}
            or result['observation_id'] != _id(run_id, step_id)
            or result['observation']['contract'] != CONTRACT
            or result['observation']['authority'] != 'source_field_observation_only'):
        _deny()
    operation = result['observation']['operation']
    key, name, _, _ = _TOOLS[operation]
    run = session.scalar(_scope_query(AgentRun, actor, id=run_id))
    execution = session.scalar(_scope_query(AgentExecution, actor, run_id=run_id))
    step = session.scalar(_scope_query(AgentStep, actor, id=step_id, run_id=run_id))
    receipt = session.scalar(_scope_query(AgentPiToolReceipt, actor, id=pi_receipt_id, run_id=run_id))
    call = session.scalar(_scope_query(AgentToolCall, actor, run_id=run_id, step_id=step_id))
    if any(x is None for x in (run, execution, step, receipt, call)):
        _deny()
    binding = execution.frozen_context.get('pi_readonly_binding', {})
    output = 'sha256:' + _digest(result)
    if (run.requester_id != actor.subject_id or binding.get('agent_id') != agent.subject_id
            or binding.get('requester_id') != actor.subject_id or binding.get('run_id') != run_id
            or binding.get('organization_id') != actor.organization_id or binding.get('store_id') != actor.store_id
            or not any(t.get('tool_key') == key and t.get('version') == '1' and t.get('name') == name
                       for t in binding.get('tools', []))
            or (step.kind, step.state, step.output_ref) != ('tool', 'succeeded', output)
            or (receipt.tool_step_id, receipt.tool_kind, receipt.state, receipt.output_ref) != (step_id, 'tool', 'succeeded', output)
            or (receipt.tool_key, receipt.tool_version, receipt.pi_name, receipt.toolset_version) != (key, '1', name, run.toolset_version)
            or (call.tool_key, call.tool_version, call.input_digest) != (key, '1', receipt.input_digest)
            or call.input_digest != _digest({'product_id': product_id})):
        _deny()
    expected_call_key = 'pi:' + _digest([run_id, receipt.native_call_id])
    if call.idempotency_key != expected_call_key:
        _deny()
    return run


def _check_claim_at_commit(session, claim, instant):
    # commit_guard already sampled the DB clock after flushing. Its callbacks
    # accept structured SELECTs only; do not issue repository's textual clock.
    repository._reject_runtime_mutations(session)
    execution = repository.execution_for(session, claim.organization_id, claim.store_id, claim.run_id)
    if (execution is None or claim.runtime_identity != repository.SDK_RUNTIME
            or repository.runtime_identity(execution.frozen_context) != repository.SDK_RUNTIME
            or execution.mode != 'worker_v2' or execution.phase != 'claimed'
            or execution.owner_machine_id != claim.machine_id or execution.epoch != claim.epoch
            or execution.lease_until is None or execution.lease_until <= instant
            or execution.authorization_expires_at <= instant
            or execution.cancel_requested_at is not None):
        _deny()


def stage(session, *, actor, agent, claim, step_id, pi_receipt_id, product_id, snapshot_id, prepared, source_selection=None):
    """Stage only; caller commits. No observation values escape this function.

Must be called after the same tool result/receipt transitions to succeeded,
before the Host's result-root commit. Rejected or unknown roots never disclose.
"""
    try:
        engine = session.get_bind();_engine(engine)
        if type(prepared) is not PreparedObservation:
            _deny()
        result = prepared.result();projection = result['observation'];operation = projection['operation']
        from backend.app.modules.assistant.principal import WorkPrincipal
        if type(actor) is WorkPrincipal and actor.run_id != claim.run_id:
            _deny()
        epoch = lock_command_authority(session, actor)
        repository.execution_for(session, claim.organization_id, claim.store_id, claim.run_id, for_update=True)
        session.scalar(_scope_query(AgentRun, actor, id=claim.run_id).with_for_update())
        execution = repository.require_worker_claim(session, claim, expected_runtime=repository.SDK_RUNTIME)
        with session.no_autoflush:
            run = _facts(session, actor, agent, run_id=claim.run_id, step_id=step_id,
                         pi_receipt_id=pi_receipt_id, product_id=product_id, result=result)
            if run.state != 'running' or (execution.organization_id, execution.store_id) != (actor.organization_id, actor.store_id):
                _deny()
        # Serialize catalog identity changes through the result root (SQLite's
        # authority fence already holds the writer lock; PG needs row locks).
        with session.no_autoflush:
            session.scalar(_scope_query(CatalogProduct, actor, id=product_id).with_for_update(read=True))
            session.scalar(_scope_query(CatalogSnapshot, actor, id=snapshot_id).with_for_update(read=True))
        with Session(engine, autoflush=False) as current:
            _authorize(current, actor, agent, operation)
            product, snapshot = _catalog(current, actor, product_id, snapshot_id, projection)
            version, snapshot_sha = product.version, snapshot.payload_sha256
            content_sha = _content_digest(product, snapshot)
            if source_selection is not None and (type(source_selection) is not SourceSelection
                    or source_selection != SourceSelection(product_id,snapshot_id,version,snapshot_sha,content_sha)):
                _deny()
        identity = result['observation_id']
        if session.get(Receipt, identity) is not None:
            _deny()  # read back an existing receipt; never stage/re-execute it
        row = Receipt(id=identity, organization_id=actor.organization_id, store_id=actor.store_id,
            requester_id=actor.subject_id, agent_id=agent.subject_id, run_id=claim.run_id,
            step_id=step_id, step_kind='tool', pi_receipt_id=pi_receipt_id,
            product_id=product_id, product_version=version, snapshot_id=snapshot_id,
            snapshot_sha256=snapshot_sha, snapshot_content_sha256=content_sha, operation=operation, result=result,
            result_sha256=_digest(result), created_at=datetime.now(timezone.utc))
        session.add(row)
        expected = {key: getattr(row, key) for key in ('id','organization_id','store_id','requester_id',
            'agent_id','run_id','step_id','step_kind','pi_receipt_id','product_id','product_version',
            'snapshot_id','snapshot_sha256','snapshot_content_sha256','operation','result_sha256')}
        record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
            store_id=actor.store_id, action='guanjia.observation.recorded', target_type='guanjia_observation',
            target_id=identity, idempotency_key='guanjia.observation:' + identity,
            details={'run_id':claim.run_id,'step_id':step_id,'operation':operation,'result_sha256':row.result_sha256})

        def check(active, _instant):
            check_command_authority(active, actor, permission='agents.run', epoch=epoch, now=_instant)
            _check_claim_at_commit(active, claim, _instant)
            durable = active.scalar(_scope_query(Receipt, actor, id=identity))
            if (durable is None or any(getattr(durable, key) != value for key, value in expected.items())
                    or _json(durable.result) != prepared.payload_json):
                _deny()
            # Final checks must see this root's flushed edits as well as the
            # fenced current authority; a separate connection misses own writes.
            _authorize(active, actor, agent, operation, now=_instant)
            _catalog(active, actor, product_id, snapshot_id, projection, version=version,
                     digest=snapshot_sha, content_digest=content_sha)
            final_run = _facts(active, actor, agent, run_id=claim.run_id, step_id=step_id,
                              pi_receipt_id=pi_receipt_id, product_id=product_id, result=prepared.result())
            if final_run.state != 'running':
                _deny()
        commit_guard.enlist(session, action='guanjia_observation', check=check)
        return {'kind':'guanjia_observation','id':identity,'sha256':row.result_sha256}
    except (ValueError, TypeError, KeyError, AttributeError, HTTPException, SQLAlchemyError,
            tool_registry.ToolRegistryError, repository.AgentPersistenceError):
        raise ObservationReceiptDenied() from None


def read(engine, actor, reference):
    """Return an authorized historical observation from a fresh committed read.

Returning None reveals neither another user's IDs nor why an object is absent.
There is no transport fallback, write, renewal or result replay on this path.
"""
    try:
        _engine(engine)
        if (type(reference) is not dict or set(reference) != {'kind','id','sha256'}
                or reference['kind'] != 'guanjia_observation'
                or type(reference['id']) is not str or type(reference['sha256']) is not str):
            _deny()
        with Session(engine, autoflush=False) as session:
            row = session.scalar(_scope_query(Receipt, actor, id=reference['id'], requester_id=actor.subject_id))
            if row is None:
                return None
            agent = AuthenticatedSubject(subject_id=row.agent_id, organization_id=actor.organization_id,
                store_id=actor.store_id, auth_expires_at=actor.auth_expires_at)
            _authorize(session, actor, agent, row.operation)
            if row.result_sha256 != reference['sha256'] or _digest(row.result) != row.result_sha256:
                _deny()
            _facts(session, actor, agent, run_id=row.run_id, step_id=row.step_id,
                   pi_receipt_id=row.pi_receipt_id, product_id=row.product_id, result=row.result)
            if row.result['observation']['operation'] != row.operation:
                _deny()
            _catalog(session, actor, row.product_id, row.snapshot_id, row.result['observation'],
                     version=row.product_version, digest=row.snapshot_sha256, content_digest=row.snapshot_content_sha256)
            return json.loads(_json(row.result))
    except (ValueError, TypeError, KeyError, AttributeError, HTTPException, SQLAlchemyError,
            tool_registry.ToolRegistryError, repository.AgentPersistenceError):
        return None


def handles_tool(key):
    return type(key) is str and key in {entry[0] for entry in _TOOLS.values()}


def select_source(session, *, actor, agent, tool_key, arguments):
    """Freeze current catalog identity before dispatch, not after its return."""
    if (not handles_tool(tool_key) or type(arguments) is not dict
            or set(arguments) != {'product_id'} or type(arguments['product_id']) is not str):
        _deny()
    operation = next(k for k,v in _TOOLS.items() if v[0] == tool_key)
    _authorize(session, actor, agent, operation)
    with session.no_autoflush:
        product = session.scalar(_scope_query(CatalogProduct, actor, id=arguments['product_id']))
        if product is None or not product.active or product.current_snapshot_id is None:
            _deny()
        snapshot = session.scalar(_scope_query(CatalogSnapshot, actor, id=product.current_snapshot_id))
        if snapshot is None or snapshot.product_id != product.id or snapshot.source_version != product.source_version:
            _deny()
        return SourceSelection(product.id,snapshot.id,product.version,snapshot.payload_sha256,_content_digest(product,snapshot))


def stage_tool_result(session, *, actor, agent, claim, step_id, pi_receipt_id,
                      tool_key, arguments, result, selection):
    """Internal Host result-root adapter, never a model-callable tool.

    Registration and the source handler own transport/projection. This layer
    requires their exact successful native receipt before creating a reference.
    """
    if (not handles_tool(tool_key) or type(arguments) is not dict
            or set(arguments) != {'product_id'} or type(arguments['product_id']) is not str
            or type(result) is not dict or type(result.get('observation')) is not dict
            or _TOOLS.get(result['observation'].get('operation'), (None,))[0] != tool_key
            or type(selection) is not SourceSelection or selection.product_id != arguments['product_id']):
        _deny()
    return stage(session, actor=actor, agent=agent, claim=claim, step_id=step_id,
        pi_receipt_id=pi_receipt_id, product_id=arguments['product_id'], snapshot_id=selection.snapshot_id,
        prepared=PreparedObservation(_json(result)), source_selection=selection)


def citation(engine, actor, reference):
    """No raw business values or source metadata assertions in an answer cite."""
    result = read(engine, actor, reference)
    if result is None:
        return None
    observation = result['observation']
    return {'kind': 'guanjia_observation', 'object_id': reference['id'],
        'version_ref': reference['sha256'], 'version': reference['sha256'],
        'label': '管家婆' + ('价格' if observation['operation'] == 'live_prices' else '库存') + '原始字段（查询时观察）',
        'source_ref': 'guanjia-observation:' + reference['id'],
        'observed_at': observation['observed_finished_at']}
