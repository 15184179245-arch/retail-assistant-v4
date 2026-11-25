"""Opt-in local Pi Host assembly; synthetic, single-response diagnostics or bounded Go read-only trial.

Provisioning identities, schema and budgets is deliberately a separate action.
The offline mode has a fixed fake connection without sockets or key access.
Both real modes assemble only the fixed Go transport and a Host file descriptor;
external account approval and metering uncertainty are operator release gates.
A stale journal requires operator reconciliation, even after startup failure;
no mode resets it or adopts an existing Run.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core.security import AuthenticatedSubject
from backend.app.db.session import _configured_engine
from backend.app.modules.agents.pi_runtime_contract import contract_for_record
from backend.app.modules.agents import cost_ledger
from backend.app.modules.agents import local_sales_runtime, tool_registry
from backend.app.modules.agents.cost_service import load_price_book
from backend.app.modules.agents.models import AgentRun, BudgetPolicy, CostLedger
from backend.app.modules.agents.pi_online_campaign import PiOnlineCampaign, digest
from backend.app.modules.agents.pi_online_transport import PiOnlineTransport, HostFileCredentialSource
from backend.app.modules.agents.runtime_http import PiHttpConfig
from backend.app.modules.agents.runtime_launch import PiHostConfig, prepare_pi_release
from backend.app.modules.auth.models import EmployeeAccount
from backend.app.modules.identity.machine_service import authenticate_machine_token
from backend.app.modules.identity.models import Subject
from backend.app.modules.identity.service import authorize_scope

SANDBOX_ROOT = Path('/tmp/retail-pi-go-budget-dev').resolve()
PERSISTENT_TRIAL_ROOT = Path.home() / '.config/retail-v4/pi-preview'


def _trial_root(mode, source_enabled):
    flag = os.environ.get('RETAIL_PI_PERSISTENT_TRIAL', '0')
    if flag not in ('0', '1'):
        reject()
    if flag == '1':
        if mode != 'real-assistant-trial' or source_enabled:
            reject()
        return PERSISTENT_TRIAL_ROOT
    return SANDBOX_ROOT
MARKER = {'purpose': 'agt044-isolated-budget-development-v1', 'synthetic_data_only': True}
# Public synthetic machine credential, usable only in the marked sandbox.
SYNTHETIC_MACHINE_TOKEN = 'sx2_' + 'C' * 43
SAMPLE = '软件开发合成验收：请用只读 tasks.list_mine 列出我自己的代码审查待办，并指出 CAMPAIGN-001 是否在列表中；不创建或修改记录。'
SAMPLE_SHA256 = 'fcec3cbc008051fcc71fcee1797b3aca202ade9bcf3b7fa1a07adbcd142e8af6'
CONFIG_KEYS = {'version', 'sandbox_name', 'release_root', 'node_binary', 'node_sha256',
               'manifest_sha256', 'organization_id', 'store_id', 'requester_id',
               'agent_id', 'machine_id', 'run_id', 'sample_sha256', 'price_path',
               'price_sha256', 'expires_at'}


class BootstrapError(RuntimeError):
    pass


def reject():
    raise BootstrapError('pi_host_bootstrap_rejected')


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject()
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: reject())


def _path(raw):
    if type(raw) is not str or not Path(raw).is_absolute() or '..' in Path(raw).parts:
        reject()
    path = Path(raw)
    if path != path.resolve(strict=True):
        reject()
    return path


def _private(path, *, directory=False):
    info = path.lstat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or (not stat.S_ISDIR(info.st_mode) if directory else
                not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
        reject()


def _read(path, limit=16384):
    _private(path)
    if path.stat().st_size > limit:
        reject()
    return path.read_bytes()


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _synthetic_credential():
    return 'synthetic-no-provider-key'


def _sse(turn):
    base = {'id': 'synthetic-offline-completion', 'object': 'chat.completion.chunk',
            'model': 'deepseek-v4.1-flash'}
    delta = ({'tool_calls': [{'index': 0, 'id': 'offline_tasks_1', 'type': 'function',
              'function': {'name': 'v4_tasks_list_mine_v1', 'arguments': '{"limit":3}'}}]}
             if turn == 1 else {'content': 'Synthetic Host result received.'})
    parts = [{**base, 'choices': [{'index': 0, 'delta': delta,
               'finish_reason': 'tool_calls' if turn == 1 else 'stop'}]},
             {**base, 'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2,
               'prompt_tokens_details': {'cached_tokens': 1, 'cache_write_tokens': 0}}}]
    return ''.join('data: ' + json.dumps(row) + '\n\n' for row in parts) + 'data: [DONE]\n\n'


class _SyntheticResponse:
    status = 200

    def __init__(self, turn):
        self.body = _sse(turn).encode()
        self.offset = 0

    def getheader(self, name, default=None):
        return {'content-type': 'text/event-stream',
                'x-request-id': 'synthetic-offline-request'}.get(name, default)

    def read(self, size):
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def read1(self, size):
        return self.read(size)


class _SyntheticConnection:
    def __init__(self, journal, binding):
        self.journal, self.binding, self.turn = journal, binding, None

    def request(self, method, path, *, body, headers):
        # No URL, response, mode or factory can be selected from configuration.
        if method != 'POST' or path != '/inference/go/openai/v1/chat/completions':
            reject()
        fd = os.open(self.journal, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r+b', buffering=0) as target:
            info = os.fstat(target.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 4096):
                reject()
            fcntl.flock(target, fcntl.LOCK_EX)
            rows = [strict_json(line) for line in target.read().splitlines()]
            if (not rows or rows[0] != self.binding or len(rows) > 2
                    or any(row.get('turn') != index for index, row in enumerate(rows[1:], 1))):
                reject()
            self.turn = len(rows)
            target.seek(0, os.SEEK_END)
            target.write((json.dumps({'turn': self.turn, 'request_sha256': sha256(body).hexdigest()}) + '\n').encode())
            os.fsync(target.fileno())

    def getresponse(self):
        if self.turn not in (1, 2):
            reject()
        return _SyntheticResponse(self.turn)

    def close(self):
        pass


class _SyntheticFactory:
    def __init__(self, journal, binding):
        self.journal, self.binding = str(journal), binding

    def __call__(self, host, *, port, **kwargs):
        if host != 'opencode.ai' or port != 443:
            reject()
        return _SyntheticConnection(self.journal, self.binding)


def _budget_minimums(*, assistant, memory, output_limit, attempts):
    """Preserve the fixed per-call quote and distinct policy/remaining counts."""
    with localcontext(Context(prec=16, Emin=-18, Emax=18, clamp=0)):
        per_call = (Decimal(24576 if assistant else 4096) * Decimal('0.30')
                    + Decimal(output_limit) * Decimal('1.20')) / Decimal(1000000)
        return per_call, per_call * (attempts if memory else 2), per_call * attempts


def _assemble(config_path, expected_sha256, *, mode='offline-synthetic'):
    if mode not in ('offline-synthetic', 'real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial', 'source-assistant-trial'):
        reject()
    source_runtime = mode == 'source-assistant-trial'
    assistant = mode in ('real-assistant-trial','source-assistant-trial')
    probe = mode == 'real-probe'
    memory = mode in ('real-memory-trial', 'real-assistant-trial', 'source-assistant-trial')
    real = mode in ('real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial', 'source-assistant-trial')
    config_path = _path(config_path)
    repo = Path(__file__).resolve().parents[4]
    if config_path.is_relative_to(repo):
        reject()
    _private(config_path.parent, directory=True)
    raw = _read(config_path)
    direct_access = os.environ.get('RETAIL_PI_DIRECT_ACCESS') == '1'
    if not (direct_access and expected_sha256 in (None, '', 'direct')):
        if not re.fullmatch('[0-9a-f]{64}', expected_sha256) or sha256(raw).hexdigest() != expected_sha256:
            reject()
    cfg = strict_json(raw)
    keys = CONFIG_KEYS | {'credential_path', 'artifact_directory'} if real else CONFIG_KEYS
    if assistant:
        keys = (keys - {'run_id', 'sample_sha256'}) | {'core_manifest_sha256'}
        if type(cfg) is dict and cfg.get('version') in (7,8,9,10,11,12,13,14,15,16):
            keys = keys | {'work_authority_enabled'}
        if type(cfg) is dict and cfg.get('version') in (8,9,10,11,12,13,14,15,16):
            keys=keys|{'turns_enabled','turns_manifest_sha256'}
        if type(cfg) is dict and cfg.get('version') in (9,10,11,12,13,14,15,16):
            keys=keys|{'inbox_enabled'}
        if type(cfg) is dict and cfg.get('version') in (10,11,12,13,14,15,16):
            keys = keys | {'discovery_enabled'}
        if type(cfg) is dict and cfg.get('version') in (11,12,13,14,15,16):
            keys = keys | {'source_enabled'}
        if source_runtime:
            keys = (keys - {'sandbox_name'}) | {'environment_sha256'}
            if cfg.get('version') == 12 and 'research' in cfg:
                keys |= {'research'}
            if cfg.get('version') in (14,15) or cfg.get('version') == 16 and 'sales' in cfg:
                keys |= {'sales'}
            if cfg.get('version') == 13:
                keys |= {'category_directory_name','category_artifact_sha256'}
            if cfg.get('version') == 16:
                # The workbench profile is the only tool_profile override:
                # an explicit configured key/version 2D selection, never all.
                keys |= {'tool_profile', 'tool_selection'}
    elif memory:
        keys = keys | {'sample_text'}
    optional_keys = ({'expires_at', 'environment_sha256'} & keys) if (assistant and direct_access) else set()
    if (type(cfg) is not dict or not (keys - optional_keys) <= set(cfg) <= keys
            or type(cfg['version']) is not int
            or cfg['version'] not in {'offline-synthetic':(1,),'real-probe':(2,),'real-readonly-trial':(3,4),'real-memory-trial':(5,),'real-assistant-trial':(6,7,8,9,10,11), 'source-assistant-trial':(12,13,14,15,16)}[mode]):
        reject()
    # Actual Host SQLite FD safety is not proved by path/inode checks.
    # Keep version12 price/stock intact; never assemble real category version13.
    if cfg['version'] == 13:
        reject()
    discovery_enabled = cfg.get('discovery_enabled', False)
    if type(discovery_enabled) is not bool or (cfg['version'] in (10,11,12,13,14,15,16) and not discovery_enabled):
        reject()
    source_enabled = cfg.get('source_enabled',False)
    if (type(source_enabled) is not bool or (cfg['version'] in (11,12,13,14,15,16) and not source_enabled)
            or source_enabled and not discovery_enabled):
        reject()
    # Workbench version16: the explicit tool_selection is a configured upper
    # bound of key/version pairs; it is never defaulted to the whole catalog.
    workbench_runtime = cfg.get('version') == 16
    if workbench_runtime:
        selection_pairs = cfg.get('tool_selection')
        if (cfg.get('tool_profile') != 'pi-workbench-v1'
                or type(selection_pairs) is not list or not selection_pairs
                or any(type(pair) is not list or len(pair) != 2
                       or type(pair[0]) is not str or type(pair[1]) is not str
                       for pair in selection_pairs)
                or sorted([tuple(pair) for pair in selection_pairs]) != [tuple(pair) for pair in selection_pairs]
                or len({tuple(pair) for pair in selection_pairs}) != len(selection_pairs)):
            reject()
        tool_selection = tuple((pair[0], pair[1]) for pair in selection_pairs)
        if ((local_sales_runtime.KEY, '3') in tool_selection) != ('sales' in cfg):
            reject()
    else:
        tool_selection = ()
    source_opt_in = os.environ.get('RETAIL_PI_SOURCE_OPT_IN')
    if source_opt_in is not None and source_opt_in != ('1' if source_enabled else '0'):
        reject()
    cli_opt_in = os.environ.get('RETAIL_PI_DISCOVERY_OPT_IN')
    if cli_opt_in is not None and cli_opt_in != ('1' if discovery_enabled else '0'):
        reject()
    discovery_resources = None
    if discovery_enabled:
        from backend.app.modules.agents import service as agent_service
        m,t,s,d,o = (agent_service.MAX_MODEL_CALLS, agent_service.MAX_TOOL_CALLS,
            agent_service.MAX_STEPS, agent_service.MAX_SECONDS, agent_service.MAX_OUTPUT_TOKENS)
        if (any(type(v) is not int for v in (m,t,s,d,o)) or not 4 <= m <= 32
                or not 4 <= t <= 64 or not m+t <= s <= 96 or not 60 <= d <= 900
                or not 8192*m <= o <= 262144):
            reject()
        discovery_resources = (m,d)
    work_authority_enabled = cfg.get('work_authority_enabled', False)
    if type(work_authority_enabled) is not bool or source_enabled and not work_authority_enabled:
        reject()
    turns_enabled=cfg.get('turns_enabled',False)
    if type(turns_enabled) is not bool or (turns_enabled and not work_authority_enabled):reject()
    if cfg['version'] in (8,9,10,11,12,13,14,15,16) and (type(cfg['turns_manifest_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',cfg['turns_manifest_sha256'])):reject()
    inbox_enabled=cfg.get('inbox_enabled',False)
    if type(inbox_enabled) is not bool or (inbox_enabled and not turns_enabled):reject()
    if source_runtime and not all((source_enabled,discovery_enabled,work_authority_enabled,turns_enabled,inbox_enabled)):
        reject()
    output_limit = 8192 if discovery_enabled else 2048 if cfg['version'] in (4, 5, 6, 7, 8, 9) else 256
    attempts = discovery_resources[0] if discovery_enabled else 1 if probe else 3 if memory else 2
    if assistant:
        if os.environ.get('MEMORY_DURABLE_ENABLED') != 'true':
            reject()
    elif memory:
        from backend.app.modules.agents.runtime_http import PiRequest
        PiRequest(text=cfg['sample_text'], run_id=cfg['run_id'])
        if '\x00' in cfg['sample_text'] or os.environ.get('MEMORY_DURABLE_ENABLED') != 'true':
            reject()
        sample_sha256 = sha256(cfg['sample_text'].encode('utf-8')).hexdigest()
    else:
        sample_sha256 = SAMPLE_SHA256
    minimum_per_call, minimum_total, minimum_attempts_total = _budget_minimums(
        assistant=assistant, memory=memory, output_limit=output_limit, attempts=attempts)
    environment = None
    isolated_research = False
    machine_token = SYNTHETIC_MACHINE_TOKEN
    if source_runtime:
        from backend.app.modules.agents.source_environment import load
        from .research_host import validate_pin as research_pin
        selection=research_pin(cfg['research']) if 'research' in cfg else {}
        env_sha = (cfg.get('environment_sha256') or 'direct') if direct_access else cfg['environment_sha256']
        environment = load(env_sha,trial_name=selection.get('trial_name'))
        isolated_research=environment.trial_name is not None
        environment.check_binding(cfg)
        if config_path != environment.root / 'host.json':
            reject()
        machine_token = environment.machine_token
    else:
        if not re.fullmatch('[a-z0-9][a-z0-9-]{0,39}', cfg['sandbox_name']):
            reject()
        for key in ('organization_id', 'store_id', 'requester_id', 'agent_id', 'machine_id'):
            if not re.fullmatch('agt044-dev-[a-z0-9-]{1,25}', cfg[key]):
                reject()
    if not assistant and (str(UUID(cfg['run_id'])) != cfg['run_id'] or cfg['sample_sha256'] != sample_sha256):
        reject()
    now = datetime.now(timezone.utc)
    raw_expiry = cfg.get('expires_at')
    horizon_direct = assistant and direct_access
    if horizon_direct:
        if raw_expiry is not None:
            parsed_expiry = datetime.fromisoformat(raw_expiry)
            if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() != timezone.utc.utcoffset(None):
                reject()
            expiry = parsed_expiry if parsed_expiry > now else now + timedelta(days=30)
        else:
            expiry = now + timedelta(days=30)
    else:
        expiry = datetime.fromisoformat(raw_expiry)
        if expiry.tzinfo is None or expiry.utcoffset() != timezone.utc.utcoffset(None) or expiry <= now:
            reject()
        if assistant and expiry > now + timedelta(hours=24):
            reject()
    trial_root = _trial_root(mode, source_enabled)
    folder = environment.root if environment else _path(str(trial_root / cfg['sandbox_name']))
    if environment is None:
        _private(_path(str(trial_root)), directory=True)
    _private(folder, directory=True)
    if not assistant:
        journal = folder / {'offline-synthetic':'pi-offline-journal.jsonl',
            'real-probe':'pi-real-probe-journal.jsonl','real-readonly-trial':'pi-real-readonly-trial-journal.jsonl',
            'real-memory-trial':'pi-memory-' + cfg['run_id'] + '-journal.jsonl'}[mode]
        if os.path.lexists(journal):
            reject()
    if environment is None and strict_json(_read(folder / 'synthetic-only.json', 1024)) != MARKER:
        reject()
    if real:
        # This config is an operator authorization boundary, not an assertion
        # that Go account/tokenizer conditions have been externally verified.
        # No account flags or arbitrary network settings can be self-reported.
        from backend.app.modules.agents.pi_probe_artifacts import validate_directory
        validate_directory(str(config_path.parent))
        artifacts = _path(cfg['artifact_directory'])
        validate_directory(str(artifacts))
        if artifacts.is_relative_to(repo) or (not assistant and any(artifacts.iterdir())):
            reject()
        credential = cfg['credential_path']
        if (type(credential) is not str or not Path(credential).is_absolute()
                or '..' in Path(credential).parts or '\x00' in credential
                or Path(credential).is_relative_to(repo)):
            reject()
        # Only the deadline-bound send worker reads/validates the key file.
        source = HostFileCredentialSource(credential)
    database = environment.database if environment else _path(str(folder / 'budget.sqlite'))
    _private(database)
    if os.environ.get('DATABASE_URL') != 'sqlite:///' + str(database):
        reject()
    # Authentication and HTTP Host sessions use this exact cached engine.
    engine = _configured_engine()
    if engine.url.drivername != 'sqlite' or engine.url.database != str(database):
        reject()
    if (os.environ.get('AGENT_SUBJECT_ID') != cfg['agent_id'] or
            any(os.environ.get(key) != '1' for key in
                ('AGENT_WORKER_SUBMIT_ENABLED', 'AGENT_WORKER_RUNTIME_ENABLED', 'AGENT_WORKER_DISPATCH_ENABLED'))):
        reject()
    prices = _path(cfg['price_path'])
    if sha256(prices.read_bytes()).hexdigest() != cfg['price_sha256'] or os.environ.get('AGENT_PRICE_CONFIG_PATH') != str(prices):
        reject()
    price = load_price_book(prices).select('opencode-go', 'deepseek-v4.1-flash', now)
    if (price is None or price.effective_until is None
            or (_utc(price.effective_until) <= now if horizon_direct else price.effective_until < expiry)
            or price.currency != 'USD' or price.usage_contract != 'disjoint-v1'
            or price.provenance != 'verified'
            or dict(price.rates_per_million) != {'input': '0.30', 'output': '1.20'}):
        reject()
    operational_horizons = [_utc(price.effective_until)]
    root, node = _path(cfg['release_root']), _path(cfg['node_binary'])
    if root.is_relative_to(repo) or sha256(node.read_bytes()).hexdigest() != cfg['node_sha256']:
        reject()
    launch = PiHostConfig(root, root / 'node_modules', node, cfg['manifest_sha256'])
    prepared = prepare_pi_release(launch)
    record = prepared.runtime_record()
    expected_family = 'workbench' if workbench_runtime else 'source' if source_enabled else 'discovery' if discovery_enabled else 'memory' if memory else 'readonly'
    contract = contract_for_record(record)
    if not contract.online or contract.family != expected_family:
        reject()
    if workbench_runtime and ('sales' in cfg or any(pair in tool_registry.GUANJIA_SOURCE_TOOLS for pair in tool_selection)):
        from backend.app.modules.integrations.guanjia import sqlite_identity
        sqlite_identity._artifact()
    # Only run the Node already authenticated by the release verifier.
    version = subprocess.run([str(node), '--version'], env={}, capture_output=True, timeout=5, check=True)
    if version.stdout.strip() != b'v24.22.1':
        reject()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    # query_only is connection-local and is restored before releasing it.
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA query_only=ON')
        try:
            from sqlalchemy.orm import Session
            with Session(bind=connection) as db:
                principal = authenticate_machine_token(db, machine_token)
                if (principal.machine_id, principal.organization_id, principal.store_id, principal.purpose) != (
                        cfg['machine_id'], cfg['organization_id'], cfg['store_id'], 'worker'):
                    reject()
                if (_utc(principal.auth_expires_at) <= now if horizon_direct else principal.auth_expires_at < expiry):
                    reject()
                operational_horizons.append(_utc(principal.auth_expires_at))
                if assistant:
                    from backend.app.modules.identity.machine_service import authorize_machine_action
                    for action in ('worker.agent.execute', 'worker.agent.recover'):
                        authorize_machine_action(db, principal, organization_id=cfg['organization_id'],
                                                 store_id=cfg['store_id'], action=action, now=now)
                account = db.scalar(select(EmployeeAccount).where(
                    EmployeeAccount.subject_id == cfg['requester_id'], EmployeeAccount.active.is_(True),
                    EmployeeAccount.organization_id == cfg['organization_id'], EmployeeAccount.store_id == cfg['store_id']))
                if account is None:
                    reject()
                for subject, kind, permissions in (
                    (cfg['requester_id'], 'person',
                     ('agents.run','tasks.read_self','tasks.manage','knowledge.read') if isolated_research else
                     ('agents.run', 'memory.read', 'tasks.read_self', 'tasks.manage') if assistant else
                     ('agents.run', 'memory.read' if memory else 'tasks.read_self')),
                    (cfg['agent_id'], 'agent', ('agents.tasks.list_mine','agents.knowledge.search') if isolated_research else ('agents.memory.search' if memory else 'agents.tasks.list_mine',))):
                    row = db.get(Subject, subject)
                    if row is None or row.subject_type != kind:
                        reject()
                    actor = AuthenticatedSubject(subject, cfg['organization_id'], cfg['store_id'], expiry)
                    for permission in permissions:
                        authorize_scope(db, actor, organization_id=cfg['organization_id'],
                                        store_id=cfg['store_id'], permission=permission)
                if source_runtime and not isolated_research and not workbench_runtime:
                    for subject, permissions in (
                        (cfg['requester_id'],('catalog.read','guanjia.company.read','guanjia.pricing.read','guanjia.inventory.read')),
                        (cfg['agent_id'],('agents.catalog.search','agents.guanjia.prices.read','agents.guanjia.stock.read'))):
                        actor = AuthenticatedSubject(subject,cfg['organization_id'],cfg['store_id'],expiry)
                        for permission in permissions:
                            authorize_scope(db,actor,organization_id=cfg['organization_id'],store_id=cfg['store_id'],permission=permission)
                if workbench_runtime:
                    person = AuthenticatedSubject(cfg['requester_id'], cfg['organization_id'], cfg['store_id'], expiry)
                    agent = AuthenticatedSubject(cfg['agent_id'], cfg['organization_id'], cfg['store_id'], expiry)
                    for pair in tool_selection:
                        definition = tool_registry.GUANJIA_SOURCE_TOOLS.get(pair)
                        if pair == ('catalog.search', '1'):
                            definition = tool_registry.resolve_tool(*pair)
                        if definition is None:
                            continue
                        grants = ((person, (definition.requester_permission, *definition.extra_requester_permissions)),
                                  (agent, (definition.agent_permission,)))
                        for actor, permissions in grants:
                            for permission in permissions:
                                authorize_scope(db, actor, organization_id=cfg['organization_id'],
                                                store_id=cfg['store_id'], permission=permission)
                for scope, subject in (('employee', cfg['requester_id']), ('store', None)):
                    policies = list(db.scalars(select(BudgetPolicy).where(
                        BudgetPolicy.organization_id == cfg['organization_id'], BudgetPolicy.store_id == cfg['store_id'],
                        BudgetPolicy.scope_type == scope, BudgetPolicy.subject_id == subject,
                        BudgetPolicy.active.is_(True), BudgetPolicy.effective_from <= now,
                        BudgetPolicy.effective_until > now)))
                    if len(policies) != 1:
                        reject()
                    policy = policies[0]
                    spent = cost_ledger._spent(db, policy)
                    policy_column = CostLedger.employee_budget_id if scope == 'employee' else CostLedger.store_budget_id
                    unsettled = db.scalar(select(CostLedger.id).where(
                        CostLedger.organization_id == cfg['organization_id'],
                        CostLedger.store_id == cfg['store_id'], policy_column == policy.id,
                        CostLedger.status.not_in(('settled', 'released'))).limit(1)) if memory and not assistant else None
                    # A persistent assistant does not adopt old attempts on
                    # restart. Their unknown reservations still count in spent,
                    # but must not make the whole service unstartable. Bounded
                    # diagnostics retain their clean-ledger prerequisite.
                    total_limit = cost_ledger._limit(policy)
                    call_limit = cost_ledger._per_call_limit(policy)
                    # The persistent assistant follows its explicit cost policy.
                    # The old five-cent ceiling belongs only to bounded probes;
                    # it must not silently impose a product budget on its owner.
                    capacity_invalid = (
                        (total_limit is not None and total_limit < minimum_total)
                        or (call_limit is not None and call_limit < minimum_per_call)
                        or (total_limit is not None and call_limit is not None and call_limit > total_limit)
                        or (not assistant and (total_limit is None or call_limit is None or total_limit > Decimal('0.05')))
                    )
                    if ((policy.provider_key, policy.model, policy.price_version, policy.currency) !=
                            ('opencode-go', 'deepseek-v4.1-flash', price.version, 'USD')
                            or (_utc(policy.effective_until) <= now if horizon_direct else _utc(policy.effective_until) < expiry)
                            or capacity_invalid
                            or cost_ledger._reconciliation_blocked(db, policy)
                            or unsettled is not None
                            or (cost_ledger._exceeds(cost_ledger._sum_amounts((spent, minimum_attempts_total)), total_limit) if memory else spent != 0)):
                        reject()
                    operational_horizons.append(_utc(policy.effective_until))
                # All SDK receipts now probe both exact-run grant tables. Fail before
                # starting even a legacy diagnostic against an unprepared schema.
                from backend.app.modules.assistant.models import AssistantWorkAuthority,AssistantTurnAuthority,AssistantTurnDispatch
                for model in (AssistantWorkAuthority,AssistantTurnAuthority,AssistantTurnDispatch):db.execute(select(model).limit(0))
                # A restart may never adopt any existing Run, including unknown.
                if not assistant and db.get(AgentRun, cfg['run_id']) is not None:
                    reject()
                if discovery_enabled:
                    # Read constraint identity; startup never repairs schema.
                    from sqlalchemy import inspect
                    checks = inspect(connection).get_check_constraints('agent_pi_provider_receipts')
                    expected_wire = f"'{contract.wire_protocol}'"
                    if not any(c['name'] == 'ck_pi_provider_identity' and expected_wire in c['sqltext'] for c in checks):
                        reject()
                if source_enabled and not isolated_research:
                    from backend.app.modules.integrations.guanjia.observation_models import GuanjiaObservationReceipt
                    db.execute(select(GuanjiaObservationReceipt).limit(0))
                if assistant:
                    from backend.app.modules.assistant.models import AssistantWork, AssistantDispatch, AssistantInbox
                    # Read existing schema only. Startup never creates tables.
                    db.execute(select(AssistantWork).limit(0))
                    db.execute(select(AssistantDispatch).limit(0))
                    from backend.app.modules.assistant.models import AssistantWorkAuthority,AssistantTurn,AssistantTurnDispatch,AssistantTurnAuthority
                    from backend.app.modules.identity.models import ScopeSecurityFence
                    for model in (AssistantTurn,AssistantTurnDispatch,AssistantInbox):
                        db.execute(select(model).limit(0))
                    db.execute(select(ScopeSecurityFence.work_version).limit(0))
        finally:
            connection.rollback()
            connection.exec_driver_sql('PRAGMA query_only=OFF')
            connection.rollback()
    if horizon_direct and operational_horizons:
        expiry = min(operational_horizons)
    if environment is not None:
        environment.revalidate()
    category_source = None
    research_source = None
    sales_source = None
    local_sales = cfg['version'] == 15 or workbench_runtime and 'sales' in cfg
    if local_sales:
        from backend.app.modules.integrations.guanjia import sales_ranking_source
        selection=cfg['sales']
        if (type(selection) is not dict or set(selection)!={'descriptor_sha256'} or
                type(selection['descriptor_sha256']) is not str or
                not re.fullmatch('[a-f0-9]{64}',selection['descriptor_sha256'])):reject()
        with factory() as db:
            for subject,permissions in ((cfg['requester_id'],('guanjia.sales.read','guanjia.company.read')),
                    (cfg['agent_id'],('agents.guanjia.sales.read',))):
                actor=AuthenticatedSubject(subject,cfg['organization_id'],cfg['store_id'],expiry)
                for permission in permissions:
                    authorize_scope(db,actor,organization_id=cfg['organization_id'],
                                    store_id=cfg['store_id'],permission=permission)
        sales_source=sales_ranking_source.configure_local(factory.kw['bind'],environment,
            descriptor_sha256=selection['descriptor_sha256'])
    if cfg['version'] == 14:
        from backend.app.modules.integrations.guanjia import sales_ranking_source
        selection = cfg['sales']
        if (type(selection) is not dict or set(selection) != {'material_sha256','tenant_id','tenancy_sha256'}
                or type(selection['tenant_id']) is not int or selection['tenant_id'] <= 0
                or any(type(selection[k]) is not str or not re.fullmatch('[a-f0-9]{64}', selection[k])
                       for k in ('material_sha256','tenancy_sha256'))): reject()
        # Validate already provisioned dual-subject authority; never grant it.
        with factory() as db:
            for subject, permissions in ((cfg['requester_id'], ('guanjia.sales.read','guanjia.company.read')),
                    (cfg['agent_id'], ('agents.guanjia.sales.read',))):
                actor = AuthenticatedSubject(subject,cfg['organization_id'],cfg['store_id'],expiry)
                for permission in permissions:
                    authorize_scope(db, actor, organization_id=cfg['organization_id'],
                        store_id=cfg['store_id'], permission=permission)
        sales_source = sales_ranking_source.configure(factory.kw['bind'], environment,
            material_sha256=selection['material_sha256'], expected_tenant_id=selection['tenant_id'],
            tenancy_sha256=selection['tenancy_sha256'])

    if 'research' in cfg:
        from . import research_host
        pin=research_host.validate_pin(cfg['research'])
        if not source_runtime or cfg['version']!=12 or environment is None:reject()
        research_source=research_host.configure(factory.kw['bind'],environment,pin['directory_name'],pin['manifest_sha256'],read=pin.get('read'))
        if 'read' in pin:
            from .research_broker_host import required_permissions
            with engine.connect() as readonly:
                readonly.exec_driver_sql('PRAGMA query_only=ON')
                try:
                    with Session(bind=readonly,autoflush=False) as checks:
                        person=AuthenticatedSubject(cfg['requester_id'],cfg['organization_id'],cfg['store_id'],expiry)
                        agent=AuthenticatedSubject(cfg['agent_id'],cfg['organization_id'],cfg['store_id'],expiry)
                        for permission in ('knowledge.read',*required_permissions(research_source)):
                            authorize_scope(checks,person,organization_id=cfg['organization_id'],store_id=cfg['store_id'],permission=permission)
                        for permission in ('agents.knowledge.search','agents.guanjia.research.read'):
                            tool_registry._agent_scope(checks,person,agent,permission)
                finally:
                    readonly.rollback();readonly.exec_driver_sql('PRAGMA query_only=OFF');readonly.rollback()
    if assistant:
        from backend.app.modules.assistant.core_adapter import CoreConfig, DshCoreAdapter
        from backend.app.modules.assistant.online import AssistantOnlinePolicy
        from backend.app.modules.assistant.dispatch import AssistantWorkerConfig
        from backend.app.modules.assistant.lifecycle import ManagedAssistant
        if workbench_runtime:
            available_names = tool_registry._pi_names(tool_registry.PI_WORKBENCH_PROFILE,
                sales_source=sales_source.pin_dict() if local_sales else None,
                sales_contract='3' if local_sales else '1')
            if not set(tool_selection) <= available_names.keys():
                reject()
        core = DshCoreAdapter(CoreConfig(node, cfg['node_sha256'], cfg['core_manifest_sha256']))
        core._verify()
        pi = PiHttpConfig(launch, factory, machine_token, cfg['organization_id'],
                          cfg['store_id'], root / 'runtime/http', PiOnlineTransport(source), category_source=category_source, research_source=research_source, sales_source=sales_source)
        policy = AssistantOnlinePolicy(cfg['organization_id'], cfg['store_id'], cfg['requester_id'],
            cfg['machine_id'], prepared.manifest_sha256, digest(cost_ledger._price_snapshot(price)), expiry, artifacts, discovery_enabled=discovery_enabled, source_enabled=source_enabled,
            workbench_enabled=workbench_runtime,
            **({'max_attempts': attempts, 'run_seconds': discovery_resources[1], 'max_output_tokens': output_limit}
               if discovery_enabled else {}))
        turns=None
        if cfg['version'] in (8,9,10,11,12,13,14,15,16):
            from backend.app.modules.assistant.turn_adapter import DshTurnsAdapter,MANIFEST_TURNS,ENTRY_TURNS
            from backend.app.modules.assistant.core_adapter import ROOT,ENTRY,CORE_COMMIT
            manifest=ROOT/MANIFEST_TURNS
            if manifest.is_symlink() or sha256(manifest.read_bytes()).hexdigest()!=cfg['turns_manifest_sha256']:reject()
            content=strict_json(manifest.read_bytes())
            expected={ENTRY,ENTRY_TURNS}|{str(p.relative_to(ROOT)) for p in (ROOT/'third_party/dsh-superagent-prototype/src/core').glob('*.ts')}
            if content['entry']!=ENTRY_TURNS or content['core_commit']!=CORE_COMMIT or set(content['files'])!=expected:reject()
            for name,value in content['files'].items():
                file=ROOT/name
                if file.is_symlink() or sha256(file.read_bytes()).hexdigest()!=value:reject()
            if turns_enabled:turns=DshTurnsAdapter(CoreConfig(node,cfg['node_sha256'],cfg['turns_manifest_sha256']))
        from backend.app.modules.assistant.authority import WorkAuthorityPolicy
        from backend.app.modules.assistant.principal import DEFAULT_CAPABILITIES
        work_capabilities=frozenset({'agents.run','tasks.manage','tasks.read_self','knowledge.read'}) if isolated_research else DEFAULT_CAPABILITIES
        if sales_source is not None:
            work_capabilities = work_capabilities | frozenset({'guanjia.sales.read'})
        if research_source is not None and 'read' in research_source.pin():
            from .research_broker_host import required_permissions
            work_capabilities=work_capabilities|frozenset(required_permissions(research_source))
        if workbench_runtime:
            # The workbench host may delegate workspace artifact kinds.
            from backend.app.modules.assistant.principal import _WORKSPACE_PERMISSIONS
            work_capabilities = work_capabilities | _WORKSPACE_PERMISSIONS
        inbox_policy=WorkAuthorityPolicy(expiry,tuple(sorted(work_capabilities))) if inbox_enabled else None
        return ManagedAssistant(core, AssistantWorkerConfig(pi, online_policy=policy, discovery=discovery_enabled, source=source_enabled,
            work_authority_enabled=work_authority_enabled,turns=turns,inbox_policy=inbox_policy,
            tool_profile=('pi-workbench-v1' if workbench_runtime else ''),
            tool_selection=tool_selection))
    campaign = PiOnlineCampaign(cfg['organization_id'], cfg['store_id'], cfg['requester_id'],
        cfg['machine_id'], cfg['run_id'], sample_sha256, prepared.manifest_sha256,
        digest(cost_ledger._price_snapshot(price)), expiry, enabled=True,max_output_tokens=output_limit,
        **({'mode': mode, 'max_attempts': attempts,
            'max_request_body_bytes': 8192 if probe else 32768 if memory else 16384,
            'artifact_directory': str(artifacts)} if real else {}))
    campaign.validate()
    binding = {'config_sha256': expected_sha256, 'run_id': cfg['run_id'], 'sample_sha256': sample_sha256}
    # Atomic exclusive creation also prevents concurrent Hosts or stale replay.
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write((json.dumps(binding, sort_keys=True) + '\n').encode())
        target.flush()
        os.fsync(target.fileno())
    transport = (PiOnlineTransport(source) if real else
                 PiOnlineTransport(_synthetic_credential, _connection_factory=_SyntheticFactory(journal, binding)))
    campaign.check_transport(transport)
    return PiHttpConfig(launch, factory, machine_token, cfg['organization_id'],
                        cfg['store_id'], root / 'runtime/http', transport, campaign)


@asynccontextmanager
async def pi_host_lifespan(app):
    mode = os.environ.get('RETAIL_PI_HOST_MODE', 'disabled')
    config = None
    state_key = 'pi_memory' if mode == 'real-memory-trial' else 'pi_readonly'
    try:
        if mode in ('real-assistant-trial','source-assistant-trial'):
            if (hasattr(app.state, 'pi_readonly') or hasattr(app.state, 'pi_memory')
                    or hasattr(app.state, 'assistant_core') or hasattr(app.state, 'assistant_host')
                    or app.dependency_overrides):
                reject()
            try:
                host = _assemble(os.environ.get('RETAIL_PI_HOST_CONFIG', ''),
                                 os.environ.get('RETAIL_PI_HOST_CONFIG_SHA256', ''), mode=mode)
            except Exception:
                raise BootstrapError('pi_host_bootstrap_rejected') from None
            async with host.lifespan(app):
                yield
            return
        if mode != 'disabled':
            if (mode not in ('offline-synthetic', 'real-probe', 'real-readonly-trial', 'real-memory-trial')
                    or hasattr(app.state, 'pi_readonly') or hasattr(app.state, 'pi_memory') or app.dependency_overrides):
                reject()
            try:
                config = _assemble(os.environ.get('RETAIL_PI_HOST_CONFIG', ''),
                                   os.environ.get('RETAIL_PI_HOST_CONFIG_SHA256', ''), mode=mode)
            except Exception:
                raise BootstrapError('pi_host_bootstrap_rejected') from None
            setattr(app.state, state_key, config)
        yield
    finally:
        if config is not None and getattr(app.state, state_key, None) is config:
            delattr(app.state, state_key)
