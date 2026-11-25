"""Experimental single fresh-DB SDK binding, NOT a formal IPC/engine adapter.

Only a bridge created by submit_fresh can own one new Run. Losing this object
loses the binding: no attach, restore, fallback or SDK/legacy engine migration.
The SDK owns inference. This object only validates native events and applies
real Host operations in short transactions. It never runs the Python loop.
"""
from __future__ import annotations
from copy import deepcopy
from contextlib import contextmanager, nullcontext
import json
import time
from hashlib import sha256
from uuid import uuid4
from sqlalchemy import select,func
from backend.app.db.session import (
    has_sqlite_timeout_owner, discard_failed_sqlite_timeout_owner,
    forget_released_sqlite_timeout_owner,
)
from backend.app.modules.agents import service,repository,cost_ledger,answer_receipts,conversation_service,tool_registry
from backend.app.modules.agents.models import AgentRun
from backend.app.modules.agents.schemas import RunState,StepState,StepKind,StepCreate,StepTransition
from backend.app.workers import agent_executor as executor
from backend.app.workers.agent_guard import guard_execution

def digest(value):
 return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
class BridgeError(ValueError):pass

def reject(code):raise BridgeError(code)

def _provider_http_failure(response):
    """Expose only a validated transport status, never exception/body text.

    This is an execution failure reason, not a billing determination. Provider
    receipts and cost reservations remain unknown without observed usage.
    """
    from backend.app.modules.agents.runtime_protocol import _pi_provider_response
    try:
        _pi_provider_response(response)
    except Exception:
        return None
    status = response['status']
    return f'pi_provider_http_{status}' if 400 <= status <= 599 else None

def _provider_transport_failure(error):
    """Finite Host transport phase only; exception strings never become facts."""
    from .pi_online_transport import PiOnlineTransportError, FAILURE_PHASES
    if type(error) is PiOnlineTransportError:
        phase = error.failure_phase
        if type(phase) is str and phase in FAILURE_PHASES:
            return 'pi_provider_transport_' + phase
    return 'pi_provider_call_stopped'

class FreshRunBridge:
 def __init__(self):reject('use_submit_fresh_only')
 @classmethod
 def submit_fresh(cls,*,session_factory,requester,machine_token,text,key):
  with session_factory() as s:
   receipt=service.submit_worker_run(s,requester,text,key,runtime_binding=repository.sdk_runtime_binding())
   if receipt.get('replayed') is not False:reject('existing_run_attach_forbidden')
  return cls._claim_new_receipt(receipt,session_factory=session_factory,machine_token=machine_token)
 @classmethod
 def submit_for_http(cls,*,session_factory,requester,machine_token,text,key,requested_run_id):
  # A successful service return is the sole new-versus-existing authority. Never
  # reconstruct this result after a commit/response exception or re-claim by ID.
  with session_factory() as s:
   receipt=service.submit_worker_run(s,requester,text,key,requested_run_id=requested_run_id,runtime_binding=repository.sdk_runtime_binding())
  if receipt.get('replayed') is not False:return receipt,None
  return receipt,cls._claim_new_receipt(receipt,session_factory=session_factory,machine_token=machine_token)
 @classmethod
 def _claim_new_receipt(cls,receipt,*,session_factory,machine_token):
  # No ownership exists here until _claim returned normally after commit/exit.
  claim=executor._claim(session_factory=session_factory,machine_token=machine_token,lease_seconds=90,expected_runtime=repository.SDK_RUNTIME,run_id=receipt['run_id'])
  if claim is None or claim.run_id!=receipt['run_id']:reject('fresh_run_claim_mismatch')
  obj=object.__new__(cls);obj.factory=session_factory;obj.machine_token=machine_token;obj.claim=claim;obj.run_id=claim.run_id
  obj._close_attempts=[];obj._close_errors=[];obj._first_close_status=None
  obj.pending=None;obj.call=None;obj.evidence=[];obj.closed=False;obj.seen=set();obj.used_call_ids=set();obj.permits=0;obj.tool_count=0
  try:
   with session_factory() as s:
    ctx=obj._guard(s);descriptions=obj._tools(s,ctx);obj.schema=descriptions[0]['input_schema'];s.commit()
  except BaseException as error:
   obj._fail_closed('sdk_claim_initialization_failed',error=error)
   raise
  return obj
 def _scope(self,run,epoch):
  if self.closed:reject('binding_closed')
  if run!=self.run_id or type(epoch)is not int or epoch!=self.claim.epoch:reject('binding_scope_mismatch')
 def _tools(self,s,ctx):
  descriptions,skill=executor._tool_views(s,ctx,'')
  if skill is not None or [(d['tool_key'],d['version']) for d in descriptions]!=[('tasks.list_mine','1')]:reject('only_exact_read_tool_binding')
  return descriptions
 def _guard(self,s,**kw):
  ctx=guard_execution(s,self.machine_token,self.claim,additional_evidence=self.evidence or None,expected_runtime=repository.SDK_RUNTIME,**kw)
  self._tools(s,ctx)
  return ctx
 def begin_model(self,*,run,epoch,request_id,input_digest):
  self._scope(run,epoch)
  if self.pending is not None or self.call is not None:reject('prior_attempt_unfinished')
  if not isinstance(request_id,str) or not 1<=len(request_id)<=100 or request_id in self.seen:reject('request_replay')
  if not isinstance(input_digest,str) or len(input_digest)!=64 or any(c not in '0123456789abcdef'for c in input_digest):reject('invalid_input_digest')
  try:
   with self.factory() as s:
    ctx=self._guard(s);r=ctx.run
    if r.state=='created':r=repository.transition_run(s,organization_id=r.organization_id,store_id=r.store_id,run_id=r.id,expected_version=r.version,to_state=RunState.RUNNING)
    steps=repository.list_steps(s,r.organization_id,r.store_id,r.id);budget=executor._remaining_budget(r,steps)
    step=repository.append_step(s,StepCreate(id=str(uuid4()),organization_id=r.organization_id,store_id=r.store_id,run_id=r.id,sequence_no=len(steps)+1,kind=StepKind.MODEL,input_ref='sha256:'+input_digest),expected_run_version=r.version,now=ctx.now)
    r=repository.get_run(s,r.organization_id,r.store_id,r.id)
    step=repository.transition_step(s,organization_id=r.organization_id,store_id=r.store_id,run_id=r.id,step_id=step.id,expected_run_version=r.version,expected_step_version=step.version,change=StepTransition(to_state=StepState.RUNNING),now=ctx.now)
    cost_ledger.reserve_cost(s,ctx.person,r,step,max_input_tokens=budget['input'],max_output_tokens=budget['output'],now=ctx.now,commit=False)
    repository.record_worker_dispatch(s,self.claim,step_id=step.id,expected_runtime=repository.SDK_RUNTIME)
    r=repository.get_run(s,r.organization_id,r.store_id,r.id)
    pending={'request_id':request_id,'permit':str(uuid4()),'step_id':step.id,'run_version':r.version,'step_version':step.version,'budget':budget}
    s.commit()
  except Exception as error:
   self._fail_closed("sdk_model_begin_failed",error=error)
   raise
  self.pending=pending;self.seen.add(request_id);self.permits+=1
  return dict(pending)
 def complete_model(self,*,run,epoch,permit,message):
  self._scope(run,epoch);p=self.pending
  if p is None or permit!=p['permit']:reject('attempt_permit_mismatch')
  try:
   return self._accept_model(run=run,epoch=epoch,permit=permit,message=message)
  except Exception as error:
   self._fail_closed("sdk_physical_result_rejected",error=error)
   raise
 def _accept_model(self,*,run,epoch,permit,message):
  self._scope(run,epoch);p=self.pending
  if p is None or permit!=p['permit']:reject('attempt_permit_mismatch')
  if not isinstance(message,dict) or set(message)!={'role','content','usage','stopReason'} or message['role']!='assistant':reject('native_message_invalid')
  usage=message['usage']
  if not isinstance(usage,dict) or set(usage)!={'input','output'} or any(type(v)is not int or v<0 for v in usage.values()):reject('usage_invalid')
  if usage['input']>p['budget']['input'] or usage['output']>p['budget']['output']:reject('usage_exceeded')
  content=message['content']
  if not isinstance(content,list) or len(content)!=1:reject('single_native_content_required')
  item=content[0];call=None;answer=None
  if message['stopReason']=='toolUse':
   if not isinstance(item,dict) or set(item)!={'type','id','name','arguments'} or item['type']!='toolCall' or item['name']!='tasks.list_mine@1' or not isinstance(item['id'],str) or not 1<=len(item['id'])<=100:reject('read_tool_call_required')
   if item['id'] in self.used_call_ids:reject('call_id_replay')
   definition=tool_registry.resolve_tool('tasks.list_mine','1',store_id=self.claim.store_id)
   definition.input_model.model_validate(item['arguments']);call=json.loads(json.dumps(item,allow_nan=False))
  elif message['stopReason']=='stop':
   if not isinstance(item,dict) or set(item)!={'type','text'} or item['type']!='text' or not isinstance(item['text'],str) or not 1<=len(item['text'])<=8000 or not self.evidence:reject('sourced_final_required')
   answer=item['text']
  else:reject('native_stop_not_supported')
  with self.factory() as s:
   ctx=self._guard(s,step_id=p['step_id'],expected_run_version=p['run_version'],expected_step_version=p['step_version']);r=ctx.run;step=ctx.step
   ledger=cost_ledger.settle_cost(s,ctx.person,r,step,input_tokens=usage['input'],output_tokens=usage['output'],now=ctx.now,commit=False)
   if ledger is None or ledger.status in ('unknown','reconciliation_required'):reject('usage_unsettled')
   repository.transition_step(s,organization_id=r.organization_id,store_id=r.store_id,run_id=r.id,step_id=step.id,expected_run_version=r.version,expected_step_version=step.version,change=StepTransition(to_state=StepState.SUCCEEDED,input_tokens=usage['input'],output_tokens=usage['output'],output_ref='sha256:'+digest(message)),now=ctx.now)
   r=repository.get_run(s,r.organization_id,r.store_id,r.id)
   if answer is not None:
    refs=answer_receipts.merge_evidence(ctx.execution.frozen_context.get('evidence')or[],self.evidence)
    valid,_=answer_receipts.validate_evidence(s,ctx.person,refs)
    if not valid:reject('final_evidence_unavailable')
    stored=conversation_service.append_message(s,ctx.person,conversation_id=ctx.execution.frozen_context['conversation_id'],role='assistant',content=answer,source_ref='run:'+r.id,now=ctx.now)
    r=repository.transition_run(s,organization_id=r.organization_id,store_id=r.store_id,run_id=r.id,expected_version=r.version,to_state=RunState.COMPLETED)
    repository.record_answer(s,run=r,message=stored,evidence=refs,own_evidence=self.evidence)
    repository.settle_worker_attempt(s,self.claim,step_id=step.id,outcome='finished',expected_runtime=repository.SDK_RUNTIME);executor._audit(s,ctx,'agent_run.worker_finished','completed')
   else:repository.settle_worker_attempt(s,self.claim,step_id=step.id,outcome='continue',expected_runtime=repository.SDK_RUNTIME)
   s.commit()
  self.pending=None;self.call=call;self.closed=answer is not None
  if call is not None:self.used_call_ids.add(call['id'])
  return {'committed':True,'finished':self.closed}
 def invoke_read(self,*,run,epoch,call_id,arguments):
  self._scope(run,epoch);call=self.call
  if self.pending is not None or call is None or call_id!=call['id'] or digest(arguments)!=digest(call['arguments']):reject('tool_call_binding_mismatch')
  try:
   definition=tool_registry.resolve_tool('tasks.list_mine','1',store_id=self.claim.store_id)
   decision={'tool_key':'tasks.list_mine','version':'1','arguments':arguments}
   with self.factory() as s:
    ctx=self._guard(s);executor._remaining_budget(ctx.run,repository.list_steps(s,ctx.run.organization_id,ctx.run.store_id,ctx.run.id),phase='tool')
    step,rv=executor._open_tool_step(s,ctx,self.claim,ctx.run,decision,definition,expected_runtime=repository.SDK_RUNTIME);sid,sv=step.id,step.version;s.commit()
   # After dispatch intent commit, any error stays ambiguous until recovery.
   self.call=None
   with self.factory() as s:
    ctx=self._guard(s,step_id=sid,expected_run_version=rv,expected_step_version=sv)
    started=time.monotonic()
    result=tool_registry.invoke_tool(s,requester=ctx.person,agent=ctx.agent,tool_key='tasks.list_mine',version='1',arguments=arguments)
    refs=answer_receipts.capture_tool_evidence(s,ctx.person,tool_key=definition.key,permission=definition.requester_permission,result=result)
    now=repository._db_clock(s);row=repository.require_worker_claim(s,self.claim,expected_runtime=repository.SDK_RUNTIME)
    if time.monotonic()-started>definition.timeout_seconds or row.cancel_requested_at is not None or ctx.run.deadline_at<=now:reject('tool_dispatch_expired')
    repository.transition_step(s,organization_id=ctx.run.organization_id,store_id=ctx.run.store_id,run_id=ctx.run.id,step_id=ctx.step.id,expected_run_version=ctx.run.version,expected_step_version=ctx.step.version,change=StepTransition(to_state=StepState.SUCCEEDED,output_ref='sha256:'+digest(result)),now=ctx.now)
    executor._append_frozen_evidence(s,ctx,refs);repository.settle_worker_attempt(s,self.claim,step_id=ctx.step.id,outcome='continue',expected_runtime=repository.SDK_RUNTIME);executor._audit(s,ctx,'agent_tool.worker_invoked',definition.key);s.commit()
   self.evidence=answer_receipts.merge_evidence(self.evidence,refs);self.tool_count+=1
   return result
  except Exception as error:
   self._fail_closed("sdk_tool_response_failed",error=error)
   raise
 @property
 def close_attempts(self):
  return json.loads(json.dumps(self._close_attempts))
 @property
 def first_close_status(self):
  return self._first_close_status
 def _fail_closed(self,code,*,error=None):
  self.closed=True
  errors=[]
  status=executor._close_sdk_bound_attempt(self.factory,self.machine_token,self.claim,code=code,errors=errors)
  attempt={'index':len(self._close_attempts)+1,'code':code,'status':status,
           'operation_error':executor._sdk_exception_detail(error)if error is not None else None,
           'recovery_errors':[executor._sdk_exception_detail(e)for e in errors]}
  self._close_errors.append({'operation':error,'recovery':tuple(errors)})
  self._close_attempts.append(attempt)
  if self._first_close_status is None:self._first_close_status=status
  self.close_status=status
  return status
 def abandon_unknown(self):
  return self._fail_closed("sdk_attempt_response_unknown")



