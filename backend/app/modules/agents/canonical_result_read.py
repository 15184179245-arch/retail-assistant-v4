"""Authenticated historical result disclosure. Reads facts; never resumes Pi."""
from datetime import datetime, timezone
from hashlib import sha256
import json

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from backend.app.core.security import AuthenticatedSubject
from backend.app.modules.auth.service import validate_actor_session
from backend.app.modules.identity.command_guard import refresh_authority
from backend.app.modules.memory.models import PersonalMemory
from backend.app.modules.memory.schemas import MemoryOut, MemorySearchPage
from . import canonical_result as codec, repository, tool_registry
from .models import AgentExecution, AgentPiToolResult, AgentPiToolReceipt, AgentStep, AgentToolCall


class ResultWithheld(ValueError):
    pass


def _withhold(reason):
    raise ResultWithheld(reason)


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _one(session, model, run, **values):
    clauses=[model.organization_id==run.organization_id, model.store_id==run.store_id]
    if hasattr(model, 'run_id'):clauses.append(model.run_id==run.id)
    clauses.extend(getattr(model,key)==value for key,value in values.items())
    return session.scalar(select(model).where(*clauses).execution_options(populate_existing=True))


def _owned(session, actor, run_id):
    from . import service
    refresh_authority(session)
    validate_actor_session(session, actor)
    return service._load_owned(session, actor, run_id)


def _facts(session, actor, run, step):
    value=_one(session, AgentPiToolResult, run, tool_step_id=step.id)
    if value is None:return None
    try:envelope=codec.validate_columns(value)
    except ValueError:_withhold('binding_invalid')
    control=_one(session, AgentExecution, run)
    native=_one(session, AgentPiToolReceipt, run, id=value.pi_receipt_id)
    model=_one(session, AgentStep, run, id=value.model_step_id)
    call=_one(session, AgentToolCall, run, step_id=step.id)
    binding=envelope['binding'];scope=envelope['scope']
    if control is None or native is None or model is None or call is None:_withhold('binding_invalid')
    try:
        identity=repository.runtime_identity(control.frozen_context)
        record=repository.validate_pi_runtime_record(control.frozen_context['pi_runtime_record'])
        frozen=tool_registry._pi_binding_shape(control.frozen_context['pi_readonly_binding'])
    except (ValueError,KeyError,TypeError,repository.AgentPersistenceError,tool_registry.ToolRegistryError):_withhold('binding_invalid')
    expected_scope={'organization_id':run.organization_id,'store_id':run.store_id,
                    'requester_id':run.requester_id,'agent_id':frozen['agent_id']}
    expected_binding={'run_id':run.id,'model_step_id':model.id,'tool_step_id':step.id,
        'pi_receipt_id':native.id,'native_call_id':native.native_call_id,'tool_key':native.tool_key,
        'tool_name':native.pi_name,'tool_version':native.tool_version,'toolset_version':native.toolset_version,
        'input_digest':native.input_digest,'execution_epoch':value.execution_epoch}
    call_key='pi:'+_digest([run.id,native.native_call_id])
    tool_input='sha256:'+_digest({'toolset_version':native.toolset_version,'call_key':call_key,
        'tool_key':native.tool_key,'version':native.tool_version,'input_digest':native.input_digest})
    if (identity!=repository.SDK_RUNTIME or control.mode!='worker_v2'
        or type(control.epoch)is not int or control.epoch<1
        or record['launch_profile']not in codec.RESULT_PROFILES or envelope['runtime_record']!=record
        or scope!=expected_scope or binding!=expected_binding or value.execution_epoch>control.epoch
        or frozen['run_id']!=run.id or frozen['organization_id']!=run.organization_id
        or frozen['store_id']!=run.store_id or frozen['requester_id']!=actor.subject_id
        or tool_registry.pi_binding_digest(frozen)!=run.toolset_version
        or native.contract_version!=1 or native.state!='succeeded'
        or native.model_kind!='model' or native.tool_kind!='tool' or native.tool_step_id!=step.id
        or native.model_step_id!=model.id or step.kind!='tool' or step.state!='succeeded'
        or model.kind!='model' or model.state!='succeeded'
        or type(model.sequence_no)is not int or type(step.sequence_no)is not int
        or not 1<=model.sequence_no<step.sequence_no
        or step.output_ref!=native.output_ref or envelope['output_ref']!=native.output_ref
        or native.toolset_version!=run.toolset_version or step.input_ref!=tool_input
        or (call.step_kind,call.tool_key,call.tool_version,call.input_digest,call.idempotency_key)
           !=('tool',native.tool_key,native.tool_version,native.input_digest,call_key)):
        _withhold('binding_invalid')
    try:
        if tool_registry.pi_pair(frozen,native.pi_name,native.tool_version)!=(native.tool_key,native.tool_version):
            _withhold('binding_invalid')
    except tool_registry.ToolRegistryError:_withhold('binding_invalid')
    return envelope,frozen


