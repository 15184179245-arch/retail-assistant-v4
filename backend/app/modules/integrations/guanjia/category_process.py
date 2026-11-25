"""Fixed fresh-exec category candidate. Production version13 remains held.

Only the dedicated worker opens authority connections. Every connection uses
actual-FD attestation; paths and hashes carried over IPC are never authority.
Dispatch intent is committed by the parent; an ambiguous child is never retried.
"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents.process_transport import exchange_committed
from backend.app.modules.agents import repository, tool_registry, answer_receipts
from backend.app.modules.agents import capability_discovery as discovery
from backend.app.modules.agents import source_environment
from backend.app.modules.assistant.principal import WorkPrincipal
from backend.app.modules.identity.machine_service import authenticate_machine_token, authorize_machine_action
from backend.app.workers import agent_executor as executor
from backend.app.workers.agent_guard import guard_execution
from . import category_receipts as receipts, category_host, live_loader as protected, sqlite_identity
from .work_source_process import _exchange, _cleanup

_ROOT = Path(__file__).resolve().parents[5]
_COMMAND = (sys.executable, '-I', '-c', 'import sys,runpy;sys.path.insert(0,'+repr(str(_ROOT))+');'
            'runpy.run_module("backend.app.workers.guanjia_category_source",run_name="__main__")')
_TIMEOUT = 30.0
_IN_WORKER = False


def _digest(value):
    return receipts._digest(value)


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 16384:
        raise ValueError()
    value = protected._decode(raw)
    protected._shape(value, ('version', 'mode', 'environment_sha256', 'pin', 'payload'))
    if type(value['version']) is not int or value['version'] != 1 or value['mode'] not in ('execute', 'consume'):
        raise ValueError()
    protected._digest(value['environment_sha256'])
    category_host.validate_pin(value['pin'])
    if value['mode'] == 'execute':
        p = protected._shape(value['payload'], ('claim','step_id','run_version','step_version','decision','name'))
        c = protected._shape(p['claim'], ('run_id','organization_id','store_id','epoch','machine_id'))
        for k,v in c.items():
            (protected._integer if k == 'epoch' else protected._text)(v)
        for k in ('run_version','step_version'): protected._integer(p[k])
        protected._text(p['step_id'])
        decision = protected._shape(p['decision'], ('tool_key','version','arguments','native_call_id','toolset_version'))
        operation, contract = receipts.native_contract(decision['tool_key'], decision['version'], p['name'])
        receipts._arguments(operation, decision['arguments'])
        protected._text(decision['native_call_id'])
        if not decision['toolset_version'].startswith('sha256:'): raise ValueError()
        protected._digest(decision['toolset_version'][7:])
    else:
        p = protected._shape(value['payload'], ('actor','actor_kind','reference'))
        cls = WorkPrincipal if p['actor_kind'] == 'work' else AuthenticatedSubject if p['actor_kind'] == 'login' else None
        if cls is None or type(p['actor']) is not dict or set(p['actor']) != set(cls.__dataclass_fields__): raise ValueError()
        actor = dict(p['actor']); actor['auth_expires_at'] = datetime.fromisoformat(actor['auth_expires_at'])
        if actor['auth_expires_at'].tzinfo is None: raise ValueError()
        cls(**actor)
        r = protected._shape(p['reference'], ('kind','id','sha256'))
        if r['kind'] != 'guanjia_category': raise ValueError()
        protected._text(r['id']); protected._digest(r['sha256'])
    return value


@contextmanager
def _loaded(request):
    global _IN_WORKER
    protected._process_gate()
    environment = source_environment.load(request['environment_sha256'])
    files = engine = None
    previous = os.environ.get('AGENT_SUBJECT_ID')
    previous_worker = _IN_WORKER
    try:
        files = protected._Files(environment.root)
        files.read('environment.json')
        files.read('authority.sqlite3', database=True)
        info = next(info for name,fd,info,database in files.files if database)
        if (info.st_dev,info.st_ino) != environment.database_identity: raise ValueError()
        def connect():
            environment.revalidate(); files.validate()
            connection = sqlite_identity.connect(environment.database, environment.database_identity)
            try:
                environment.revalidate(); files.validate()
                connection.execute('PRAGMA foreign_keys=ON')
                # Bound parent transaction waits; no indefinite/repeated dispatch.
                connection.execute('PRAGMA busy_timeout=1000')
                return connection
            except BaseException:
                connection.close(); raise
        engine = create_engine(URL.create('sqlite',database=str(environment.database)),
                               creator=connect,poolclass=NullPool,hide_parameters=True)
        selected = category_host.configure(engine,environment,request['pin']['directory_name'],request['pin']['artifact_sha256'])
        if selected.pin() != request['pin']: raise ValueError()
        os.environ['AGENT_SUBJECT_ID'] = environment.values['agent_id']
        _IN_WORKER = True
        yield engine,environment,selected
    finally:
        _IN_WORKER = previous_worker
        if engine is not None:
            engine.dispose()
        if files is not None: files.close()
        if previous is None: os.environ.pop('AGENT_SUBJECT_ID',None)
        else: os.environ['AGENT_SUBJECT_ID'] = previous


def _machine(session, environment, *, now=None):
    now = repository._db_clock(session) if now is None else now
    principal = authenticate_machine_token(session,environment.machine_token,now=now)
    values = environment.values
    if (principal.machine_id,principal.organization_id,principal.store_id,principal.purpose) != (
            values['machine_id'],values['organization_id'],values['store_id'],'worker'): raise ValueError()
    authorize_machine_action(session,principal,organization_id=values['organization_id'],
        store_id=values['store_id'],action='worker.agent.execute',now=now)


def _consume(engine, environment, selected, actor, reference):
    if (actor.subject_id,actor.organization_id,actor.store_id) != tuple(environment.values[k] for k in ('requester_id','organization_id','store_id')):
        raise ValueError()
    def authority_check(session):
        _machine(session,environment)
        environment.revalidate(); selected.check(engine)
    # Machine/material and person/agent/pin/page checks share the same fresh
    # protected read transaction. History does not need the old execution lease.
    return receipts.consume(engine,actor,reference,source_binding=selected.binding(),
        expected_pin=selected.pin(),authority_check=authority_check)


def _execute(engine, environment, selected, payload):
    from backend.app.application import commit_guard
    from backend.app.modules.agents.schemas import StepState, StepTransition
    claim = repository.AgentClaim(**payload['claim'],runtime_identity=repository.SDK_RUNTIME)
    decision = payload['decision']; name = payload['name']; sid = payload['step_id']
    values = environment.values
    if (claim.organization_id,claim.store_id,claim.machine_id) != tuple(values[k] for k in ('organization_id','store_id','machine_id')): raise ValueError()
    with Session(engine,expire_on_commit=False) as session:
        ctx = guard_execution(session,environment.machine_token,claim,step_id=sid,
            expected_run_version=payload['run_version'],expected_step_version=payload['step_version'],expected_runtime=repository.SDK_RUNTIME)
        if (ctx.person.subject_id,ctx.agent.subject_id) != (values['requester_id'],values['agent_id']): raise ValueError()
        binding = ctx.execution.frozen_context['pi_readonly_binding']
        if binding.get('category_source') != selected.pin(): raise ValueError()
        tool_registry.verify_pi_binding(session,ctx.person,ctx.agent,run_id=ctx.run.id,binding=binding,expected_digest=ctx.run.toolset_version)
        if (ctx.run.toolset_version != decision['toolset_version'] or name not in discovery.active_names(session,ctx)):
            raise ValueError()
        current = executor.pi_readonly_decision(session,ctx,name=name,version='1',native_call_id=decision['native_call_id'],arguments=decision['arguments'])
        if current != decision: raise ValueError()
        native = repository.require_pi_tool_receipt(session,claim,pi_name=name,decision=decision,state='dispatch_intent',tool_step_id=sid)
        definition = tool_registry.resolve_tool(decision['tool_key'],'1',store_id=claim.store_id)
        executor._check_tool_scope(session,ctx,decision,definition)
        operation, contract = receipts.native_contract(decision['tool_key'], decision['version'], name)
        prepared = receipts.prepare(session,actor=ctx.person,agent=ctx.agent,run_id=ctx.run.id,step_id=sid,
            operation=operation,arguments=decision['arguments'],source_binding=selected.binding())
        result = definition.output_model.model_validate(prepared.result()).model_dump(mode='json')
        tool_registry._audit(session,ctx.person,action=definition.audit_action,tool_key=definition.key,version='1',code='ok')
        now = repository._db_clock(session)
        step = repository.transition_step(session,organization_id=ctx.run.organization_id,store_id=ctx.run.store_id,run_id=ctx.run.id,
            step_id=sid,expected_run_version=ctx.run.version,expected_step_version=ctx.step.version,
            change=StepTransition(to_state=StepState.SUCCEEDED,output_ref='sha256:'+_digest(result)),now=now)
        native = repository.succeed_pi_tool_receipt(session,claim,pi_name=name,decision=decision,step=step,now=now)
        reference = receipts.stage(session,actor=ctx.person,agent=ctx.agent,claim=claim,step_id=sid,pi_receipt_id=native.id,
            prepared=prepared,source_binding=selected.binding())
        refs = [{'kind':'permission','key':definition.requester_permission},reference]
        executor._append_frozen_evidence(session,ctx,refs,supersede_memory=True)
        repository.settle_worker_attempt(session,claim,step_id=sid,outcome='continue',expected_runtime=repository.SDK_RUNTIME)
        executor._audit(session,ctx,'agent_tool.worker_invoked',definition.key)
        def final_pin(active,instant):
            _machine(active,environment,now=instant)
            environment.revalidate(); selected.check(engine)
            tool_registry.verify_pi_binding(active,ctx.person,ctx.agent,run_id=ctx.run.id,binding=binding,expected_digest=ctx.run.toolset_version,now=instant)
            discovery.active_names(active,ctx)
        commit_guard.enlist(session,action='pi_category_process_result',check=final_pin)
        response = dict(call_id=decision['native_call_id'],name=name,version='1',toolset_version=decision['toolset_version'],
            receipt_id=native.id,step_id=sid,output_ref=native.output_ref,result=result)
        actor = ctx.person
        session.commit()
    committed = _consume(engine,environment,selected,actor,reference)
    if committed is None or 'sha256:'+_digest(committed) != response['output_ref']: raise ValueError()
    response['result'] = committed
    return {'response':response,'refs':refs}


def run(raw):
    try:
        request = _decode(raw)
        with _loaded(request) as (engine,environment,selected):
            if request['mode'] == 'execute': result = _execute(engine,environment,selected,request['payload'])
            else:
                payload=request['payload']; actor=dict(payload['actor'])
                actor['auth_expires_at']=datetime.fromisoformat(actor['auth_expires_at'])
                cls=WorkPrincipal if payload['actor_kind']=='work' else AuthenticatedSubject
                result=_consume(engine,environment,selected,cls(**actor),payload['reference'])
                if result is None: raise ValueError()
            return dict(version=1,code='committed',request_sha256=_digest(request),result=result)
    except BaseException:
        return dict(version=1,code='unavailable',result=None)


def exchange(request):
    return exchange_committed(request, command=_COMMAND, cwd=_ROOT, nested=_IN_WORKER,
        nested_modes=('consume',), encode=receipts._json, decode_request=_decode,
        decode_reply=protected._decode, digest=_digest, timeout=_TIMEOUT,
        spawn=subprocess.Popen, communicate=_exchange, cleanup_outer=_cleanup)


def execute(selected,claim,step_id,run_version,step_version,decision,name):
    c=asdict(claim);c.pop('runtime_identity')
    request=dict(version=1,mode='execute',environment_sha256=selected.environment.sha256,pin=selected.pin(),
        payload=dict(claim=c,step_id=step_id,run_version=run_version,step_version=step_version,decision=decision,name=name))
    result=exchange(request)
    if type(result) is not dict or set(result) != {'response','refs'}: return None
    r=result['response']
    if (type(r) is not dict or set(r) != {'call_id','name','version','toolset_version','receipt_id','step_id','output_ref','result'}
            or (r['call_id'],r['name'],r['version'],r['toolset_version'],r['step_id']) !=
                (decision['native_call_id'],name,'1',decision['toolset_version'],step_id)
            or r['output_ref'] != 'sha256:'+_digest(r['result'])): return None
    operation, contract = receipts.native_contract(decision['tool_key'], decision['version'], name)
    value=r['result']
    if (value.get('category_receipt_id') != receipts._id(claim.run_id,step_id)
            or value.get('category_result',{}).get('arguments') != decision['arguments']
            or value['category_result'].get('operation') != operation
            or value['category_result'].get('contract') != contract): return None
    expected=[{'kind':'permission','key':'catalog.read'},dict(kind='guanjia_category',id=value['category_receipt_id'],sha256=_digest(value))]
    return result if result['refs'] == expected else None


def consume(selected,actor,reference):
    if type(actor) not in (WorkPrincipal,AuthenticatedSubject): return None
    a=asdict(actor);a['auth_expires_at']=actor.auth_expires_at.isoformat()
    result=exchange(dict(version=1,mode='consume',environment_sha256=selected.environment.sha256,pin=selected.pin(),
        payload=dict(actor=a,actor_kind='work' if type(actor) is WorkPrincipal else 'login',reference=reference)))
    if (type(result) is not dict or set(result) != {'category_receipt_id','category_result'}
            or result['category_receipt_id'] != reference.get('id')
            or _digest(result) != reference.get('sha256')):
        return None
    return result