class PiRunBridge:
    """Formal 028 fresh-owner Host gateway, distinct from the experiment above.

    Only a normally returned new submit+claim constructs this object. It never
    attaches a previous Run, executes an SDK loop, or retries a native call.
    No permission is delegated to Node: all three call boundaries use the
    original 027 allowlist and the original employee/agent execution guard.
    """
    def __init__(self):
        reject("use_pi_submit_only")

    @classmethod
    def submit_for_http(cls, *, session_factory, requester, machine_token,
                        text, key, requested_run_id, runtime_record, offline_transport=None, campaign=None,
                        category_source=None, category_products_contract=None, research_source=None, sales_source=None,
                        tool_selection=None):
        record = repository.validate_pi_runtime_record(runtime_record)
        if campaign is not None:
            from backend.app.modules.agents.pi_online_campaign import PiOnlineCampaign
            if type(campaign) is not PiOnlineCampaign:
                reject('pi_campaign_held')
            campaign.check_submission(requester, text, requested_run_id, record, offline_transport)
        if record['contract_version'] in (2, 3):
            # H1 is internal/offline only. HTTP and launch do not supply this
            # Host-owned object. No frame/config/env can create a transport.
            if offline_transport is None or not callable(getattr(offline_transport, 'send', None)):
                reject('pi_online_transport_unconfigured')
            from backend.app.modules.agents.pi_online_transport import PiOnlineTransport
            import http.client
            if (type(offline_transport) is PiOnlineTransport
                    and offline_transport._connection_factory is http.client.HTTPSConnection
                    and campaign is None):
                reject('pi_online_transport_held')
        elif offline_transport is not None:
            reject('pi_online_transport_binding')
        with session_factory() as session:
            receipt = service.submit_worker_run(session, requester, text, key,
                requested_run_id=requested_run_id, runtime_binding=repository.sdk_runtime_binding(),
                tool_profile=repository.pi_record_tool_profile(record), pi_runtime_record=record,
                category_source=category_source.pin() if category_source is not None else None,
                category_products_contract=category_products_contract,
                research_source=research_source.pin() if research_source is not None else None,
                sales_source=sales_source.pin_dict() if sales_source is not None else None,
                tool_selection=tool_selection)
        if receipt.get("replayed") is not False:
            return receipt, None
        claim = executor._claim(session_factory=session_factory, machine_token=machine_token,
            lease_seconds=90, expected_runtime=repository.SDK_RUNTIME, run_id=receipt["run_id"])
        if claim is None or claim.run_id != receipt["run_id"]:
            reject("pi_new_claim_unresolved")
        obj = object.__new__(cls)
        obj.factory, obj.machine_token, obj.claim, obj.run_id = session_factory, machine_token, claim, claim.run_id
        obj.runtime_record = record
        obj._sales_source = sales_source
        obj._research_source = research_source
        obj._category_source = category_source
        obj._category_products_contract = category_products_contract
        obj._campaign = campaign
        obj._offline_transport = offline_transport
        obj.pending = obj.call = None
        obj.queued_calls = []
        obj.evidence, obj._close_attempts, obj._close_errors = [], [], []
        obj.closed, obj._first_close_status = False, None
        obj.seen = set()
        obj.permits = obj.tool_count = 0
        try:
            with session_factory() as session:
                if campaign is not None:
                    ctx = guard_execution(session, obj.machine_token, obj.claim,
                        expected_runtime=repository.SDK_RUNTIME)
                    obj._tools(session, ctx)
                    campaign.check_context(ctx, record)
                    repository.bind_pi_campaign(session, obj.claim, campaign.snapshot())
                ctx = obj._guard(session)
                if record['contract_version'] in (2, 3) and callable(getattr(offline_transport, 'bind_run', None)):
                    obj._offline_transport = offline_transport.bind_run(
                        ctx.run.organization_id, ctx.run.store_id, ctx.run.id)
                obj.toolset_version = ctx.run.toolset_version
                obj.model_identity = {"provider": ctx.run.provider_key, "model": ctx.run.model}
                binding = ctx.execution.frozen_context["pi_readonly_binding"]
                versions = {entry['name']: entry['version'] for entry in binding['tools']}
                if len(versions) != len(binding['tools']): reject('pi_tool_name_duplicate')
                obj.tools = [dict(t, version=versions[t['name']])
                             for t in tool_registry.pi_model_tools(binding)]
                session.commit()
        except BaseException as error:
            obj._fail_closed("pi_claim_initialization_failed", error=error)
            raise
        return receipt, obj

    @contextmanager
    def _transaction(self, label, *, required_seconds=0):
        """Phase is outside the ORIGINAL application Session, through close."""
        control = getattr(self, '_execution_control', None)
        phase = (nullcontext(None) if control is None else
                 control.accounting_transaction() if label == 'accounting' else
                 control.execute_transaction(label, required_seconds=required_seconds))
        with phase as owned:
            session = self.factory()
            primary = None
            try:
                with session:
                    if owned is not None:
                        session.info['_pi_execution_phase'] = owned
                    try:
                        yield session
                    except BaseException as error:
                        primary = error
                        raise
                    finally:
                        session.info.pop('_pi_execution_phase', None)
            except BaseException as error:
                if has_sqlite_timeout_owner(session):
                    original = primary if primary is not None else error
                    discard_failed_sqlite_timeout_owner(session, original)
                    if primary is not None and error is not primary:
                        primary.add_note("sqlite_timeout_session_close_failed: " + repr(error))
                        raise primary from error
                raise
            finally:
                forget_released_sqlite_timeout_owner(session)

    @staticmethod
    def _commit(session, *, terminal=False):
        phase = session.info.get('_pi_execution_phase')
        if phase is None:
            session.commit()
        else:
            phase.commit(session, terminal=terminal)

    def _scope(self, run, epoch):
        if self.closed:
            reject("binding_closed")
        if run != self.run_id or type(epoch) is not int or epoch != self.claim.epoch:
            reject("binding_scope_mismatch")
        control = getattr(self, '_execution_control', None)
        if control is not None:
            control.check()

    def _tools(self, session, ctx):
        frozen = ctx.execution.frozen_context
        if (type(frozen) is not dict or frozen.get("pi_runtime_record") != self.runtime_record
                or type(frozen.get("pi_readonly_binding")) is not dict):
            reject("pi_runtime_binding")
        sales_pin = frozen['pi_readonly_binding'].get('sales_source')
        if sales_pin is not None:
            from backend.app.modules.integrations.guanjia import sales_ranking_source
            if sales_ranking_source.current(session.get_bind(), ctx.person, pin=sales_pin) is not getattr(self, '_sales_source', None):
                reject('pi_sales_source_changed')
        research_pin = frozen['pi_readonly_binding'].get('research_source')
        if research_pin is not None:
            from . import research_host
            if research_host.current(session.get_bind(),ctx.person,pin=research_pin) is not getattr(self,'_research_source',None):
                reject('pi_research_source_changed')
        category_pin = frozen['pi_readonly_binding'].get('category_source')
        if category_pin is not None:
            from backend.app.modules.integrations.guanjia import category_host
            if category_host.current(session.get_bind(),ctx.person,pin=category_pin) is not getattr(self,'_category_source',None):
                reject('pi_category_source_changed')
        descriptions, skill = executor._tool_views(session, ctx, "")
        if skill is not None:
            reject("pi_runtime_binding")
        from backend.app.modules.agents import capability_discovery as discovery
        discovery.active_names(session, ctx)
        return descriptions

    def _guard(self, session, **kwargs):
        control = getattr(self, '_execution_control', None)
        if control is not None:
            control.check()
        ctx = guard_execution(session, self.machine_token, self.claim,
            additional_evidence=self.evidence or None, expected_runtime=repository.SDK_RUNTIME, **kwargs)
        self._tools(session, ctx)
        campaign = getattr(self, '_campaign', None)
        frozen = ctx.execution.frozen_context
        if campaign is None:
            if 'pi_online_campaign' in frozen:
                reject('pi_campaign_held')
        else:
            campaign.check_context(ctx, self.runtime_record)
            if frozen.get('pi_online_campaign') != campaign.snapshot():
                reject('pi_campaign_held')
        return ctx

    def _renew_discovery_lease(self, session):
        # Caller has already locked and revalidated current execution authority.
        # Renewal remains in its normal attempt transaction; never revive a lease.
        if repository.pi_record_tool_profile(self.runtime_record) in (
                tool_registry.PI_DISCOVERY_PROFILE, tool_registry.PI_SOURCE_PROFILE, tool_registry.PI_WORKBENCH_PROFILE):
            repository.heartbeat_worker_execution(session, self.claim, lease_seconds=90,
                expected_runtime=repository.SDK_RUNTIME)

    def start_payload(self, prompt):
        if self.closed:
            reject("binding_closed")
        campaign = getattr(self, '_campaign', None)
        if campaign is not None and campaign.mode == 'real-assistant-trial':
            from backend.app.core.model_context import fit_prompt, estimate_tokens
            with self._transaction('start_payload') as session:
                ctx = self._guard(session)
                history = ctx.execution.frozen_context.get('pi_conversation_context') or {}
                def render(context):
                    if not (context or {}).get('messages'):
                        return prompt
                    return (conversation_service.format_context(context)
                            + '\n\n当前用户请求：\n' + prompt)
                # Reserve tool/schema/system overhead; actual complete wire
                # request is checked again before any physical Provider send.
                capacity = campaign.max_input_reservation - estimate_tokens(json.dumps(self.tools,ensure_ascii=False)) - 2048
                fitted = fit_prompt(render, history, capacity)
                prompt = fitted
        payload = {"prompt": prompt, "tool_profile": repository.pi_record_tool_profile(self.runtime_record),
                "toolset_version": self.toolset_version,
                "tools": json.loads(json.dumps(self.tools, allow_nan=False))}
        campaign = getattr(self, '_campaign', None)
        if campaign is not None and campaign.max_output_tokens > 256:
            if self.runtime_record['contract_version'] not in (2, 3):
                reject('pi_campaign_held')
            with self._transaction('start_payload') as session:
                ctx = self._guard(session)
                # A fixed per-attempt request fits the Run even if every prior
                # attempt used its whole allowance. Physical permits recheck it.
                limit = min(campaign.max_output_tokens, ctx.run.max_output_tokens // campaign.max_attempts)
                if limit < 1:
                    reject('pi_campaign_held')
                payload['max_output_tokens'] = limit
        return payload

    def begin_model(self, *, run, epoch, request_id, input_digest, purpose, toolset_version,
                    request_digest=None):
        from backend.app.modules.agents import runtime_protocol as wire
        self._scope(run, epoch)
        if self.pending is not None or self.call is not None:
            reject("prior_attempt_unfinished")
        wire.identifier(request_id)
        wire._pi_digest(input_digest)
        online = self.runtime_record['contract_version'] in (2, 3)
        if online:
            wire._pi_digest(request_digest)
            if purpose != 'inference':
                reject('pi_online_automatic_path_held')
        elif request_digest is not None:
            reject('pi_attempt_binding')
        if request_id in self.seen or purpose not in wire.PI_PURPOSES or toolset_version != self.toolset_version:
            reject("pi_attempt_binding")
        try:
            with self._transaction('model_begin') as session:
                ctx = self._guard(session)
                self._renew_discovery_lease(session)
                run = ctx.run
                if run.state == "created":
                    run = repository.transition_run(session, organization_id=run.organization_id,
                        store_id=run.store_id, run_id=run.id, expected_version=run.version, to_state=RunState.RUNNING)
                steps = repository.list_steps(session, run.organization_id, run.store_id, run.id)
                budget = executor._remaining_budget(run, steps)
                campaign = getattr(self, '_campaign', None)
                if campaign is not None:
                    campaign.check_facts(repository.pi_campaign_facts(session, self.claim,
                        require_completed=True), admission=True)
                    budget = {'input': min(budget['input'], campaign.max_input_reservation),
                              'output': min(budget['output'], campaign.max_output_tokens)}
                # A retry/compaction calls this same gate afresh. No parent
                # permit is transferable, and every physical call has a Step.
                step = repository.append_step(session, StepCreate(id=str(uuid4()),
                    organization_id=run.organization_id, store_id=run.store_id, run_id=run.id,
                    sequence_no=len(steps)+1, kind=StepKind.MODEL,
                    input_ref="sha256:"+digest({"purpose": purpose, "input_digest": input_digest,
                                              "toolset_version": toolset_version})),
                    expected_run_version=run.version, now=ctx.now)
                run = repository.get_run(session, run.organization_id, run.store_id, run.id)
                step = repository.transition_step(session, organization_id=run.organization_id,
                    store_id=run.store_id, run_id=run.id, step_id=step.id,
                    expected_run_version=run.version, expected_step_version=step.version,
                    change=StepTransition(to_state=StepState.RUNNING), now=ctx.now)
                cost_ledger.reserve_cost(session, ctx.person, run, step,
                    max_input_tokens=budget["input"], max_output_tokens=budget["output"], now=ctx.now, commit=False)
                repository.record_worker_dispatch(session, self.claim, step_id=step.id, expected_runtime=repository.SDK_RUNTIME)
                run = repository.get_run(session, run.organization_id, run.store_id, run.id)
                pending = {"request_id": request_id, "permit": str(uuid4()), "step_id": step.id,
                           "run_version": run.version, "step_version": step.version, "budget": budget}
                if online:
                    pending.update(input_digest=input_digest, request_digest=request_digest,
                                   toolset_version=toolset_version)
                    repository.permit_pi_provider(session, self.claim, step=step, pending=pending,
                        input_digest=input_digest, request_digest=request_digest,
                        runtime_record=self.runtime_record, run=run, now=ctx.now)
                    if campaign is not None:
                        campaign.check_facts(repository.pi_campaign_facts(session, self.claim))
                self._commit(session)
        except BaseException as error:
            self._fail_closed("pi_model_begin_failed", error=error)
            raise
        self.pending = pending
        self.seen.add(request_id)
        self.permits += 1
        return {"attempt_id": pending["step_id"], "step_id": pending["step_id"],
                "permit": pending["permit"], "limits": dict(budget)}

    def _decision(self, session, ctx, message):
        from backend.app.modules.agents import runtime_protocol as wire
        content = message["content"]
        profile = repository.pi_record_tool_profile(self.runtime_record)
        text_limit = 131072 if profile in (tool_registry.PI_DISCOVERY_PROFILE,
            tool_registry.PI_SOURCE_PROFILE, tool_registry.PI_WORKBENCH_PROFILE) else 8000
        memory_profile = profile != tool_registry.PI_READONLY_PROFILE
        if not memory_profile and len(content) != 1:
            reject("single_native_content_required")
        if message["stopReason"] == "toolUse":
            calls, seen, text_count = [], set(), 0
            if not 1 <= len(content) <= (5 if memory_profile else 1):
                reject("native_content_bound")
            for item in content:
                if memory_profile and isinstance(item, dict) and item.get('type') == 'text':
                    wire.exact(item, {'type', 'text'})
                    if type(item['text']) is not str or not 1 <= len(item['text']) <= text_limit or text_count:
                        reject('native_content_bound')
                    text_count += 1
                    continue
                wire.exact(item, {"type", "id", "name", "arguments"})
                if item["type"] != "toolCall":
                    reject("native_tool_call_required")
                if item['id'] in repository.pi_rejected_native_ids(session, self.claim):
                    reject('native_call_replay')
                entries = [entry for entry in ctx.execution.frozen_context['pi_readonly_binding']['tools']
                           if entry['name'] == item['name']]
                if len(entries) != 1: reject('pi_tool_binding_invalid')
                decision = executor.pi_readonly_decision(session, ctx, name=item["name"], version=entries[0]['version'],
                    native_call_id=item["id"], arguments=item["arguments"])
                if item['id'] in seen or session.scalar(repository._pi_receipt_query(self.claim, item["id"])) is not None:
                    reject("native_call_replay")
                seen.add(item['id'])
                calls.append({"name": item["name"], "decision": decision})
            if not 1 <= len(calls) <= (4 if memory_profile else 1):
                reject('native_content_bound')
            if memory_profile:
                # Reject the entire batch before accepting any effects. Retain
                # room for all tool steps and the next final model turn.
                steps = repository.list_steps(session, ctx.run.organization_id, ctx.run.store_id, ctx.run.id)
                if (sum(s.kind == 'tool' for s in steps) + len(calls) > ctx.run.max_tool_calls
                        or len(steps) + len(calls) + 1 > ctx.run.max_steps):
                    reject('native_batch_budget_exceeded')
                executor._remaining_budget(ctx.run, steps)
            return calls, None
        if len(content) != 1:
            reject('sourced_final_required')
        item = content[0]
        wire.exact(item, {"type", "text"})
        if (message["stopReason"] != "stop" or item["type"] != "text"
                or type(item["text"]) is not str or not 1 <= len(item["text"]) <= text_limit
                or (not self.evidence and getattr(getattr(self, '_campaign', None), 'mode', None) != 'real-assistant-trial')):
            reject("sourced_final_required")
        references = answer_receipts.merge_evidence(ctx.execution.frozen_context.get("evidence") or [], self.evidence)
        valid, _ = answer_receipts.validate_evidence(session, ctx.person, references)
        if not valid:
            reject("final_evidence_unavailable")
        return None, (item["text"], references)

    def complete_model(self, *, run, epoch, attempt_id, step_id, permit, message):
        from backend.app.modules.agents import runtime_protocol as wire
        from fastapi import HTTPException
        self._scope(run, epoch)
        online = self.runtime_record['contract_version'] in (2, 3)
        pending = self.pending
        try:
            if (pending is None or permit != pending["permit"]
                    or attempt_id != pending["step_id"] or step_id != attempt_id):
                reject("attempt_permit_mismatch")
            wire.exact(message, {"role", "content", "usage", "stopReason"})
            wire.exact(message["usage"], {"input", "output", "cacheRead", "cacheWrite"} if online else {"input", "output"})
            for value in message["usage"].values():
                wire.integer(value)
            if message["role"] != "assistant" or type(message["content"]) is not list:
                reject("native_message_invalid")
            usage, output_ref = message["usage"], "sha256:"+digest(message)
        except Exception as error:
            if online:
                self._fail_closed('pi_online_content_binding', error=error)
            raise
        denial = None
        feedback = None
        try:
            # Only a failed initial execution guard is eligible for the narrow
            # accounting fallback. Driver/flush/commit failures never are.
            with self._transaction('model_complete') as session:
                try:
                    ctx = guard_execution(session, self.machine_token, self.claim,
                        step_id=pending["step_id"], expected_run_version=pending["run_version"],
                        expected_step_version=pending["step_version"],
                        additional_evidence=self.evidence or None, expected_runtime=repository.SDK_RUNTIME)
                except (repository.AgentPersistenceError, HTTPException) as error:
                    session.rollback()  # must return normally before recovery
                    denial = error
                if denial is None:
                    current, model = ctx.run, ctx.step
                    if online:
                        ledger = repository.require_pi_content_binding(session, self.claim,
                            pending=pending, message=message, runtime_record=self.runtime_record)
                    else:
                        ledger = cost_ledger.settle_cost(session, ctx.person, current, model,
                            input_tokens=usage["input"], output_tokens=usage["output"], now=ctx.now, commit=False)
                    if ledger is None or ledger.status not in ("settled", "reconciliation_required"):
                        reject("pi_usage_unsettled")
                    model = repository.transition_step(session, organization_id=current.organization_id,
                        store_id=current.store_id, run_id=current.id, step_id=model.id,
                        expected_run_version=current.version, expected_step_version=model.version,
                        change=StepTransition(to_state=StepState.SUCCEEDED, input_tokens=usage["input"] + (usage["cacheRead"] + usage["cacheWrite"] if online else 0),
                            output_tokens=usage["output"], output_ref=output_ref), now=ctx.now)
                    current = repository.get_run(session, current.organization_id, current.store_id, current.id)
                    calls, answer = [], None
                    # Pure validation errors do not erase a known physical bill.
                    # Persistence/driver errors below are NOT swallowed.
                    try:
                        if (ledger.status != "settled" or usage["input"] + (usage["cacheRead"] + usage["cacheWrite"] if online else 0) > pending["budget"]["input"]
                                or usage["output"] > pending["budget"]["output"]):
                            reject("pi_usage_exceeded")
                        self._tools(session, ctx)
                        if online and pending.get('invalid_arguments'):
                            from . import capability_discovery
                            memory_profile = repository.pi_record_tool_profile(self.runtime_record) != tool_registry.PI_READONLY_PROFILE
                            batch = [item for item in message['content'] if item['type'] == 'toolCall']
                            if not 1 <= len(batch) <= (4 if memory_profile else 1):
                                reject('native_content_bound')
                            active = capability_discovery.active_names(session, ctx)
                            for item in batch:
                                if item['name'] not in active or item['name'] == 'tool_search':
                                    reject('agent_pi_tool_not_loaded')
                                if session.scalar(repository._pi_receipt_query(self.claim,item['id'])) is not None:
                                    reject('native_call_replay')
                            feedback = repository.record_pi_argument_rejection(session, self.claim,
                                step_id=model.id, message=message, toolset_version=self.toolset_version,
                                invalid_arguments=pending['invalid_arguments'])
                        else:
                            calls, answer = self._decision(session, ctx, message)
                    except (BridgeError, wire.ProtocolError, executor.AgentExecutorError,
                            tool_registry.ToolRegistryError, answer_receipts.EvidenceError, HTTPException) as error:
                        denial = error
                    for call in calls or []:
                        receipt = repository.accept_pi_tool_receipt(session, self.claim,
                            model_step_id=model.id, pi_name=call["name"], decision=call["decision"], now=ctx.now)
                        from . import public_trace_capture
                        public_trace_capture.capture(session, ctx, self.claim, receipt,
                            call['decision'], phase='accepted')
                        call["receipt_id"] = receipt.id
                    if answer is not None:
                        text, refs = answer
                        stored = conversation_service.append_message(session, ctx.person,
                            conversation_id=ctx.execution.frozen_context["conversation_id"], role="assistant",
                            content=text, source_ref="run:"+current.id, now=ctx.now)
                        current = repository.transition_run(session, organization_id=current.organization_id,
                            store_id=current.store_id, run_id=current.id, expected_version=current.version,
                            to_state=RunState.COMPLETED)
                        repository.record_answer(session, run=current, message=stored, evidence=refs, own_evidence=self.evidence)
                        executor._audit(session, ctx, "agent_run.worker_finished", "completed")
                    repository.settle_worker_attempt(session, self.claim, step_id=model.id,
                        outcome="finished" if answer is not None else "continue", expected_runtime=repository.SDK_RUNTIME)
                    self._scope(run, epoch)
                    self._commit(session, terminal=answer is not None)
                    # Set only after a normally returned commit. If session
                    # exit raises, no memory fact grants another attempt.
                    self.pending = None
                    self.queued_calls = list(calls or [])
                    self.call = self.queued_calls.pop(0) if self.queued_calls else None
                    self.closed = answer is not None
            if self.pending is not None and denial is not None:
                if not online:
                    executor.record_pi_known_model_after_denial(self.factory, self.machine_token, self.claim,
                        pending=pending, usage=usage, output_ref=output_ref)
                self.pending = None
            if denial is not None:
                raise denial
            return {"committed": True, "finished": self.closed, **({"argument_rejection":feedback} if feedback else {})}
        except BaseException as error:
            self._fail_closed("pi_physical_result_rejected", error=error)
            raise

    def _online_request(self, session, ctx, pending, args):
        from backend.app.modules.agents import runtime_protocol as wire
        wire._pi_provider_request(args)
        if args['attempt_id'] != pending['step_id'] or args['permit'] != pending['permit']:
            reject('attempt_permit_mismatch')
        raw = args['request']['body'].encode('utf-8')
        if sha256(raw).hexdigest() != args['request_digest']:
            reject('pi_provider_request_digest')
        body = wire._decode_json(raw, finite_numbers=True)
        # Adapter-only bounded subset. Provider configuration, tool selection,
        # sampling extensions, routing and arbitrary control fields fail closed.
        allowed = {'model','messages','stream','stream_options','tools','tool_choice',
                   'max_tokens','max_completion_tokens','temperature','top_p'}
        if (set(body)-allowed or body.get('model') != 'deepseek-v4.1-flash'
                or body['model'] != ctx.run.model or body.get('stream') is not True
                or type(body.get('messages')) is not list or not body['messages']):
            reject('pi_provider_request_binding')
        if ('max_tokens' in body) == ('max_completion_tokens' in body):
            reject('pi_provider_output_limit')
        wire.integer(body.get('max_tokens',body.get('max_completion_tokens')),1,pending['budget']['output'])
        if 'stream_options' in body and body['stream_options'] != {'include_usage':True}:
            reject('pi_provider_request_binding')
        if 'tool_choice' in body and body['tool_choice'] != 'auto':
            reject('pi_provider_request_binding')
        from backend.app.modules.agents import capability_discovery as discovery
        active = discovery.active_names(session, ctx)
        by_name = {tool['name']: tool for tool in self.tools}
        expected = [{'type':'function','function':{'name':t['name'],
                    'description':t['description'],'parameters':t['parameters']}} for t in (by_name[name] for name in active)]
        if body.get('tools') != expected:
            reject('pi_provider_tool_binding')
        row = repository.pi_provider_receipt(session,self.claim,pending['step_id'])
        if (row is None or row.input_digest != args['input_digest']
                or row.request_digest != args['request_digest'] or row.model != ctx.run.model
                or row.toolset_version != ctx.run.toolset_version
                or row.manifest_digest != self.runtime_record['source_manifest_sha256']):
            reject('pi_provider_attempt_binding')
        campaign = getattr(self, '_campaign', None)
        if campaign is not None:
            campaign.check_body(args['request']['body'])
            if campaign.mode == 'real-assistant-trial':
                from backend.app.core.model_context import estimate_tokens, PROTOCOL_RESERVE
                # Conservative admission estimate, not exact billed usage.
                if estimate_tokens(args['request']['body']) + PROTOCOL_RESERVE > pending['budget']['input']:
                    reject('agent_context_window_exceeded')
            campaign.check_facts(repository.pi_campaign_facts(session, self.claim))

    @staticmethod
    def _online_accounting(response):
        """Known accounting is independent of decisive-content acceptance."""
        from backend.app.modules.agents import runtime_protocol as wire
        from backend.app.modules.agents.pi_online_response import _bound_response_usage
        wire._pi_provider_response(response)
        if response['status'] != 200:
            reject('pi_provider_status')
        try:
            return _bound_response_usage(response['body'])
        except Exception:
            reject('pi_provider_usage_unknown')

    @staticmethod
    def _online_usage(response):
        return dict(PiRunBridge._online_accounting(response).usage)

    def provider_call(self, *, run, epoch, attempt_id, step_id, permit,
                      input_digest, request_digest, request, on_stream=None):
        self._scope(run,epoch)
        if self.runtime_record['contract_version'] not in (2, 3) or self._offline_transport is None:
            reject('pi_online_transport_unconfigured')
        pending = self.pending
        if pending is None:
            reject('attempt_permit_mismatch')
        args = dict(attempt_id=attempt_id,step_id=step_id,permit=permit,
            input_digest=input_digest,request_digest=request_digest,request=request)
        failure_code = 'pi_provider_call_stopped'
        try:
            with self._transaction('provider_send_intent') as session:
                ctx = self._guard(session,step_id=pending['step_id'],
                    expected_run_version=pending['run_version'],expected_step_version=pending['step_version'])
                if getattr(self, '_campaign', None) is not None and self._campaign.mode in ('real-probe', 'real-readonly-trial', 'real-memory-trial', 'real-assistant-trial'):
                    self._campaign.check_transport(self._offline_transport, bound=True)
                if callable(getattr(self._offline_transport, 'require_run', None)):
                    self._offline_transport.require_run(ctx.run.organization_id, ctx.run.store_id, ctx.run.id)
                self._online_request(session,ctx,pending,args)
                response_binding = response_names = None
                # Research/sales source runs authenticate tool calls against the
                # frozen binding. Workbench runs use the same identity-checked
                # admitted-schemas machinery (its artifact inputs are not, and
                # must not be, part of the ordinary static catalog dict).
                if (getattr(self,'_research_source',None) is not None
                        or getattr(self,'_sales_source',None) is not None
                        or repository.pi_record_tool_profile(self.runtime_record) == tool_registry.PI_WORKBENCH_PROFILE):
                    from . import capability_discovery
                    response_binding = deepcopy(ctx.execution.frozen_context['pi_readonly_binding'])
                    response_names = sorted(capability_discovery.active_names(session,ctx))
                self._require_probe_dispatch_window()
                repository.consume_pi_provider_permit(session,self.claim,step_id=step_id,
                    permit=permit,input_digest=input_digest,request_digest=request_digest,now=ctx.now)
                actor = ctx.person
                self._commit(session)
            # No transport operation may precede the normally returned intent
            # commit/session exit. Lost commit replies never trigger a resend.
            try:
                if getattr(self, '_campaign', None) is not None:
                    self._campaign.validate()
                self._require_probe_dispatch_window()
                control = getattr(self, '_execution_control', None)
                if control is None:
                    response = self._offline_transport.send(request['body'])
                else:
                    try:
                        control.check()
                    except Exception as stopped:
                        from .runtime_execution_control import ExecutionControlError
                        from .pi_online_transport import PiOnlineTransportError
                        if not isinstance(stopped, ExecutionControlError): raise
                        raise PiOnlineTransportError('pi_online_cancelled', send_started=False) from stopped
                    response = self._offline_transport.send(request['body'], cancel_event=control.stop_event, **({'on_stream': on_stream} if on_stream is not None else {}))
            except BaseException as error:
                failure_code = _provider_transport_failure(error)
                self._store_provider_result(pending,actor,error=error)
                reject('pi_provider_transport_unknown')
            probe = getattr(self, '_campaign', None) is not None and self._campaign.mode == 'real-probe'
            real_trial = getattr(self, '_campaign', None) is not None and self._campaign.mode in ('real-readonly-trial', 'real-memory-trial', 'real-assistant-trial')
            if probe or real_trial:
                # Preserve each exact response BEFORE normalization or cost
                # adjudication. Failure still closes the already-sent attempt as
                # unknown; evidence failure is never permission to resend.
                from backend.app.modules.agents.pi_probe_artifacts import record_response
                try:
                    record_response(directory=self._campaign.artifact_directory,
                        organization_id=self.claim.organization_id, store_id=self.claim.store_id,
                        run_id=self.run_id, model_step_id=step_id,
                        request_sha256=request_digest, response=response, mode=self._campaign.mode)
                except BaseException:
                    self._store_provider_result(pending,actor,response=response)
                    reject('pi_probe_artifact_unknown' if probe else 'pi_trial_artifact_unknown')
            from backend.app.modules.agents.pi_online_response import normalize_response, PiOnlineResponseTruncated
            normalized = None
            truncated = False
            try:
                # Workbench rides the source-level admission lattice (WorkbenchPeer
                # is online+memory+discovery+source all true). Artifact tool calls
                # are authenticated against the frozen binding via the
                # admitted-schemas path, never against the static catalog dict.
                _profile = repository.pi_record_tool_profile(self.runtime_record)
                normalized = normalize_response(response['body'], model='deepseek-v4.1-flash',
                    memory=_profile in (tool_registry.PI_MEMORY_PROFILE, tool_registry.PI_DISCOVERY_PROFILE, tool_registry.PI_SOURCE_PROFILE, tool_registry.PI_WORKBENCH_PROFILE),
                    discovery=_profile in (tool_registry.PI_DISCOVERY_PROFILE, tool_registry.PI_SOURCE_PROFILE, tool_registry.PI_WORKBENCH_PROFILE),
                    source=_profile in (tool_registry.PI_SOURCE_PROFILE, tool_registry.PI_WORKBENCH_PROFILE),
                    categories=getattr(self,'_category_source',None) is not None,
                    category_products_contract=getattr(self,'_category_products_contract',None),
                    verified_binding=response_binding, active_names=response_names, recover_invalid_arguments=True)
            except PiOnlineResponseTruncated:
                truncated = True
            except (ValueError, TypeError, KeyError):
                pass
            try:
                observation = self._online_accounting(response)
            except Exception:
                self._store_provider_result(pending,actor,response=response,normalized=normalized)
                # Keep the known HTTP rejection visible after the unknown
                # accounting receipt has committed. Never settle or resend it.
                failure_code = _provider_http_failure(response) or failure_code
                reject(failure_code if failure_code != 'pi_provider_call_stopped'
                       else 'pi_provider_usage_unknown')
            # Billing observations remain durable even if content cannot be
            # authenticated. A failed parser never grants a next action.
            self._store_provider_result(pending,actor,response=response,observation=observation,normalized=normalized)
            if on_stream is not None:on_stream('receipt_committed', {'at_ms':time.time()*1000})
            if probe:
                # A diagnostic is never a business answer or a tool permission.
                # No response returns to Node, so neither native tool dispatch
                # nor a second SDK model stream can start, for text or toolCall.
                reject('pi_probe_response_observed')
            if truncated:
                self._fail_closed('pi_provider_output_truncated')
                reject('pi_provider_output_truncated')
            if normalized is None or not normalized['accounting_complete']:
                reject('pi_online_content_binding')
            pending['response_digest'] = normalized['raw_sha256']
            pending['invalid_arguments'] = normalized.get('invalid_arguments')
            usage = dict(observation.usage)
            if (usage['input']+usage['cacheRead']+usage['cacheWrite'] > pending['budget']['input']
                    or usage['output'] > pending['budget']['output']):
                reject('pi_provider_usage_exceeded')
            return response
        except BaseException as error:
            self._fail_closed(failure_code,error=error)
            raise

    def _require_probe_dispatch_window(self):
        campaign = getattr(self, '_campaign', None)
        if campaign is not None:
            campaign.require_dispatch_window()

    def _store_provider_result(self,pending,actor,*,response=None,observation=None,error=None,normalized=None):
        from backend.app.workers.agent_guard import guard_recovery
        from backend.app.modules.agents import runtime_protocol as wire
        usage = None if observation is None else dict(observation.usage)
        with self._transaction('accounting') as session:
            # Accounting is not a new execution permission. A revoked employee
            # cannot erase a known bill; machine/scope/fence stay revalidated.
            ctx = guard_recovery(session,self.machine_token,organization_id=self.claim.organization_id,
                store_id=self.claim.store_id,run_id=self.run_id,expected_runtime=repository.SDK_RUNTIME)
            row = repository.pi_provider_receipt(session,self.claim,pending['step_id'])
            if row is None or row.state != 'send_intent':
                reject('pi_provider_result_conflict')
            from backend.app.modules.agents.models import AgentStep
            model = session.scalar(select(AgentStep).where(
                AgentStep.organization_id == self.claim.organization_id,
                AgentStep.store_id == self.claim.store_id, AgentStep.run_id == self.run_id,
                AgentStep.id == pending['step_id'], AgentStep.kind == 'model'))
            if model is None:
                reject('pi_provider_result_conflict')
            row.send_started = True
            if error is not None and getattr(error,'send_started',None) is False:
                row.send_started = False
            row.updated_at = ctx.now
            response_digest = None
            if response is not None:
                # Store only safe whitelisted metadata, never body/error text.
                try:
                    wire._pi_provider_response(response)
                    digest = sha256(response['body'].encode()).hexdigest()
                    row.response_digest = digest
                    row.provider_request_id = response['request_id']
                    response_digest = digest
                except Exception:
                    pass
            if normalized is not None:
                if normalized['raw_sha256'] != row.response_digest or (usage is not None and normalized['usage'] != usage):
                    reject('pi_online_content_binding')
                row.message_digest = normalized['message_sha256']
                row.usage_presence = normalized['usage_presence']
            if observation is not None:
                # The accounting fence still verifies the exact lexical body
                # independently. Never reuse evidence from another response.
                if response_digest != observation.response_digest or response['status'] != 200:
                    reject('pi_provider_usage_unknown')
                row.usage_presence = dict(observation.usage_presence)
                basis = observation.usage_basis
                row.usage_basis = None if basis is None else dict(basis)
            if usage is None:
                row.state = 'not_sent' if row.send_started is False else 'unknown'
                row.error_code = 'pi_provider_result_unknown'
                cost_ledger.mark_cost_unknown(session,actor,ctx.run,model,
                    reason='cost_usage_unknown',now=ctx.now,commit=False)
            else:
                row.state = 'responded'
                row.usage_input,row.usage_output = usage['input'],usage['output']
                row.cache_read,row.cache_write = usage['cacheRead'],usage['cacheWrite']
                # Frozen conservative estimate: charge all prompt dimensions at
                # the stored noncached input price. This is not a vendor bill.
                ledger = cost_ledger.reconcile_pi_provider_cost(session,actor,ctx.run,model,
                    receipt_id=row.id,response_digest=row.response_digest,now=ctx.now)
            self._commit(session)
        if usage is not None and (self.closed or ledger.status != 'settled'):
            reject('pi_provider_result_stopped')


    def _complete_confirmed_call(self, call, refs, response, *, run, epoch):
        """Consume one call only after its result/session/readback confirmed.

        This is in-memory sequencing, not an execution grant or a retry path.
        The caller remains fail-closed if scope/control or evidence fails.
        """
        self._scope(run, epoch)
        queue = self.queued_calls
        if self.pending is not None or self.call is not call or type(queue) is not list:
            reject("tool_call_binding_mismatch")
        calls = [call, *queue]
        if any(type(item) is not dict or type(item.get('decision')) is not dict
               or type(item.get('name')) is not str or not item['name']
               or type(item.get('receipt_id')) is not str or not item['receipt_id']
               or type(item['decision'].get('native_call_id')) is not str
               or not item['decision']['native_call_id'] for item in calls):
            reject("tool_call_binding_mismatch")
        identities = [item['decision']['native_call_id'] for item in calls]
        if len(set(identities)) != len(identities):
            reject("tool_call_binding_mismatch")
        evidence = answer_receipts.advance_memory_evidence(self.evidence, refs)
        next_call, remaining = (queue[0], queue[1:]) if queue else (None, [])
        count = self.tool_count + 1
        # All fallible calculations precede updates. These assignments are
        # not a concurrency primitive; outer failure still closes this Run.
        self.evidence, self.tool_count = evidence, count
        self.call, self.queued_calls = next_call, remaining
        return response

    def invoke_read(self, *, run, epoch, call_id, name, version, toolset_version, arguments, selection=None):
        from backend.app.application import commit_guard
        self._scope(run, epoch)
        call = self.call
        if (self.pending is not None or call is None or call_id != call["decision"]["native_call_id"]
                or name != call["name"] or version != call["decision"]["version"]
                or toolset_version != self.toolset_version or digest(arguments) != digest(call["decision"]["arguments"])):
            reject("tool_call_binding_mismatch")
        decision = call["decision"]
        from . import canonical_result
        result_mode = self.runtime_record['launch_profile'] in canonical_result.RESULT_PROFILES
        from backend.app.modules.agents import capability_discovery as discovery
        metadata = name == discovery.NAME
        if not metadata and selection is not None:
            reject("tool_call_binding_mismatch")
        try:
            with self._transaction('tool_dispatch_intent') as session:
                ctx = self._guard(session)
                self._renew_discovery_lease(session)
                # Re-translation uses the actual same native ID and current
                # exact finite subset, not a globally resolved alias.
                current = executor.pi_readonly_decision(session, ctx, name=name, version=version,
                    native_call_id=call_id, arguments=arguments)
                if current != decision:
                    reject("tool_call_binding_mismatch")
                if metadata:
                    discovery.selection_result(session, ctx, arguments, selection)
                repository.require_pi_tool_receipt(session, self.claim, pi_name=name, decision=decision, state="accepted")
                if (repository.pi_record_tool_profile(self.runtime_record) == tool_registry.PI_WORKBENCH_PROFILE
                        and type(decision["tool_key"]) is str and type(version) is str
                        and (decision["tool_key"], version) in tool_registry.WORKSPACE_TOOLS):
                    # Workspace artifact pair inside the verified workbench
                    # binding's frozen selection: resolve the fixed registry
                    # definition; resolve_tool keeps rejecting these keys.
                    definition = tool_registry.workspace_definition(decision["tool_key"], version)
                else:
                    definition = tool_registry.resolve_tool(decision["tool_key"], version, store_id=self.claim.store_id)
                executor._remaining_budget(ctx.run, repository.list_steps(session, ctx.run.organization_id,
                    ctx.run.store_id, ctx.run.id), phase="tool")
                step, run_version = executor._open_tool_step(session, ctx, self.claim, ctx.run,
                    decision, definition, expected_runtime=repository.SDK_RUNTIME)
                repository.bind_pi_tool_intent(session, self.claim, pi_name=name, decision=decision, step=step, now=ctx.now)
                sid, step_version = step.id, step.version
                self._commit(session)
            # The consumed intent cannot replay. Remaining calls retain their
            # own accepted receipts; any subsequent failure closes them.
            # Product K2: an explicit Host profile branch (never a model
            # argument and never "a projection happens to exist") runs the
            # frozen A-close / B-worker / C-root phases for knowledge.search.
            # The legacy non-Pi tool keeps its original contract in registry.
            projection_prepared = projection_worker = projection_deadline = None
            projection_stop = None
            projection_profile = None
            if definition.key == 'knowledge.search' and version == '1':
                projection_profile = repository.pi_record_tool_profile(self.runtime_record)
                if projection_profile in tool_registry.PI_ALL_PROFILES:
                    from backend.app.modules.knowledge import p1c as knowledge_p1c
                    projection_input = definition.input_model.model_validate(arguments)
                    control = getattr(self, '_execution_control', None)
                    projection_stop = getattr(control, 'stop_event', None)
                    intent_started = time.monotonic()
                    with self.factory() as preflight:
                        pctx = self._guard(preflight, step_id=sid,
                                           expected_run_version=run_version,
                                           expected_step_version=step_version)
                        executor._check_tool_scope(preflight, pctx, decision, definition)
                        repository.require_pi_tool_receipt(preflight, self.claim, pi_name=name,
                            decision=decision, state='dispatch_intent', tool_step_id=sid)
                        projection_now = repository._db_clock(preflight)
                        projection_actor = pctx.person
                        run_remaining = (pctx.run.deadline_at - projection_now).total_seconds()
                    projection_deadline = intent_started + min(3.0, max(0.0, run_remaining))
                    snapshot_error = None
                    try:
                        projection_prepared = knowledge_p1c.prepare_search(
                            self.factory, projection_actor, query=projection_input.query,
                            now=projection_now, deadline=projection_deadline,
                            cancel_event=projection_stop)
                        projection_worker = knowledge_p1c.score_prepared(
                            projection_prepared, deadline=projection_deadline,
                            cancel_event=projection_stop, limit=projection_input.limit)
                    except BaseException as error:
                        snapshot_error = error
                        raise
                    finally:
                        # B is the only file-path consumer. C uses the frozen
                        # in-memory rows and rechecks the current DB projection.
                        if projection_prepared is not None:
                            try:
                                knowledge_p1c.cleanup(projection_prepared)
                            except OSError:
                                code = 'pi_knowledge_snapshot_cleanup_unconfirmed'
                                if snapshot_error is None:
                                    raise BridgeError(code) from None
                                snapshot_error.add_note(code)
            from backend.app.modules.integrations.guanjia import observation_receipts
            source_process = ((definition.key, version) in tool_registry.GUANJIA_SOURCE_TOOLS
                and repository.pi_record_tool_profile(self.runtime_record) in (
                    tool_registry.PI_SOURCE_PROFILE, tool_registry.PI_WORKBENCH_PROFILE))
            if (definition.key, version) in tool_registry.GUANJIA_SALES_TOOLS:
                from backend.app.modules.integrations.guanjia import sales_ranking_process
                selected_sales = getattr(self, '_sales_source', None)
                if selected_sales is None: reject('pi_sales_source_changed')
                outcome = sales_ranking_process.execute(selected_sales, self.claim, sid,
                    run_version, step_version, decision, name)
                if outcome is None: reject('guanjia_sales_ranking_unavailable')
                self._scope(run, epoch)
                return self._complete_confirmed_call(call, outcome['refs'], outcome['response'],
                                                     run=run, epoch=epoch)
            if (definition.key,version) in tool_registry.RESEARCH_TOOLS:
                from . import research_process
                selected_research=getattr(self,'_research_source',None)
                if selected_research is None:reject('pi_research_source_changed')
                outcome=research_process.execute(selected_research,self.claim,sid,run_version,step_version,decision,name)
                if outcome is None:reject('research_process_unavailable')
                self._scope(run, epoch)
                return self._complete_confirmed_call(call, outcome['refs'], outcome['response'],
                                                     run=run, epoch=epoch)
            category = (definition.key, version) in tool_registry.GUANJIA_CATEGORY_TOOLS
            if category:
                from backend.app.modules.integrations.guanjia import category_process
                # Intent is committed and its Session is closed. The dedicated
                # process owns the entire original result root and readback.
                selected_category = getattr(self, '_category_source', None)
                if selected_category is None:
                    reject('pi_category_source_changed')
                outcome = category_process.execute(selected_category, self.claim, sid,
                    run_version, step_version, decision, name)
                if outcome is None:
                    reject('guanjia_category_unavailable')
                self._scope(run, epoch)
                return self._complete_confirmed_call(call, outcome['refs'], outcome['response'],
                                                     run=run, epoch=epoch)
            source_call = source_result = None
            started = time.monotonic()
            if source_process:
                from backend.app.modules.integrations.guanjia import work_source, work_source_process
                # This transaction must CLOSE before child authority checks.
                # The prior committed intent is already consumed even if this
                # preflight/exec/result transaction subsequently fails.
                with self._transaction('source_preflight') as preflight:
                    before = self._guard(preflight, step_id=sid, expected_run_version=run_version,
                                         expected_step_version=step_version)
                    executor._check_tool_scope(preflight, before, decision, definition)
                    repository.require_pi_tool_receipt(preflight, self.claim, pi_name=name,
                        decision=decision, state='dispatch_intent', tool_step_id=sid)
                    selected = observation_receipts.select_source(preflight, actor=before.person, agent=before.agent,
                        tool_key=definition.key, arguments=arguments)
                    source_call = work_source.SourceCall(self.claim, sid, run_version, step_version,
                        call_id, definition.key, toolset_version, selected)
                source_result = work_source_process.execute(source_call)
                if source_result.code != 'prepared' or source_result.prepared is None:
                    reject('guanjia_source_process_unavailable')
            with self._transaction('tool_result', required_seconds=(definition.timeout_seconds if not source_process and not metadata else 0)) as session:
                if projection_prepared is not None:
                    # C: the original root transaction starts on a fresh
                    # connection with a budgeted BEGIN IMMEDIATE before any
                    # permission/business read, and never upgrades A's data.
                    from backend.app.modules.knowledge import p1c as knowledge_p1c
                    knowledge_p1c.begin_read(session, deadline=projection_deadline)
                    session.info['p1c_prepared'] = projection_prepared
                    session.info['p1c_worker'] = projection_worker
                    session.info['p1c_deadline_monotonic'] = projection_deadline
                    session.info['p1c_stop_event'] = projection_stop
                    session.info['knowledge_projection_profile'] = projection_profile
                ctx = self._guard(session, step_id=sid, expected_run_version=run_version,
                                  expected_step_version=step_version)
                executor._check_tool_scope(session, ctx, decision, definition)
                repository.require_pi_tool_receipt(session, self.claim, pi_name=name,
                    decision=decision, state="dispatch_intent", tool_step_id=sid)
                if not source_process:
                    started = time.monotonic()  # retain ordinary-tool timing
                observation = observation_receipts.handles_tool(definition.key)
                observation_selection = (observation_receipts.select_source(session, actor=ctx.person, agent=ctx.agent,
                    tool_key=definition.key, arguments=arguments) if observation else None)
                if source_process:
                    if observation_selection != source_call.selection:
                        reject('guanjia_source_changed')
                    # Fixed source profile/definition and exact native intent
                    # were revalidated by _guard above; never call a handler
                    # override or the direct model gateway for this output.
                    result = definition.output_model.model_validate(source_result.prepared.result()).model_dump(mode='json')
                    tool_registry._audit(session, ctx.person, action=definition.audit_action,
                        tool_key=definition.key, version=version, code='ok')
                elif metadata:
                    result = discovery.selection_result(session, ctx, arguments, selection)
                else:
                    result = tool_registry.invoke_tool(session, requester=ctx.person, agent=ctx.agent,
                        tool_key=definition.key, version=version, arguments=arguments,
                        idempotency_key=(executor._pi_call_identity(session, ctx, decision)[0]
                                         if definition.idempotency == "required" else None),
                        **({'result_abi': canonical_result.RESULT_ABI} if result_mode else {}))
                no_effect = result_mode and type(result) is tool_registry.NoEffectToolResult
                result_kind = 'tool_error' if no_effect else 'success'
                if no_effect:
                    result = result.payload
                self._scope(run, epoch)
                now = repository._db_clock(session)
                control = repository.require_worker_claim(session, self.claim, expected_runtime=repository.SDK_RUNTIME)
                if (time.monotonic()-started > definition.timeout_seconds or control.cancel_requested_at is not None
                        or control.lease_until is None or control.lease_until <= now or ctx.run.deadline_at <= now):
                    reject("tool_dispatch_expired")
                refs = [] if metadata or observation or category or no_effect else answer_receipts.capture_tool_evidence(session, ctx.person,
                    tool_key=definition.key, permission=definition.requester_permission, result=result)
                step = repository.transition_step(session, organization_id=ctx.run.organization_id,
                    store_id=ctx.run.store_id, run_id=ctx.run.id, step_id=sid,
                    expected_run_version=ctx.run.version, expected_step_version=ctx.step.version,
                    change=StepTransition(to_state=StepState.SUCCEEDED, output_ref="sha256:"+digest(result)), now=now)
                receipt = repository.succeed_pi_tool_receipt(session, self.claim, pi_name=name,
                    decision=decision, step=step, now=now)
                if observation:
                    reference = observation_receipts.stage_tool_result(session, actor=ctx.person, agent=ctx.agent,
                        claim=self.claim, step_id=sid, pi_receipt_id=receipt.id, tool_key=definition.key,
                        arguments=arguments, result=result, selection=observation_selection)
                    refs = [{'kind': 'permission', 'key': definition.requester_permission}, reference]
                canonical = None
                if result_mode:
                    canonical = repository.record_pi_tool_result(session, self.claim, receipt_id=receipt.id,
                        step_id=sid, pi_name=name, decision=decision, runtime_record=self.runtime_record,
                        payload=result, source_refs=refs, result_kind=result_kind, now=now)
                from . import public_trace_capture
                public_trace_capture.capture(session, ctx, self.claim, receipt,
                    decision, phase='result', result=result)
                if metadata:
                    discovery.append_event(ctx, decision=decision, receipt=receipt, step=step, result=result)
                executor._append_frozen_evidence(session, ctx, refs, supersede_memory=True)
                repository.settle_worker_attempt(session, self.claim, step_id=sid,
                    outcome="continue", expected_runtime=repository.SDK_RUNTIME)
                executor._audit(session, ctx, "agent_tool.preflight_rejected" if no_effect else "agent_tool.worker_invoked",
                    'memory_query_invalid' if no_effect else definition.key)
                def final_pin(active_session, instant):
                    # Existing 027 verifier is pure reads/no_autoflush, not the
                    # locking execution guard or flush-capable Skill verifier.
                    tool_registry.verify_pi_binding(active_session, ctx.person, ctx.agent,
                        run_id=ctx.run.id, binding=ctx.execution.frozen_context["pi_readonly_binding"],
                        expected_digest=ctx.run.toolset_version, now=instant)
                    discovery.active_names(active_session, ctx)
                    if canonical is not None:
                        pinned = repository.require_pi_tool_result(active_session, self.claim,
                            receipt_id=receipt.id, step_id=sid, pi_name=name, decision=decision,
                            runtime_record=self.runtime_record, expected_ref=canonical.canonical_ref, check_at=instant)
                        if canonical_result.load_envelope(pinned.canonical_json)['source_refs'] != refs:
                            reject('pi_canonical_refs_changed')
                commit_guard.enlist(session, action="pi_readonly_result", check=final_pin)
                response = {"call_id": call_id, "name": name, "version": version,
                    "toolset_version": toolset_version, "receipt_id": receipt.id, "step_id": sid,
                    "output_ref": receipt.output_ref, "result": result}
                if canonical is not None:
                    response['canonical_ref'] = canonical.canonical_ref
                    response['result'] = canonical_result.load_envelope(canonical.canonical_json,
                        expected_ref=canonical.canonical_ref)
                self._commit(session)
            if observation:
                # Never return the handler's in-transaction object. A failure
                # here closes this Run; the committed native call cannot replay.
                with self._transaction('observation_readback') as current:
                    bind = current.get_bind()
                    committed = observation_receipts.read(bind.engine, ctx.person, reference)
                if committed is None or 'sha256:' + digest(committed) != response['output_ref']:
                    reject('guanjia_observation_unavailable')
                response['result'] = committed
            return self._complete_confirmed_call(call, refs, response, run=run, epoch=epoch)
        except BaseException as error:
            self._fail_closed("pi_tool_response_failed", error=error)
            raise

    @property
    def close_attempts(self):
        return json.loads(json.dumps(self._close_attempts))

    @property
    def first_close_status(self):
        return self._first_close_status

    def _fail_closed(self, code, *, error=None):
        self.closed = True
        if self._close_attempts:
            return self._first_close_status
        failures = []
        control = getattr(self, '_execution_control', None)
        with (nullcontext() if control is None else control.recovery_transaction()):
            status = executor._close_sdk_bound_attempt(self.factory, self.machine_token, self.claim,
                                                      code=code, errors=failures)
        self._close_errors.append({"operation": error, "recovery": tuple(failures)})
        self._close_attempts.append({"index": 1, "code": code, "status": status,
            "operation_error": executor._sdk_exception_detail(error) if error is not None else None,
            "recovery_errors": [executor._sdk_exception_detail(item) for item in failures]})
        self._first_close_status = self.close_status = status
        return status

    def abandon_unknown(self):
        return self._fail_closed("pi_attempt_response_unknown")