def _authorized(session, actor, run, envelope, frozen, now):
    # Expire all cached authority objects before the existing pure verifier;
    # its SELECTs cannot reuse a held pre-revocation grant/session/account.
    refresh_authority(session)
    validate_actor_session(session,actor,now=now)
    agent=AuthenticatedSubject(frozen['agent_id'],run.organization_id,run.store_id,actor.auth_expires_at)
    try:
        tool_registry.verify_pi_binding(session,actor,agent,run_id=run.id,binding=frozen,
            expected_digest=run.toolset_version,now=now)
    except tool_registry.ToolRegistryError:_withhold('authorization_changed')
    except HTTPException as error:
        if error.status_code==401:raise
        _withhold('authorization_changed')


def _payload(session, actor, run, envelope, now):
    if envelope['binding']['tool_key']!='memory.search':_withhold('source_unsupported')
    if envelope['result_kind']=='tool_error':
        from .schemas import PiSafeToolError
        try:public_payload=PiSafeToolError.model_validate(envelope['payload']).model_dump(mode='json')
        except ValidationError:_withhold('source_incomplete')
        if public_payload!=envelope['payload'] or _digest(public_payload)!=envelope['payload_sha256']:
            _withhold('source_incomplete')
        return dict(envelope['payload'])
    if codec.recovery_requirements(envelope)!='source_validation_required':_withhold('source_incomplete')
    try:
        page=MemorySearchPage.model_validate_json(json.dumps(envelope['payload']),strict=True)
        from .schemas import PiMemoryResultPayload
        public_payload=PiMemoryResultPayload.model_validate(envelope['payload'])
    except ValidationError:_withhold('source_incomplete')
    # Strict validation may fill schema defaults. Disclose only if the final
    # public representation still commits the exact captured payload bytes.
    if _digest(public_payload.model_dump(mode='json'))!=envelope['payload_sha256']:
        _withhold('source_incomplete')
    refs=envelope['source_refs'];memory_refs={};permission=False
    for ref in refs:
        if ref=={'kind':'permission','key':'memory.read'} and not permission:permission=True
        elif (set(ref)=={'kind','id','version'} and ref['kind']=='memory'
              and type(ref['id'])is str and type(ref['version'])is int and ref['id']not in memory_refs):
            memory_refs[ref['id']]=ref['version']
        else:_withhold('source_incomplete')
    if not permission or len(memory_refs)!=len(page.items):_withhold('source_incomplete')
    for item in page.items:
        row=_one(session,PersonalMemory,run,id=item.id,owner_id=actor.subject_id)
        if (row is None or not row.active or row.version!=memory_refs.get(item.id)
            or (row.expires_at is not None and row.expires_at<=now)
            or not (row.persistent or row.origin_session_id==actor.session_id)):
            _withhold('source_changed')
        current=MemoryOut.model_validate(row,from_attributes=True).model_dump(mode='json')
        if item.model_dump(mode='json')!=current:_withhold('source_changed')
    return envelope['payload']


def read_tool_result(session, actor, run_id, tool_step_id):
    from . import service
    if session.new or session.dirty or session.deleted:
        raise service.AgentRuntimeError('agent_result_read_pending',409)
    if type(actor)is not AuthenticatedSubject or actor.account_id is None or actor.session_id is None:
        raise service.AgentRuntimeError('agent_login_required',401)
    common={'run_id':run_id,'tool_step_id':tool_step_id}
    with session.no_autoflush:
        run=_owned(session,actor,run_id)
        step=_one(session,AgentStep,run,id=tool_step_id,kind='tool')
        if step is None:raise service.AgentRuntimeError('agent_not_found',404)
        try:
            facts=_facts(session,actor,run,step)
            if facts is None:return {**common,'status':'not_recorded','reason':'result_absent'}
            envelope,frozen=facts
            now=datetime.now(timezone.utc)
            _authorized(session,actor,run,envelope,frozen,now)
            payload=_payload(session,actor,run,envelope,now)
            # Final disclosure repeats owned/login/tool/source reads. This is
            # current-read validation, not a PostgreSQL concurrency claim.
            run=_owned(session,actor,run_id)
            step=_one(session,AgentStep,run,id=tool_step_id,kind='tool')
            if step is None:_withhold('binding_invalid')
            final=_facts(session,actor,run,step)
            if final is None or final[0]!=envelope:_withhold('binding_invalid')
            now=datetime.now(timezone.utc)
            _authorized(session,actor,run,envelope,frozen,now)
            if _payload(session,actor,run,envelope,now)!=payload:_withhold('source_changed')
            return {**common,'status':'available','tool_key':envelope['binding']['tool_key'],
                'tool_version':envelope['binding']['tool_version'],'captured_at':envelope['captured_at'],
                'output_ref':envelope['output_ref'],'canonical_ref':codec.canonical_reference(envelope),
                'kind':envelope['result_kind'],'payload':payload}
        except ResultWithheld as error:
            return {**common,'status':'withheld','reason':str(error)}
