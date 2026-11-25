"""Private immutable sales results; native/Step commit is disclosure authority.

Files alone confer no authority. Every read binds the original successful
native receipt and replays strict parsing of both retained complete reads.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from backend.app.modules.agents import repository, tool_registry
from backend.app.modules.agents.models import AgentRun, AgentExecution, AgentStep, AgentToolCall, AgentPiToolReceipt
from backend.app.modules.agents.sales_ranking_runtime import KEY, NAME, Output, project_receipt
from backend.app.modules.assistant.principal import WorkPrincipal
from backend.app.modules.identity.service import authorize_scope
from .live_call_ledger import _root_fd
from .sales_ranking_contract import parse_page, assemble_candidate
from .sales_ranking_host import ReportCandidateReceipt, _projection
from . import live_loader as protected

KIND = 'guanjia_sales_ranking'


def contract(version):
    # Explicit whitelist; never route a module/name supplied by the model.
    from backend.app.modules.agents import sales_ranking_runtime, sales_product_runtime, local_sales_runtime
    if type(version) is not str or version not in ('1', '2', '3'): raise ValueError()
    return {'1':sales_ranking_runtime,'2':sales_product_runtime,'3':local_sales_runtime}[version]



def _json(value):
    raw=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if len(raw.encode('utf-8'))>131072:raise ValueError()
    return raw


def _digest(value):return sha256(_json(value).encode()).hexdigest()


def _id(run_id,step_id):return str(uuid5(NAMESPACE_URL,'retail/sales-ranking/'+_json([run_id,step_id])))


def _authorize(session,actor,agent,*,now=None):
    tool_registry._person_scope(session,actor,now=now)
    tool_registry._agent_scope(session,actor,agent,'agents.guanjia.sales.read',now=now)
    for permission in ('agents.run','guanjia.sales.read','guanjia.company.read'):
        authorize_scope(session,actor,organization_id=actor.organization_id,store_id=actor.store_id,permission=permission,now=now)


def _identity(info):
    # Reading may update atime (including relatime); it is not integrity state.
    return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,
            info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _read(directory,name,maximum=2_000_000):
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid()
                or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1 or not 0<before.st_size<=maximum):raise ValueError()
        raw=os.pread(fd,maximum+1,0)
        verified=os.pread(fd,maximum+1,0)
        after=os.fstat(fd)
        named=os.stat(name,dir_fd=directory,follow_symlinks=False)
        if (_identity(before)!=_identity(after) or _identity(after)!=_identity(named)
                or len(raw)!=before.st_size or sha256(raw).digest()!=sha256(verified).digest()):raise ValueError()
        return raw
    finally:os.close(fd)


def _write(directory,name,raw):
    fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
    try:
        pending=memoryview(raw)
        while pending:
            count=os.write(fd,pending)
            if count<=0:raise ValueError()
            pending=pending[count:]
        os.fsync(fd)
    finally:os.close(fd)
    os.fsync(directory)
    if _read(directory,name,131072)!=raw:raise ValueError()


def _journal(root,operation_id,version='1'):
    """Find exactly one immutable acquisition and verify every retained byte."""
    fd=_root_fd(root)
    try:
        matches=[]
        for name in os.listdir(fd):
            if len(name)!=64 or any(c not in '0123456789abcdef' for c in name):continue
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            try:
                info=os.fstat(child)
                if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:raise ValueError()
                initial=protected._decode(_read(child,'000000.json'))
                if initial.get('operation_id')!=operation_id:continue
                events=[];manifest={}
                for item in sorted(os.listdir(child)):
                    raw=_read(child,item)
                    manifest[item]=sha256(raw).hexdigest()
                    if item.endswith('.json'):events.append((item,protected._decode(raw)))
                pages=[[],[]];as_of=initial['capture_utc'];store=initial['binding']['store_id']
                for event_name,event in events:
                    if event.get('state')=='returned' and event.get('raw_retained') is True:
                        rawname=event_name[:-5]+'.raw'
                        raw=_read(child,rawname)
                        if sha256(raw).hexdigest()!=event['sha256'] or len(raw)!=event['size']:raise ValueError()
                        p,kind,offset=event['request'].split(':')
                        if p not in ('0','1') or kind!='page':raise ValueError()
                        pages[int(p)].append(parse_page(raw,expected_store=store,skip=int(offset),take=200))
                candidates=[assemble_candidate(tuple(p)) for p in pages]
                first,second=map(_projection,candidates)
                comparison=events[-1][1]
                if comparison.get('state')!='comparison' or comparison.get('matches') is not True or first!=second or (comparison['first_sha256'],comparison['second_sha256'])!=(first,second):raise ValueError()
                output=contract(version).project_receipt(ReportCandidateReceipt(candidates[0],first,second,True,as_of))
                matches.append((name,manifest,output))
            finally:os.close(child)
        if len(matches)!=1:raise ValueError()
        return matches[0]
    finally:os.close(fd)


def persist(selected,*,run_id,step_id,native_id,result,arguments=None,snapshot=None,authority_check=None):
    """Before commit only; failure/orphan files must never authorize disclosure."""
    selected.revalidate()
    if type(result) is dict and type(result.get('version')) is int and result['version']==3:
        from . import local_sales_receipts
        return local_sales_receipts.persist(selected,run_id=run_id,step_id=step_id,native_id=native_id,
            arguments=arguments,snapshot=snapshot,result=result,authority_check=authority_check)
    operation=_id(run_id,step_id)
    if type(result) is not dict or type(result.get('version')) is not int: raise ValueError()
    version=str(result['version']); runtime=contract(version)
    name,manifest,recomputed=_journal(selected.host_material().journal_root,operation,version)
    result=runtime.Output.model_validate(result).model_dump(mode='json')
    if result!=recomputed:raise ValueError()
    payload=dict(version=int(version),run_id=run_id,step_id=step_id,native_id=native_id,pin=selected.pin_dict(),
        operation_id=operation,journal=name,manifest=manifest,result=result)
    root=selected._environment.root
    fd=_root_fd(root)
    try:_write(fd,'sales-result-'+operation+'.json',_json(payload).encode())
    finally:os.close(fd)
    return dict(kind=KIND,id=operation,sha256=_digest(payload))


def read(selected,reference):
    if selected.pin_dict().get('version')==3:
        from . import local_sales_receipts
        return local_sales_receipts.read(selected,reference)
    if type(reference) is not dict or set(reference)!= {'kind','id','sha256'} or reference['kind']!=KIND:raise ValueError()
    # No path syntax from references; UUID is rederived after reading.
    from uuid import UUID
    if str(UUID(reference['id']))!=reference['id']:raise ValueError()
    selected.revalidate()
    fd=_root_fd(selected._environment.root)
    try:payload=protected._decode(_read(fd,'sales-result-'+reference['id']+'.json',131072))
    finally:os.close(fd)
    if (type(payload) is not dict or set(payload)!={'version','run_id','step_id','native_id','pin','operation_id','journal','manifest','result'}
            or type(payload['version']) is not int or payload['version'] not in (1,2) or payload['pin']!=selected.pin_dict()
            or _id(payload['run_id'],payload['step_id'])!=reference['id'] or payload['operation_id']!=reference['id']
            or _digest(payload)!=reference['sha256']):raise ValueError()
    name,manifest,result=_journal(selected.host_material().journal_root,payload['operation_id'],str(payload['version']))
    if (name,manifest,result)!=(payload['journal'],payload['manifest'],payload['result']):raise ValueError()
    return payload


def facts(session,actor,agent,payload,reference,selected=None):
    """All successful native facts must be visible in this exact root."""
    if type(payload) is dict and type(payload.get('version')) is int and payload['version']==3:
        from . import local_sales_receipts
        if selected is None:raise ValueError()
        return local_sales_receipts.facts(session,actor,agent,payload,reference,selected)
    _authorize(session,actor,agent)
    run=session.get(AgentRun,payload['run_id']);execution=session.get(AgentExecution,payload['run_id'])
    step=session.get(AgentStep,payload['step_id']);native=session.get(AgentPiToolReceipt,payload['native_id'])
    call=session.scalar(select(AgentToolCall).where(AgentToolCall.run_id==payload['run_id'],AgentToolCall.step_id==payload['step_id']))
    if any(x is None for x in (run,execution,step,native,call)):raise ValueError()
    runtime=contract(str(payload['version'])); version=str(payload['version'])
    binding=execution.frozen_context.get('pi_readonly_binding',{})
    if binding.get('sales_contract','1') != version: raise ValueError()
    if (type(actor) is WorkPrincipal and actor.run_id!=run.id or run.requester_id!=actor.subject_id
            or (run.organization_id,run.store_id)!=(actor.organization_id,actor.store_id)
            or binding.get('sales_source')!=payload['pin'] or binding.get('agent_id')!=agent.subject_id
            or tool_registry.pi_binding_digest(binding)!=run.toolset_version
            or repository.runtime_identity(execution.frozen_context)!=repository.SDK_RUNTIME):raise ValueError()
    if reference not in execution.frozen_context.get('evidence',[]):raise ValueError()
    if execution.cancel_requested_at is not None:raise ValueError()
    for row in (execution,step,native,call):
        if (row.organization_id,row.store_id)!=(actor.organization_id,actor.store_id):raise ValueError()
    output='sha256:'+_digest(payload['result'])
    if ((step.run_id,step.kind,step.state,step.output_ref)!=(run.id,'tool','succeeded',output)
            or (native.run_id,native.tool_step_id,native.state,native.output_ref)!=(run.id,step.id,'succeeded',output)
            or (native.tool_key,native.tool_version,native.pi_name,native.toolset_version)!=(runtime.KEY,version,runtime.NAME,run.toolset_version)
            or native.input_digest!=_digest({}) or (call.tool_key,call.tool_version,call.input_digest)!=(runtime.KEY,version,_digest({}))
            or call.idempotency_key!='pi:'+_digest([run.id,native.native_call_id])):raise ValueError()
    return payload['result']
