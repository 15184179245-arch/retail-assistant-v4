"""Internal category facts, staged in the original successful Pi result root.

No registration, transport or credential loading. Host configuration owns the
source binding; neither that value nor PreparedCategory is a capability. Reads
recompute the entire page and never fall back to source execution or renewal.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.application import commit_guard
from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import repository, tool_registry
from backend.app.modules.agents.models import AgentRun, AgentExecution, AgentStep, AgentToolCall, AgentPiToolReceipt
from backend.app.modules.assistant.principal import WorkPrincipal
from backend.app.modules.audit.service import record_event
from backend.app.modules.identity.command_guard import lock_command_authority, check_command_authority
from backend.app.modules.identity.service import authorize_scope
from .category_catalog_query import query_category_catalog, query_category_catalog_status
from .category_receipt_models import GuanjiaCategoryReceipt as Receipt
from .category_source_loader import load_category_source
from .observation_receipts import _engine, _scope_query, _check_claim_at_commit

CONTRACT = 'v4.guanjia-category/1'
STATUS_CONTRACT = 'v4.guanjia-category/2'
_STATUS_TOOL = ('guanjia.category_products_status', 'v4_guanjia_category_products_status_v1')
_TOOLS = {
    'category_discovery': ('guanjia.category_discovery', 'v4_guanjia_category_discovery_v1'),
    'category_products': ('guanjia.category_products', 'v4_guanjia_category_products_v1'),
}


class CategoryReceiptDenied(ValueError):
    def __init__(self):
        super().__init__('guanjia_category_receipt_unavailable')


def _deny():
    raise CategoryReceiptDenied()


def _tools_for(operation, contract):
    if contract == CONTRACT and operation in _TOOLS:
        return _TOOLS[operation]
    if contract == STATUS_CONTRACT and operation == 'category_products':
        return _STATUS_TOOL
    _deny()


def native_contract(key, version, name):
    """Fixed IPC identity mapping; no arbitrary key suffix chooses an operation."""
    for operation, pair in _TOOLS.items():
        if (key, version, name) == (pair[0], '1', pair[1]):
            return operation, CONTRACT
    if (key, version, name) == (_STATUS_TOOL[0], '1', _STATUS_TOOL[1]):
        return 'category_products', STATUS_CONTRACT
    _deny()


def _selected_contract(binding, operation):
    selector = binding.get('category_products_contract')
    if selector is None:
        if 'category_products_contract' in binding:
            _deny()
        return CONTRACT
    if (type(selector) is not str or selector != STATUS_CONTRACT
            or binding.get('profile') != tool_registry.PI_SOURCE_PROFILE
            or binding.get('category_source') is None):
        _deny()
    return STATUS_CONTRACT if operation == 'category_products' else CONTRACT


def _json(value):
    value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(value.encode('utf-8')) > 131072:
        _deny()
    return value


def _digest(value):
    return sha256(_json(value).encode('utf-8')).hexdigest()


def _id(run_id, step_id):
    return str(uuid5(NAMESPACE_URL, 'retail/guanjia-category/' + _json([run_id, step_id])))


@dataclass(frozen=True, repr=False)
class CategorySourceBinding:
    """Trusted Host configuration, never model arguments or self-signed metadata."""
    organization_id: str
    store_id: str
    directory_path: Path
    artifact_sha256: str


@dataclass(frozen=True, repr=False)
class PreparedCategory:
    payload_json: str

    def result(self):
        return json.loads(self.payload_json)


def _text(value):
    if (type(value) is not str or not value or value != value.strip() or len(value) > 200
            or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        _deny()
    return value


def _arguments(operation, arguments):
    keys = {'query', 'limit'} if operation == 'category_discovery' else {'category_business_id', 'limit', 'offset'}
    if operation not in _TOOLS or type(arguments) is not dict or set(arguments) != keys:
        _deny()
    if type(arguments['limit']) is not int or not 1 <= arguments['limit'] <= 100:
        _deny()
    _text(arguments['query'] if operation == 'category_discovery' else arguments['category_business_id'])
    if operation == 'category_products' and (type(arguments['offset']) is not int or not 0 <= arguments['offset'] <= 1000000):
        _deny()
    return json.loads(_json(arguments))


def _authorize(session, actor, agent, *, now=None):
    tool_registry._person_scope(session, actor, now=now)
    tool_registry._agent_scope(session, actor, agent, 'agents.catalog.search', now=now)
    for permission in ('agents.run', 'catalog.read'):
        authorize_scope(session, actor, organization_id=actor.organization_id,
                        store_id=actor.store_id, permission=permission, now=now)


def _source(actor, binding):
    if (type(binding) is not CategorySourceBinding
            or (binding.organization_id, binding.store_id) != (actor.organization_id, actor.store_id)
            or not isinstance(binding.directory_path, Path)
            or not binding.directory_path.is_absolute()
            or type(binding.artifact_sha256) is not str
            or re.fullmatch('[a-f0-9]{64}', binding.artifact_sha256) is None):
        _deny()
    return load_category_source(binding.directory_path, binding.artifact_sha256)


def _result(session, actor, run_id, step_id, operation, arguments, binding, *, contract=CONTRACT):
    if contract != CONTRACT:
        return _status_result(session, actor, run_id, step_id, operation, arguments, binding, contract)
    arguments = _arguments(operation, arguments)
    source = _source(actor, binding)
    if operation == 'category_discovery':
        hits, truncated = source.directory.find(arguments['query'], limit=arguments['limit'])
        data = {'items': [dict(asdict(hit.category),
                              descendant_business_ids=list(hit.descendant_business_ids)) for hit in hits],
                'limit': arguments['limit'], 'truncated': truncated}
    else:
        data = asdict(query_category_catalog(session, source.directory,
            organization_id=actor.organization_id, store_id=actor.store_id, **arguments))
        # Source metadata has one canonical position in the result envelope.
        del data['directory_sha256']
        del data['directory_observed_at']
    return json.loads(_json({'category_receipt_id': _id(run_id, step_id), 'category_result': {
        'contract': CONTRACT, 'authority': 'local_source_snapshot_only', 'operation': operation,
        'arguments': arguments, 'directory_artifact_sha256': source.artifact_sha256,
        'directory_content_sha256': source.content_sha256,
        'directory_observed_at': source.directory.observed_at.isoformat(), 'data': data}}))


def _status_result(session, actor, run_id, step_id, operation, arguments, binding, contract):
    _tools_for(operation, contract)
    arguments = _arguments(operation, arguments)
    source = _source(actor, binding)
    data = asdict(query_category_catalog_status(session, source.directory,
        organization_id=actor.organization_id, store_id=actor.store_id, **arguments))
    del data['directory_sha256']
    del data['directory_observed_at']
    return json.loads(_json({'category_receipt_id': _id(run_id, step_id), 'category_result': {
        'contract': STATUS_CONTRACT, 'authority': 'local_source_snapshot_only', 'operation': operation,
        'arguments': arguments, 'directory_artifact_sha256': source.artifact_sha256,
        'directory_content_sha256': source.content_sha256,
        'directory_observed_at': source.directory.observed_at.isoformat(), 'data': data}}))


def prepare(session, *, actor, agent, run_id, step_id, operation, arguments, source_binding):
    """Authorized local preparation; the caller must not disclose it before commit."""
    try:
        _engine(session.get_bind())
        _text(run_id); _text(step_id)
        if type(actor) is WorkPrincipal and actor.run_id != run_id:
            _deny()
        _authorize(session, actor, agent)
        arguments = _arguments(operation, arguments)
        native = session.scalar(_scope_query(AgentPiToolReceipt, actor,
            run_id=run_id, tool_step_id=step_id, state='dispatch_intent'))
        step = session.scalar(_scope_query(AgentStep, actor, run_id=run_id, id=step_id))
        if native is None:
            _deny()
        native_operation, contract = native_contract(native.tool_key, native.tool_version, native.pi_name)
        execution = session.scalar(_scope_query(AgentExecution, actor, run_id=run_id))
        run = session.scalar(_scope_query(AgentRun, actor, id=run_id))
        binding = execution.frozen_context.get('pi_readonly_binding', {}) if execution is not None else {}
        if (run is None or native_operation != operation or _selected_contract(binding, operation) != contract):
            _deny()
        if contract == STATUS_CONTRACT and tool_registry.pi_binding_digest(binding) != run.toolset_version:
            _deny()
        key, name = _tools_for(operation, contract)
        if (step is None or (step.kind, step.state) != ('tool', 'running')
                or (native.tool_key, native.tool_version, native.pi_name, native.input_digest)
                    != (key, '1', name, _digest(arguments))):
            _deny()
        prepared = PreparedCategory(_json(_result(session, actor, run_id, step_id,
            operation, arguments, source_binding, contract=contract)))
        # Only the original Host result Session/root may stage this exact value.
        # This prevents reconstructing an old successful native call in a later
        # transaction or carrying an internal prepared result across processes.
        session.info.setdefault('guanjia_category_prepared', {})[id(prepared)] = (
            session.get_transaction(), native.id, prepared)
        return prepared
    except _ERRORS:
        raise CategoryReceiptDenied() from None


def _facts(session, actor, agent, *, run_id, step_id, pi_receipt_id, result):
    if type(result) is not dict or set(result) != {'category_receipt_id', 'category_result'}:
        _deny()
    value = result['category_result']
    if (type(value) is not dict or value.get('contract') not in (CONTRACT, STATUS_CONTRACT)
            or value.get('authority') != 'local_source_snapshot_only'
            or result['category_receipt_id'] != _id(run_id, step_id)):
        _deny()
    operation = value['operation']
    arguments = _arguments(operation, value['arguments'])
    contract = value['contract']
    key, name = _tools_for(operation, contract)
    run = session.scalar(_scope_query(AgentRun, actor, id=run_id))
    execution = session.scalar(_scope_query(AgentExecution, actor, run_id=run_id))
    step = session.scalar(_scope_query(AgentStep, actor, id=step_id, run_id=run_id))
    receipt = session.scalar(_scope_query(AgentPiToolReceipt, actor, id=pi_receipt_id, run_id=run_id))
    call = session.scalar(_scope_query(AgentToolCall, actor, run_id=run_id, step_id=step_id))
    if any(row is None for row in (run, execution, step, receipt, call)):
        _deny()
    if repository.runtime_identity(execution.frozen_context) != repository.SDK_RUNTIME:
        _deny()
    binding = execution.frozen_context.get('pi_readonly_binding', {})
    if _selected_contract(binding, operation) != contract:
        _deny()
    if contract == STATUS_CONTRACT and tool_registry.pi_binding_digest(binding) != run.toolset_version:
        _deny()
    output = 'sha256:' + _digest(result)
    if (run.requester_id != actor.subject_id or binding.get('requester_id') != actor.subject_id
            or binding.get('agent_id') != agent.subject_id or binding.get('run_id') != run_id
            or binding.get('organization_id') != actor.organization_id or binding.get('store_id') != actor.store_id
            or not any(tool.get('tool_key') == key and tool.get('version') == '1' and tool.get('name') == name
                       for tool in binding.get('tools', []))
            or (step.kind, step.state, step.output_ref) != ('tool', 'succeeded', output)
            or (receipt.tool_step_id, receipt.tool_kind, receipt.state, receipt.output_ref) != (step_id, 'tool', 'succeeded', output)
            or (receipt.tool_key, receipt.tool_version, receipt.pi_name, receipt.toolset_version) != (key, '1', name, run.toolset_version)
            or (call.tool_key, call.tool_version, call.input_digest) != (key, '1', receipt.input_digest)
            or call.input_digest != _digest(arguments)
            or call.idempotency_key != 'pi:' + _digest([run_id, receipt.native_call_id])):
        _deny()
    return run


def _recompute(session, actor, row, binding):
    result = row.result
    value = result['category_result']
    for key in ('input_sha256', 'result_sha256', 'directory_artifact_sha256', 'directory_content_sha256'):
        if type(getattr(row, key)) is not str or re.fullmatch('[a-f0-9]{64}', getattr(row, key)) is None:
            _deny()
    if (row.step_kind != 'tool' or row.operation != value['operation']
            or row.input_sha256 != _digest(value['arguments'])
            or row.directory_artifact_sha256 != value['directory_artifact_sha256']
            or row.directory_content_sha256 != value['directory_content_sha256']
            or row.directory_observed_at != datetime.fromisoformat(value['directory_observed_at'])
            or row.result_sha256 != _digest(result)
            or _json(_result(session, actor, row.run_id, row.step_id, row.operation, value['arguments'], binding, contract=value['contract'])) != _json(result)):
        _deny()


def stage(session, *, actor, agent, claim, step_id, pi_receipt_id, prepared, source_binding):
    """Stage facts and minimal audit in the original native result root; no commit."""
    try:
        engine = session.get_bind(); _engine(engine)
        if type(prepared) is not PreparedCategory:
            _deny()
        origin = session.info.get('guanjia_category_prepared', {}).get(id(prepared))
        if (origin is None or origin[0] is not session.get_transaction()
                or origin[1] != pi_receipt_id or origin[2] is not prepared):
            _deny()
        result = prepared.result()
        if type(actor) is WorkPrincipal and actor.run_id != claim.run_id:
            _deny()
        epoch = lock_command_authority(session, actor)
        repository.execution_for(session, claim.organization_id, claim.store_id, claim.run_id, for_update=True)
        session.scalar(_scope_query(AgentRun, actor, id=claim.run_id).with_for_update())
        execution = repository.require_worker_claim(session, claim, expected_runtime=repository.SDK_RUNTIME)
        if (execution.organization_id, execution.store_id) != (actor.organization_id, actor.store_id):
            _deny()
        with session.no_autoflush:
            _authorize(session, actor, agent)
            run = _facts(session, actor, agent, run_id=claim.run_id, step_id=step_id,
                         pi_receipt_id=pi_receipt_id, result=result)
            if run.state != 'running':
                _deny()
            value = result['category_result']
            if _json(_result(session, actor, claim.run_id, step_id, value['operation'], value['arguments'], source_binding, contract=value['contract'])) != prepared.payload_json:
                _deny()
        identity = result['category_receipt_id']
        if session.get(Receipt, identity) is not None:
            _deny()
        row = Receipt(id=identity, organization_id=actor.organization_id, store_id=actor.store_id,
            requester_id=actor.subject_id, agent_id=agent.subject_id, run_id=claim.run_id,
            step_id=step_id, step_kind='tool', pi_receipt_id=pi_receipt_id,
            operation=value['operation'], input_sha256=_digest(value['arguments']),
            directory_artifact_sha256=value['directory_artifact_sha256'],
            directory_content_sha256=value['directory_content_sha256'],
            directory_observed_at=datetime.fromisoformat(value['directory_observed_at']),
            result=result, result_sha256=_digest(result), created_at=datetime.now(timezone.utc))
        session.add(row)
        expected = {column.name: getattr(row, column.name) for column in Receipt.__table__.columns if column.name != 'result'}
        record_event(session, actor_id=actor.subject_id, organization_id=actor.organization_id,
            store_id=actor.store_id, action='guanjia.category.recorded', target_type='guanjia_category',
            target_id=identity, idempotency_key='guanjia.category:' + identity,
            details={'run_id':claim.run_id, 'step_id':step_id, 'operation':row.operation, 'result_sha256':row.result_sha256})

        def check(active, instant):
            check_command_authority(active, actor, permission='agents.run', epoch=epoch, now=instant)
            _check_claim_at_commit(active, claim, instant)
            durable = active.scalar(_scope_query(Receipt, actor, id=identity))
            if durable is None or any(getattr(durable, key) != value for key, value in expected.items()):
                _deny()
            _authorize(active, actor, agent, now=instant)
            _recompute(active, actor, durable, source_binding)
            if _facts(active, actor, agent, run_id=claim.run_id, step_id=step_id,
                      pi_receipt_id=pi_receipt_id, result=durable.result).state != 'running':
                _deny()
        commit_guard.enlist(session, action='guanjia_category', check=check)
        return {'kind': 'guanjia_category', 'id': identity, 'sha256': row.result_sha256}
    except _ERRORS:
        raise CategoryReceiptDenied() from None


def consume(engine, actor, reference, *, source_binding, expected_pin=None, authority_check=None):
    """Fresh committed read with current authority; failures disclose no values."""
    try:
        _engine(engine)
        if (type(reference) is not dict or set(reference) != {'kind', 'id', 'sha256'}
                or reference['kind'] != 'guanjia_category' or type(reference['id']) is not str
                or type(reference['sha256']) is not str):
            _deny()
        if expected_pin is not None:
            from .category_host import validate_pin
            expected_pin = validate_pin(expected_pin)
            if (expected_pin['directory_name'] != source_binding.directory_path.name
                    or expected_pin['artifact_sha256'] != source_binding.artifact_sha256):
                _deny()
        with Session(engine, autoflush=False) as session:
            if (expected_pin is not None or authority_check is not None) and engine.dialect.name == 'sqlite':
                # sqlite3 legacy transaction control does not BEGIN for SELECT.
                # Establish one real read snapshot for pin proof AND disclosure.
                connection = session.connection()
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql('BEGIN')
            # Trusted worker callback only; never IPC/model-supplied code. Its
            # machine/material proof shares this exact read snapshot with the
            # full Run pin, current person/Agent authority and recomputed page.
            if authority_check is not None:
                authority_check(session)
            row = session.scalar(_scope_query(Receipt, actor, id=reference['id'], requester_id=actor.subject_id))
            if row is None:
                return None
            if type(actor) is WorkPrincipal and actor.run_id != row.run_id:
                _deny()
            agent = AuthenticatedSubject(row.agent_id, actor.organization_id, actor.store_id, actor.auth_expires_at)
            _authorize(session, actor, agent)
            if row.result_sha256 != reference['sha256']:
                _deny()
            if expected_pin is not None:
                # PG shared row locks fence Run/Execution pin changes through
                # this read root. Full PG page-phantom acceptance stays deferred.
                execution = session.scalar(_scope_query(AgentExecution,actor,run_id=row.run_id).with_for_update(read=True))
                run = session.scalar(_scope_query(AgentRun,actor,id=row.run_id).with_for_update(read=True))
                if execution is None or run is None or run.requester_id != actor.subject_id:
                    _deny()
                binding = execution.frozen_context.get('pi_readonly_binding')
                if (type(binding) is not dict or binding.get('profile') != tool_registry.PI_SOURCE_PROFILE
                        or binding.get('category_source') != expected_pin
                        or tool_registry.pi_binding_digest(binding) != run.toolset_version
                        or row.directory_artifact_sha256 != expected_pin['artifact_sha256']
                        or row.directory_content_sha256 != expected_pin['content_sha256']
                        or row.directory_observed_at != datetime.fromisoformat(expected_pin['observed_at'])):
                    _deny()
            _facts(session, actor, agent, run_id=row.run_id, step_id=row.step_id,
                   pi_receipt_id=row.pi_receipt_id, result=row.result)
            _recompute(session, actor, row, source_binding)
            _authorize(session, actor, agent)
            if authority_check is not None:
                authority_check(session)
            return json.loads(_json(row.result))
    except _ERRORS:
        return None


_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RecursionError, HTTPException,
           SQLAlchemyError, tool_registry.ToolRegistryError, repository.AgentPersistenceError)
