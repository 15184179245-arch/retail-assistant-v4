"""Fixed fresh-exec research worker. No network or business probe is implemented.

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
import time

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.agents import repository, tool_registry, answer_receipts
from backend.app.modules.agents import capability_discovery as discovery
from backend.app.modules.agents import source_environment
from backend.app.modules.assistant.principal import WorkPrincipal
from backend.app.modules.identity.machine_service import authenticate_machine_token, authorize_machine_action
from backend.app.workers import agent_executor as executor
from backend.app.workers.agent_guard import guard_execution
from . import research_runtime as runtime, research_host
from backend.app.modules.integrations.guanjia import category_receipts as receipts, live_loader as protected, sqlite_identity
from backend.app.modules.integrations.guanjia.work_source_process import _exchange, _cleanup

_ROOT = Path(__file__).resolve().parents[4]
_COMMAND = (sys.executable, '-I', '-c', 'import sys;sys.path.insert(0,'+repr(str(_ROOT))+');'
            'from backend.app.modules.agents.research_process import main;sys.exit(main())')
_TIMEOUT = 30.0
_IN_WORKER = False


def _digest(value):
    return receipts._digest(value)


def _decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 16384:
        raise ValueError()
    value = protected._decode(raw)
    protected._shape(value, ('version', 'mode', 'environment_sha256', 'pin', 'payload'))
    if type(value['version']) is not int or value['version'] != 1 or value['mode'] not in ('execute', 'consume', 'consume_batch'):
        raise ValueError()
    protected._digest(value['environment_sha256'])
    research_host.validate_pin(value['pin'])
    if value['mode'] == 'execute':
        p = protected._shape(value['payload'], ('claim','step_id','run_version','step_version','decision','name'))
        c = protected._shape(p['claim'], ('run_id','organization_id','store_id','epoch','machine_id'))
        for k,v in c.items():
            (protected._integer if k == 'epoch' else protected._text)(v)
        for k in ('run_version','step_version'): protected._integer(p[k])
        protected._text(p['step_id'])
        decision = protected._shape(p['decision'], ('tool_key','version','arguments','native_call_id','toolset_version'))
        operation = runtime.operation(decision['tool_key'], decision['version'], p['name'])
        runtime.INPUTS[operation].model_validate(decision['arguments'])
        protected._text(decision['native_call_id'])
        if not decision['toolset_version'].startswith('sha256:'): raise ValueError()
        protected._digest(decision['toolset_version'][7:])
    else:
        p = protected._shape(value['payload'], ('actor','actor_kind','references','references_sha256') if value['mode']=='consume_batch' else ('actor','actor_kind','reference'))
        cls = WorkPrincipal if p['actor_kind'] == 'work' else AuthenticatedSubject if p['actor_kind'] == 'login' else None
        if cls is None or type(p['actor']) is not dict or set(p['actor']) != set(cls.__dataclass_fields__): raise ValueError()
        actor = dict(p['actor']); actor['auth_expires_at'] = datetime.fromisoformat(actor['auth_expires_at'])
        if actor['auth_expires_at'].tzinfo is None: raise ValueError()
        cls(**actor)
        refs=p['references'] if value['mode']=='consume_batch' else [p['reference']]
        _references(refs)
        if value['mode']=='consume_batch' and p['references_sha256']!=_digest(refs):raise ValueError()
    return value


def _references(refs):
    if type(refs) is not list or not 1 <= len(refs) <= 32:raise ValueError()
    seen={}
    for r in refs:
        protected._shape(r, ('kind','id','sha256'))
        if type(r['kind']) is not str or r['kind']!='research':raise ValueError()
        protected._text(r['id']);protected._digest(r['sha256'])
        if r['id'] in seen and seen[r['id']]!=r['sha256']:raise ValueError()
        seen[r['id']]=r['sha256']


@contextmanager
def _loaded(request):
    global _IN_WORKER
    protected._process_gate()
    environment = source_environment.load(request['environment_sha256'],trial_name=request['pin'].get('trial_name'))
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
        selected = research_host.configure(engine,environment,request['pin']['directory_name'],request['pin']['manifest_sha256'],read=request['pin'].get('read'))
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


def _consume_batch(engine, environment, selected, actor, references):
    """Only prove the successful research call/package, NOT its old body/spans."""
    from sqlalchemy import select
    from .models import AgentRun, AgentExecution, AgentStep, AgentPiToolReceipt, AgentToolCall
    from backend.app.modules.identity.service import authorize_scope
    if (actor.subject_id,actor.organization_id,actor.store_id) != tuple(environment.values[k] for k in ('requester_id','organization_id','store_id')):
        raise ValueError()
    def query(model,**values):
        return select(model).where(model.organization_id==actor.organization_id,model.store_id==actor.store_id,
                                  *(getattr(model,k)==v for k,v in values.items()))
    # Two fresh snapshots detect a revoked identity after the first read. This
    # is not a claim to undo disclosure after an already authorized read.
    observations=[];proofs=[];common=[]
    for _ in range(2):
        round_observations=[];round_proofs=[];pending=[]
        with Session(engine,autoflush=False) as session, session.no_autoflush:
            connection=session.connection()
            if not connection.connection.driver_connection.in_transaction:connection.exec_driver_sql('BEGIN')
            from backend.app.modules.identity.models import ScopeSecurityFence
            epoch=session.scalar(select(ScopeSecurityFence.version).where(
                ScopeSecurityFence.organization_id==actor.organization_id,ScopeSecurityFence.store_id==actor.store_id))
            if type(epoch) is not int or epoch<=0:raise ValueError()
            _machine(session,environment);selected.check(engine)
            for reference in references:
                native=session.scalar(query(AgentPiToolReceipt,id=reference['id']))
                if native is None:raise ValueError()
                run=session.scalar(query(AgentRun,id=native.run_id))
                execution=session.scalar(query(AgentExecution,run_id=native.run_id))
                step=session.scalar(query(AgentStep,run_id=native.run_id,id=native.tool_step_id))
                model=session.scalar(query(AgentStep,run_id=native.run_id,id=native.model_step_id))
                call=session.scalar(query(AgentToolCall,run_id=native.run_id,step_id=native.tool_step_id))
                if any(v is None for v in (run,execution,step,model,call)):raise ValueError()
                if type(actor) is WorkPrincipal and actor.run_id!=run.id:raise ValueError()
                agent=AuthenticatedSubject(environment.values['agent_id'],actor.organization_id,actor.store_id,actor.auth_expires_at)
                tool_registry._person_scope(session,actor)
                tool_registry._agent_scope(session,actor,agent,'agents.knowledge.search')
                authorize_scope(session,actor,organization_id=actor.organization_id,store_id=actor.store_id,permission='knowledge.read')
                binding=execution.frozen_context.get('pi_readonly_binding',{})
                if (repository.runtime_identity(execution.frozen_context)!=repository.SDK_RUNTIME
                    or binding.get('research_source')!=selected.pin()
                    or binding.get('profile')!=tool_registry.PI_SOURCE_PROFILE
                    or (binding.get('run_id'),binding.get('requester_id'),binding.get('agent_id'),binding.get('organization_id'),binding.get('store_id'))
                        !=(run.id,actor.subject_id,agent.subject_id,actor.organization_id,actor.store_id)
                    or run.requester_id!=actor.subject_id or tool_registry.pi_binding_digest(binding)!=run.toolset_version
                    or tool_registry.pi_pair(binding,native.pi_name,native.tool_version)!=(native.tool_key,'1')):raise ValueError()
                operation=runtime.operation(native.tool_key,native.tool_version,native.pi_name)
                output='sha256:'+reference['sha256']
                if (native.contract_version!=1 or native.model_kind!='model' or native.tool_kind!='tool' or native.error_code is not None
                    or native.toolset_version!=run.toolset_version or (native.state,native.output_ref)!=('succeeded',output)
                    or (step.kind,step.state,step.output_ref)!=('tool','succeeded',output)
                    or (model.kind,model.state)!=('model','succeeded') or model.sequence_no>=step.sequence_no
                    or (call.tool_key,call.tool_version,call.input_digest)!=(native.tool_key,'1',native.input_digest)
                    or call.idempotency_key!='pi:'+_digest([run.id,native.native_call_id])):raise ValueError()
                round_proofs.append(_digest(dict(run_id=run.id,run_version=run.version,run_state=run.state,
                    execution_epoch=execution.epoch,execution_version=execution.version,
                    toolset_version=run.toolset_version,research_pin=binding['research_source'],
                    native_id=native.id,native_input=native.input_digest,native_output=native.output_ref,
                    native_updated_at=native.updated_at.isoformat(),native_call_id=native.native_call_id,
                    model_id=model.id,model_sequence=model.sequence_no,step_id=step.id,step_sequence=step.sequence_no,
                    call_id=call.id,call_input=call.input_digest,call_key=call.idempotency_key)))
                observed=native.updated_at.isoformat()
                metadata_args=None
                if operation=='probe':
                    operation_id,context_digest=_probe_identity(environment,selected,run,native)
                    metadata_args=dict(operation_id=operation_id,context_digest=context_digest,
                        input_digest=native.input_digest,output_digest=reference['sha256'])
                observation=dict(kind='research',object_id=native.id,version_ref=reference['sha256'],
                    version=reference['sha256'],label=('已提交只读探测（历史时点、有界部分，非实时全量）' if operation=='probe' else '研究调用及资料包来源（仅供参考，不含原结果回放）'),
                    source_ref='research-corpus:'+selected.manifest_sha256,observed_at=observed)
                pending.append((observation,metadata_args,agent))
            selected.check(engine);_machine(session,environment)
            identity=asdict(actor);identity['auth_expires_at']=actor.auth_expires_at.isoformat()
            common.append(_digest(dict(actor=identity,environment=environment.sha256,
                pin=selected.pin(),fence=epoch)))
        for observation,metadata_args,agent in pending:
            if metadata_args is not None:
                from . import research_broker_host
                with Session(engine,expire_on_commit=False) as clean:
                    meta=research_broker_host.read_committed_metadata(selected,caller=clean,
                        person=actor,agent=agent,**metadata_args)
                observation['observed_at']=meta['observed_at']
            round_observations.append(observation)
        observations.append(round_observations);proofs.append(round_proofs)
    if observations[0]!=observations[1] or proofs[0]!=proofs[1] or common[0]!=common[1]:raise ValueError()
    return dict(citations=observations[0],common_sha256=common[0],references_sha256=_digest(references))


def _consume(engine,environment,selected,actor,reference):
    return _consume_batch(engine,environment,selected,actor,[reference])['citations'][0]


def _guard(session,environment,selected,payload):
    claim=repository.AgentClaim(**payload['claim'],runtime_identity=repository.SDK_RUNTIME)
    decision=payload['decision'];name=payload['name'];sid=payload['step_id']
    values=environment.values
    if (claim.organization_id,claim.store_id,claim.machine_id)!=tuple(values[k] for k in ('organization_id','store_id','machine_id')):raise ValueError()
    ctx=guard_execution(session,environment.machine_token,claim,step_id=sid,
        expected_run_version=payload['run_version'],expected_step_version=payload['step_version'],expected_runtime=repository.SDK_RUNTIME)
    if (ctx.person.subject_id,ctx.agent.subject_id)!=(values['requester_id'],values['agent_id']):raise ValueError()
    binding=ctx.execution.frozen_context['pi_readonly_binding']
    if binding.get('research_source')!=selected.pin():raise ValueError()
    selected.check(session.get_bind());_machine(session,environment)
    tool_registry.verify_pi_binding(session,ctx.person,ctx.agent,run_id=ctx.run.id,binding=binding,expected_digest=ctx.run.toolset_version)
    if ctx.run.toolset_version!=decision['toolset_version'] or name not in discovery.active_names(session,ctx):raise ValueError()
    current=executor.pi_readonly_decision(session,ctx,name=name,version='1',native_call_id=decision['native_call_id'],arguments=decision['arguments'])
    if current!=decision:raise ValueError()
    native=repository.require_pi_tool_receipt(session,claim,pi_name=name,decision=decision,state='dispatch_intent',tool_step_id=sid)
    definition=tool_registry.resolve_tool(decision['tool_key'],'1',store_id=claim.store_id)
    executor._check_tool_scope(session,ctx,decision,definition)
    return claim,ctx,binding,definition,native


def _execute(engine, environment, selected, payload):
    from backend.app.application import commit_guard
    from .schemas import StepState,StepTransition
    decision=payload['decision'];name=payload['name'];sid=payload['step_id']
    op=runtime.operation(decision['tool_key'],decision['version'],name)
    with Session(engine,expire_on_commit=False) as before:
        claim,ctx,_,_,native=_guard(before,environment,selected,payload)
        person,agent=ctx.person,ctx.agent
        operation_id,context_digest=_probe_identity(environment,selected,ctx.run,native)
        input_digest=native.input_digest
        # Close/rollback the guard's locks BEFORE G1 opens its fresh Sessions.
    with Session(engine,expire_on_commit=False) as caller:
        if caller.in_transaction():raise ValueError()
        if op=='probe':
            from . import research_broker_host
            def gate(phase,page):
                with Session(engine,expire_on_commit=False) as guard:
                    _,current,_,_,n=_guard(guard,environment,selected,payload)
                    if _probe_identity(environment,selected,current.run,n)!=(operation_id,context_digest):raise ValueError()
                    return context_digest
            result=research_broker_host.execute(caller,person,agent,selected,decision['arguments'],operation_id=operation_id,context_digest=context_digest,input_digest=input_digest,host_gate=gate)
        else:
            result=runtime.invoke(caller,person,agent,selected.config(),op,decision['arguments'])
            if op=='describe' and 'read' in selected.pin():
                from .research_broker_host import admitted_contracts
                result['data']['approved_read_contracts']=admitted_contracts(selected,decision['arguments']['candidate_id'])
    with Session(engine,expire_on_commit=False) as session:
        claim,ctx,binding,definition,native=_guard(session,environment,selected,payload)
        result=definition.output_model.model_validate(result).model_dump(mode='json')
        now=repository._db_clock(session)
        step=repository.transition_step(session,organization_id=ctx.run.organization_id,store_id=ctx.run.store_id,run_id=ctx.run.id,
            step_id=sid,expected_run_version=ctx.run.version,expected_step_version=ctx.step.version,
            change=StepTransition(to_state=StepState.SUCCEEDED,output_ref='sha256:'+_digest(result)),now=now)
        native=repository.succeed_pi_tool_receipt(session,claim,pi_name=name,decision=decision,step=step,now=now)
        reference=dict(kind='research',id=native.id,sha256=_digest(result))
        refs=[dict(kind='permission',key='knowledge.read'),reference]
        from . import public_trace_capture
        public_trace_capture.capture(session,ctx,claim,native,decision,phase='result',result=result)
        executor._append_frozen_evidence(session,ctx,refs,supersede_memory=True)
        repository.settle_worker_attempt(session,claim,step_id=sid,outcome='continue',expected_runtime=repository.SDK_RUNTIME)
        tool_registry._audit(session,ctx.person,action=definition.audit_action,tool_key=definition.key,version='1',code='ok')
        executor._audit(session,ctx,'agent_tool.worker_invoked',definition.key)
        def final_pin(active,instant):
            _machine(active,environment,now=instant);selected.check(engine)
            tool_registry.verify_pi_binding(active,ctx.person,ctx.agent,run_id=ctx.run.id,binding=binding,expected_digest=ctx.run.toolset_version,now=instant)
            discovery.active_names(active,ctx)
        commit_guard.enlist(session,action='pi_research_process_result',check=final_pin)
        response=dict(call_id=decision['native_call_id'],name=name,version='1',toolset_version=decision['toolset_version'],
            receipt_id=native.id,step_id=sid,output_ref=native.output_ref,result=result)
        session.commit()
    if _consume(engine,environment,selected,person,reference) is None:raise ValueError()
    return dict(response=response,refs=refs)


def run(raw):
    try:
        request = _decode(raw)
        with _loaded(request) as (engine,environment,selected):
            if request['mode'] == 'execute': result = _execute(engine,environment,selected,request['payload'])
            else:
                payload=request['payload']; actor=dict(payload['actor'])
                actor['auth_expires_at']=datetime.fromisoformat(actor['auth_expires_at'])
                cls=WorkPrincipal if payload['actor_kind']=='work' else AuthenticatedSubject
                result=(_consume_batch(engine,environment,selected,cls(**actor),payload['references']) if request['mode']=='consume_batch'
                        else _consume(engine,environment,selected,cls(**actor),payload['reference']))
                if result is None: raise ValueError()
            return dict(version=1,code='committed',request_sha256=_digest(request),result=result)
    except BaseException:
        return dict(version=1,code='unavailable',result=None)


def _cleanup_nested(process):
    # A consume child never starts another operation. It shares the outer
    # worker group so outer cancellation kills both, but its own timeout must
    # kill only this PID, never its parent or the whole application group.
    try:
        process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=5)
    for stream in (process.stdin,process.stdout):
        if stream is not None:stream.close()


def exchange(request, *, deadline=None):
    process=None
    nested=_IN_WORKER
    try:
        if nested and request.get('mode') not in ('consume','consume_batch'):raise ValueError()
        raw=receipts._json(request).encode()
        if _decode(raw) != request: raise ValueError()
        is_probe=request.get('mode')=='execute' and request.get('payload',{}).get('decision',{}).get('tool_key')=='research.probe'
        ceiling=time.monotonic()+(50.0 if is_probe else _TIMEOUT)
        deadline=ceiling if deadline is None else min(deadline,ceiling)
        if time.monotonic()>=deadline:raise ValueError()
        process=subprocess.Popen(_COMMAND,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            start_new_session=not nested,close_fds=True,cwd=_ROOT,env={'PATH':os.defpath,'LANG':'C.UTF-8'})
        output,status=_exchange(process,raw,deadline)
        value=protected._decode(output)
        if (status != 0 or type(value) is not dict or set(value) != {'version','code','request_sha256','result'}
                or type(value['version']) is not int or value['version'] != 1 or value['code'] != 'committed'
                or value['request_sha256'] != _digest(request)): raise ValueError()
        return value['result']
    except BaseException:
        return None
    finally:
        if process is not None:
            try:
                (_cleanup_nested if nested else _cleanup)(process)
            except BaseException:
                return None


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
    op=runtime.operation(decision['tool_key'],decision['version'],name)
    model=runtime.ProbeOutput if op=='probe' else runtime.Output
    value=model.model_validate(r['result']).model_dump(mode='json')
    if value['operation']!=op or value['corpus_digest']!=selected.manifest_sha256:return None
    expected=[dict(kind='permission',key='knowledge.read'),dict(kind='research',id=r['receipt_id'],sha256=_digest(value))]
    return result if result['refs']==expected else None


def consume(selected,actor,reference):
    if type(actor) not in (WorkPrincipal,AuthenticatedSubject): return None
    a=asdict(actor);a['auth_expires_at']=actor.auth_expires_at.isoformat()
    result=exchange(dict(version=1,mode='consume',environment_sha256=selected.environment.sha256,pin=selected.pin(),
        payload=dict(actor=a,actor_kind='work' if type(actor) is WorkPrincipal else 'login',reference=reference)))
    if (type(result) is not dict or set(result)!={'kind','object_id','version_ref','version','label','source_ref','observed_at'}
        or (result['kind'],result['object_id'],result['version_ref'],result['version'])!=('research',reference.get('id'),reference.get('sha256'),reference.get('sha256'))
        or result['source_ref']!='research-corpus:'+selected.manifest_sha256):return None
    return result


def consume_batch(selected,actor,references,*,deadline=None):
    if type(actor) not in (WorkPrincipal,AuthenticatedSubject):return None
    try:
        if type(references) is not list or not 1<=len(references)<=answer_receipts.MAX_EVIDENCE:raise ValueError()
        a=asdict(actor);a['auth_expires_at']=actor.auth_expires_at.isoformat()
        def request(refs):
            return dict(version=1,mode='consume_batch',environment_sha256=selected.environment.sha256,pin=selected.pin(),
                payload=dict(actor=a,actor_kind='work' if type(actor) is WorkPrincipal else 'login',
                    references=refs,references_sha256=_digest(refs)))
        # Preflight EVERY envelope before starting any child; byte cap includes pins and actor.
        seen={};chunks=[];chunk=[]
        for ref in references:
            _references([ref])
            if ref['id'] in seen and seen[ref['id']]!=ref['sha256']:raise ValueError()
            seen[ref['id']]=ref['sha256']
            candidate=chunk+[ref]
            if len(candidate)>32 or len(receipts._json(request(candidate)).encode())>16384:
                if not chunk:raise ValueError()
                chunks.append(request(chunk));chunk=[ref]
            else:chunk=candidate
            _decode(receipts._json(request(chunk)).encode())
        chunks.append(request(chunk))
        ceiling=time.monotonic()+_TIMEOUT
        deadline=ceiling if deadline is None else min(deadline,ceiling)
        citations=[];common=None
        for req in chunks:
            result=exchange(req,deadline=deadline)
            refs=req['payload']['references']
            if (type(result) is not dict or set(result)!={'citations','common_sha256','references_sha256'}
                or result['references_sha256']!=_digest(refs) or type(result['citations']) is not list
                or len(result['citations'])!=len(refs)):raise ValueError()
            protected._digest(result['common_sha256'])
            if common is not None and common!=result['common_sha256']:raise ValueError()
            common=result['common_sha256']
            for ref,citation in zip(refs,result['citations']):
                if (type(citation) is not dict or set(citation)!={'kind','object_id','version_ref','version','label','source_ref','observed_at'}
                    or (citation['kind'],citation['object_id'],citation['version_ref'],citation['version'])!=('research',ref['id'],ref['sha256'],ref['sha256'])
                    or citation['source_ref']!='research-corpus:'+selected.manifest_sha256):raise ValueError()
                citations.append(citation)
        if time.monotonic()>=deadline:raise ValueError()
        return citations
    except Exception:return None


def main():
    import json
    value=run(sys.stdin.buffer.read(16385)) if len(sys.argv)==1 else dict(version=1,code='unavailable',result=None)
    print(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    return 0 if value['code']=='committed' else 6




def _probe_identity(environment,selected,run,native):
    operation=_digest(['v4.research.probe/1',run.organization_id,run.store_id,run.id,native.native_call_id])
    context=_digest(dict(version=1,organization_id=run.organization_id,store_id=run.store_id,run_id=run.id,
        native_id=native.id,native_call_id=native.native_call_id,input_digest=native.input_digest,
        toolset_version=run.toolset_version,environment_sha256=environment.sha256,research_source=selected.pin()))
    return operation,context

if __name__=='__main__':
    raise SystemExit(main())
