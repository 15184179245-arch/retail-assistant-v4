"""Local v3 source evidence; successful Native/Step facts authorize disclosure."""
import json
import os
from uuid import UUID

from sqlalchemy import select

from backend.app.modules.agents import local_sales_runtime as runtime, repository, tool_registry
from backend.app.modules.agents.models import AgentRun, AgentExecution, AgentStep, AgentToolCall, AgentPiToolReceipt
from backend.app.modules.assistant.principal import WorkPrincipal
from . import sales_ranking_receipts as base


def persist(selected, *, run_id, step_id, native_id, arguments, snapshot, result, authority_check):
    """No file alone is disclosure authority; caller's SQL transaction commits later."""
    runtime.Input.model_validate(arguments)
    args=json.loads(base._json(arguments))
    result=runtime.Output.model_validate(result).model_dump(mode='json')
    selected.revalidate()
    if selected.recompute(args,snapshot,authority_check=authority_check)!=result:
        raise ValueError()
    operation=base._id(run_id,step_id)
    payload=dict(version=3,run_id=run_id,step_id=step_id,native_id=native_id,
                 pin=selected.pin_dict(),operation_id=operation,arguments=args,snapshot=snapshot,result=result)
    fd=base._root_fd(selected._environment.root)
    try:base._write(fd,'sales-result-'+operation+'.json',base._json(payload).encode())
    finally:os.close(fd)
    return dict(kind=base.KIND,id=operation,sha256=base._digest(payload))


def read(selected,reference):
    """Read protected payload, not authorized facts; facts() must always follow."""
    if (type(reference) is not dict or set(reference)!={'kind','id','sha256'} or
            reference['kind']!=base.KIND or str(UUID(reference['id']))!=reference['id']):
        raise ValueError()
    selected.revalidate()
    fd=base._root_fd(selected._environment.root)
    try:raw=base._read(fd,'sales-result-'+reference['id']+'.json',131072)
    finally:os.close(fd)
    from .live_loader import _decode
    payload=_decode(raw)
    if (type(payload) is not dict or set(payload)!={'version','run_id','step_id','native_id','pin',
            'operation_id','arguments','snapshot','result'} or type(payload['version']) is not int or
            payload['version']!=3 or payload['pin']!=selected.pin_dict() or
            payload['operation_id']!=reference['id'] or base._id(payload['run_id'],payload['step_id'])!=reference['id'] or
            base._digest(payload)!=reference['sha256']):
        raise ValueError()
    runtime.Input.model_validate(payload['arguments'])
    if runtime.Output.model_validate(payload['result']).model_dump(mode='json')!=payload['result']:
        raise ValueError()
    return payload


def _check_facts(session,actor,agent,payload,reference,selected):
    """Recheck current authorization and exact same-root successful Native facts."""
    def authorized():
        base._authorize(session,actor,agent)
        selected.revalidate()
        return True
    authorized()
    run=session.get(AgentRun,payload['run_id']);execution=session.get(AgentExecution,payload['run_id'])
    step=session.get(AgentStep,payload['step_id']);native=session.get(AgentPiToolReceipt,payload['native_id'])
    call=session.scalar(select(AgentToolCall).where(AgentToolCall.run_id==payload['run_id'],AgentToolCall.step_id==payload['step_id']))
    if any(x is None for x in (run,execution,step,native,call)):
        raise ValueError()
    binding=execution.frozen_context.get('pi_readonly_binding',{})
    if (binding.get('sales_contract')!='3' or binding.get('sales_source')!=payload['pin'] or
            payload['pin']!=selected.pin_dict() or binding.get('agent_id')!=agent.subject_id or
            type(actor) is WorkPrincipal and actor.run_id!=run.id or run.requester_id!=actor.subject_id or
            (run.organization_id,run.store_id)!=(actor.organization_id,actor.store_id) or
            tool_registry.pi_binding_digest(binding)!=run.toolset_version or
            repository.runtime_identity(execution.frozen_context)!=repository.SDK_RUNTIME or
            reference not in execution.frozen_context.get('evidence',[]) or execution.cancel_requested_at is not None):
        raise ValueError()
    for row in (execution,step,native,call):
        if (row.organization_id,row.store_id)!=(actor.organization_id,actor.store_id):
            raise ValueError()
    output='sha256:'+base._digest(payload['result']);input_sha=base._digest(payload['arguments'])
    if ((step.run_id,step.kind,step.state,step.output_ref)!=(run.id,'tool','succeeded',output) or
            (native.run_id,native.tool_step_id,native.state,native.output_ref)!=(run.id,step.id,'succeeded',output) or
            (native.tool_key,native.tool_version,native.pi_name,native.toolset_version)!=(runtime.KEY,'3',runtime.NAME,run.toolset_version) or
            native.input_digest!=input_sha or (call.tool_key,call.tool_version,call.input_digest)!=(runtime.KEY,'3',input_sha) or
            call.idempotency_key!='pi:'+base._digest([run.id,native.native_call_id])):
        raise ValueError()
    authorized()


def facts(session,actor,agent,payload,reference,selected):
    _check_facts(session,actor,agent,payload,reference,selected)
    def authorized():
        base._authorize(session,actor,agent)
        selected.revalidate()
        return True
    result=selected.recompute(payload['arguments'],payload['snapshot'],authority_check=authorized)
    if result!=payload['result']:
        raise ValueError()
    authorized()
    return result
