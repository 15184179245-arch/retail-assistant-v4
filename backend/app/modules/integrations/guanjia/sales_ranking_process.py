"""Fixed fresh-exec sales candidate, with no model-controlled routing.

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
from . import sales_ranking_receipts as receipts, sales_ranking_source, sales_ranking_host, live_loader as protected, sqlite_identity
from .work_source_process import _exchange, _cleanup

_ROOT = Path(__file__).resolve().parents[5]
_COMMAND = (sys.executable, '-I', '-c', 'import sys,runpy;sys.path.insert(0,'+repr(str(_ROOT))+');'
            'runpy.run_module("backend.app.workers.guanjia_sales_ranking",run_name="__main__")')
_TIMEOUT = 120.0
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
    sales_ranking_source.validate_pin(value['pin'])
    if value['mode'] == 'execute':
        p = protected._shape(value['payload'], ('claim','step_id','run_version','step_version','decision','name'))
        c = protected._shape(p['claim'], ('run_id','organization_id','store_id','epoch','machine_id'))
        for k,v in c.items():
            (protected._integer if k == 'epoch' else protected._text)(v)
        for k in ('run_version','step_version'): protected._integer(p[k])
        protected._text(p['step_id'])
        decision = protected._shape(p['decision'], ('tool_key','version','arguments','native_call_id','toolset_version'))
        runtime=receipts.contract(decision['version'])
        if (decision['tool_key'],p['name']) != (runtime.KEY,runtime.NAME):raise ValueError()
        runtime.Input.model_validate(decision['arguments'])
        if decision['version']=='3':
            if value['pin'].get('version')!=3:raise ValueError()
        elif decision['arguments']!={} or value['pin'].get('version')!=1:raise ValueError()
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
        if r['kind'] != receipts.KIND: raise ValueError()
        protected._text(r['id']); protected._digest(r['sha256'])
    return value


@contextmanager
def _loaded(request):
    global _IN_WORKER
    protected._process_gate()
    environment = source_environment.load(request['environment_sha256'])
    files = engine = selected = None
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
        if request['pin'].get('version')==3:
            selected=sales_ranking_source.select_local_source(environment,
                descriptor_sha256=request['pin']['descriptor_sha256'])
        else:
            material = protected._decode(files.read('material.json'))
            selected = sales_ranking_source.select_source(environment,material_sha256=request['pin']['material_sha256'],expected_tenant_id=request['pin']['tenant_id'],tenancy_name=material['tenancy_name'])
        if selected.pin_dict() != request['pin']:raise ValueError()
        os.environ['AGENT_SUBJECT_ID'] = environment.values['agent_id']
        _IN_WORKER = True
        yield engine,environment,selected
    finally:
        _IN_WORKER = previous_worker
        if engine is not None:
            engine.dispose()
        if selected is not None:selected.close()
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
    from backend.app.core.security import AuthenticatedSubject
    values=environment.values
    if (actor.subject_id,actor.organization_id,actor.store_id)!=tuple(values[k] for k in ('requester_id','organization_id','store_id')):raise ValueError()
    with Session(engine,autoflush=False) as session:
        connection=session.connection()
        if not connection.connection.driver_connection.in_transaction:connection.exec_driver_sql('BEGIN')
        _machine(session,environment);environment.revalidate();selected.revalidate()
        payload=receipts.read(selected,reference)
        agent=AuthenticatedSubject(values['agent_id'],actor.organization_id,actor.store_id,actor.auth_expires_at)
        result=receipts.facts(session,actor,agent,payload,reference,selected=selected)
        _machine(session,environment);environment.revalidate();selected.revalidate()
        receipts._authorize(session,actor,agent)
    if selected.pin_dict().get('version')==3:
        # File recomputation can take time. End its read snapshot, then check
        # the exact same committed facts in a NEW read root. Do not acquire a
        # second writer fence: the caller may already own the command fence
        # while validating evidence for the next model/final commit.
        from . import local_sales_receipts
        current=receipts.read(selected,reference)
        if current!=payload:raise ValueError()
        with Session(engine,autoflush=False) as session:
            connection=session.connection()
            if not connection.connection.driver_connection.in_transaction:connection.exec_driver_sql('BEGIN')
            _machine(session,environment)
            local_sales_receipts._check_facts(session,actor,agent,current,reference,selected)
    return result


def _guard(session,engine,environment,selected,payload):
    claim=repository.AgentClaim(**payload['claim'],runtime_identity=repository.SDK_RUNTIME)
    ctx=guard_execution(session,environment.machine_token,claim,step_id=payload['step_id'],
        expected_run_version=payload['run_version'],expected_step_version=payload['step_version'],expected_runtime=repository.SDK_RUNTIME)
    values=environment.values;decision=payload['decision']
    runtime=receipts.contract(decision['version']); version=decision['version']
    if (type(ctx.person) is not WorkPrincipal or ctx.person.run_id!=claim.run_id
            or (ctx.person.subject_id,ctx.agent.subject_id)!=(values['requester_id'],values['agent_id'])
            or (claim.organization_id,claim.store_id,claim.machine_id)!=tuple(values[k] for k in ('organization_id','store_id','machine_id'))
            or ctx.run.state!='running' or ctx.execution.dispatch_state!='dispatched'
            or (ctx.step.kind,ctx.step.state)!=('tool','running')):raise ValueError()
    _machine(session,environment);environment.revalidate();selected.revalidate()
    binding=ctx.execution.frozen_context['pi_readonly_binding']
    if binding.get('sales_source')!=selected.pin_dict() or ctx.run.toolset_version!=decision['toolset_version']:raise ValueError()
    tool_registry.verify_pi_binding(session,ctx.person,ctx.agent,run_id=ctx.run.id,binding=binding,expected_digest=ctx.run.toolset_version)
    if binding.get('sales_contract','1') != version: raise ValueError()
    if runtime.NAME not in discovery.active_names(session,ctx):raise ValueError()
    current=executor.pi_readonly_decision(session,ctx,name=runtime.NAME,version=version,native_call_id=decision['native_call_id'],arguments=decision['arguments'])
    if current!=decision:raise ValueError()
    native=repository.require_pi_tool_receipt(session,claim,pi_name=runtime.NAME,decision=decision,state='dispatch_intent',tool_step_id=payload['step_id'])
    definition=tool_registry.resolve_tool(runtime.KEY,version,store_id=claim.store_id)
    executor._check_tool_scope(session,ctx,decision,definition)
    receipts._authorize(session,ctx.person,ctx.agent)
    return claim,ctx,native,definition


def _execute(engine, environment, selected, payload):
    from datetime import timezone
    from backend.app.application import commit_guard
    from backend.app.modules.agents.schemas import StepState,StepTransition
    def check():
        # Own fresh guard transaction is closed before entering network.
        with Session(engine,autoflush=False,expire_on_commit=False) as session:
            _guard(session,engine,environment,selected,payload)
        return True
    check()
    version=payload['decision']['version']; runtime=receipts.contract(version)
    prepared=None
    if version=='3':
        prepared=selected.evaluate(payload['decision']['arguments'],authority_check=check)
        result=prepared.result
    else:
        # Fixed private journal path; model and IPC cannot select a directory.
        rootfd=receipts._root_fd(environment.root)
        try:
            try:os.mkdir('sales-ranking-journal',mode=0o700,dir_fd=rootfd);os.fsync(rootfd)
            except FileExistsError:pass
        finally:os.close(rootfd)
        operation=receipts._id(payload['claim']['run_id'],payload['step_id'])
        receipt=sales_ranking_host.collect_report(selected.host_material(),operation_id=operation,
            as_of=datetime.now(timezone.utc).replace(microsecond=0),authority_check=check)
        result=runtime.project_receipt(receipt)
    with Session(engine,expire_on_commit=False) as session:
        claim,ctx,native,definition=_guard(session,engine,environment,selected,payload)
        def in_result_root():
            # The exact result root already owns the security fence. Opening
            # another authorizing transaction here deadlocks its SQLite fence;
            # recheck the full original guard in this same root instead.
            _guard(session,engine,environment,selected,payload)
            return True
        reference=receipts.persist(selected,run_id=ctx.run.id,step_id=ctx.step.id,native_id=native.id,result=result,
            **(dict(arguments=payload['decision']['arguments'],snapshot=prepared.snapshot,authority_check=in_result_root)
               if prepared is not None else {}))
        result=definition.output_model.model_validate(result).model_dump(mode='json')
        now=repository._db_clock(session)
        step=repository.transition_step(session,organization_id=ctx.run.organization_id,store_id=ctx.run.store_id,run_id=ctx.run.id,
            step_id=ctx.step.id,expected_run_version=ctx.run.version,expected_step_version=ctx.step.version,
            change=StepTransition(to_state=StepState.SUCCEEDED,output_ref='sha256:'+_digest(result)),now=now)
        native=repository.succeed_pi_tool_receipt(session,claim,pi_name=runtime.NAME,decision=payload['decision'],step=step,now=now)
        refs=[{'kind':'permission','key':'guanjia.sales.read'},{'kind':'permission','key':'guanjia.company.read'},reference]
        executor._append_frozen_evidence(session,ctx,refs,supersede_memory=True)
        executor._audit(session,ctx,'agent_tool.worker_invoked',definition.key)
        tool_registry._audit(session,ctx.person,action=definition.audit_action,tool_key=definition.key,version=version,code='ok')
        def final(active,instant):
            _machine(active,environment,now=instant);environment.revalidate();selected.revalidate()
            tool_registry.verify_pi_binding(active,ctx.person,ctx.agent,run_id=ctx.run.id,
                binding=ctx.execution.frozen_context['pi_readonly_binding'],expected_digest=ctx.run.toolset_version,now=instant)
            receipts._authorize(active,ctx.person,ctx.agent,now=instant)
            from .observation_receipts import _check_claim_at_commit
            _check_claim_at_commit(active,claim,instant)
            receipts.facts(active,ctx.person,ctx.agent,receipts.read(selected,reference),reference,selected=selected)
        commit_guard.enlist(session,action='pi_sales_ranking_result',check=final)
        repository.settle_worker_attempt(session,claim,step_id=ctx.step.id,outcome='continue',expected_runtime=repository.SDK_RUNTIME)
        response=dict(call_id=payload['decision']['native_call_id'],name=runtime.NAME,version=version,toolset_version=payload['decision']['toolset_version'],
            receipt_id=native.id,step_id=ctx.step.id,output_ref=native.output_ref,result=result)
        actor=ctx.person
        session.commit()
    # Separate connection/root after commit; no network path in consume.
    committed=_consume(engine,environment,selected,actor,reference)
    if 'sha256:'+_digest(committed)!=response['output_ref']:raise ValueError()
    response['result']=committed
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
    request=dict(version=1,mode='execute',environment_sha256=selected.pin_dict()['environment_sha256'],pin=selected.pin_dict(),
        payload=dict(claim=c,step_id=step_id,run_version=run_version,step_version=step_version,decision=decision,name=name))
    result=exchange(request)
    if type(result) is not dict or set(result)!={'response','refs'}:return None
    runtime=receipts.contract(decision['version']); version=decision['version']
    r=result['response']
    if (type(r) is not dict or set(r)!={'call_id','name','version','toolset_version','receipt_id','step_id','output_ref','result'}
            or (r['call_id'],r['name'],r['version'],r['toolset_version'],r['step_id'])!=(decision['native_call_id'],runtime.NAME,version,decision['toolset_version'],step_id)
            or r['output_ref']!='sha256:'+_digest(r['result'])):return None
    try:runtime.Output.model_validate(r['result'])
    except Exception:return None
    refs=result['refs']
    if type(refs) is not list or len(refs)!=3:return None
    ref=refs[-1]
    if type(ref) is not dict or set(ref)!={'kind','id','sha256'} or (ref['kind'],ref['id'])!=(receipts.KIND,receipts._id(claim.run_id,step_id)):return None
    try:protected._digest(ref['sha256'])
    except Exception:return None
    expected=[{'kind':'permission','key':'guanjia.sales.read'},{'kind':'permission','key':'guanjia.company.read'},ref]
    return result if refs==expected else None


def consume(selected,actor,reference):
    if type(actor) not in (WorkPrincipal,AuthenticatedSubject):return None
    a=asdict(actor);a['auth_expires_at']=actor.auth_expires_at.isoformat()
    result=exchange(dict(version=1,mode='consume',environment_sha256=selected.pin_dict()['environment_sha256'],pin=selected.pin_dict(),
        payload=dict(actor=a,actor_kind='work' if type(actor) is WorkPrincipal else 'login',reference=reference)))
    try:
        if type(result) is not dict or type(result.get('version')) is not int: raise ValueError()
        result=receipts.contract(str(result['version'])).Output.model_validate(result).model_dump(mode='json')
        return result
    except Exception:return None
