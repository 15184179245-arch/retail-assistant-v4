"""Thin, start-only SDK supervisor. No HTTP registration, DB migration or old loop.

The caller supplies a trusted new FreshRunBridge and exact, allowlisted launch.
Wire data can never select a process, module, credential, scope or permission.
"""
from __future__ import annotations
import os
import json
import time
import secrets
import selectors
import queue as event_queue
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from backend.app.modules.agents import runtime_protocol as wire
from backend.app.workers.agent_executor import _sdk_exception_detail
from backend.app.modules.agents.runtime_bridge import FreshRunBridge, PiRunBridge
from backend.app.modules.agents.repository import AgentClaim, SDK_RUNTIME

@dataclass(frozen=True)
class VerifiedSdk:
 package:str
 entry:str
 package_sha256:str
 entry_sha256:str
def verify_sdk(*,package,entry,package_sha256,entry_sha256):
 """Host verification before spawn; expected hashes come from fixed launch inputs."""
 p=Path(package).read_bytes();e=Path(entry).read_bytes()
 if sha256(p).hexdigest()!=package_sha256 or sha256(e).hexdigest()!=entry_sha256:wire.fail('sdk_source')
 metadata=json.loads(p)
 if metadata.get('name')!=wire.SDK['engine'] or metadata.get('version')!=wire.SDK['version']:wire.fail('sdk_identity')
 return VerifiedSdk(str(package),str(entry),package_sha256,entry_sha256)

def admit_bridge(bridge):
 """Pure internal admission, not durable authorization or a recovery grant."""
 if type(bridge)is not FreshRunBridge:wire.fail('invalid_bridge')
 claim=getattr(bridge,'claim',None)
 if type(claim)is not AgentClaim or bridge.run_id!=claim.run_id or claim.runtime_identity!=SDK_RUNTIME:wire.fail('binding_scope_mismatch')
 if type(claim.epoch)is not int or claim.epoch<1:wire.fail('binding_scope_mismatch')
 if bridge.closed or bridge.close_attempts:wire.fail('binding_closed')

def admit_pi_bridge(bridge):
 if type(bridge)is not PiRunBridge:wire.fail('invalid_bridge')
 version=bridge.runtime_record.get('contract_version')
 if version not in (1,2,3):wire.fail('pi_online_session_held')
 claim=getattr(bridge,'claim',None)
 if type(claim)is not AgentClaim or bridge.run_id!=claim.run_id or claim.runtime_identity!=SDK_RUNTIME:wire.fail('binding_scope_mismatch')
 if type(claim.epoch)is not int or claim.epoch<1:wire.fail('binding_scope_mismatch')
 if bridge.closed or bridge.close_attempts:wire.fail('binding_closed')
 if version in (2,3):
  from backend.app.modules.agents.pi_online_transport import PiOnlineTransport
  if type(bridge._offline_transport)is not PiOnlineTransport:wire.fail('pi_online_session_held')
  bridge._offline_transport.require_run(bridge.claim.organization_id,bridge.claim.store_id,bridge.run_id)


def run_session(*,bridge,argv,cwd,env,verified_sdk,prompt,publish=lambda result:None,observe=lambda event,data:None):
 # The old public entry remains experiment-only, including test consumers.
 return _run_owned_session(bridge=bridge,argv=argv,cwd=cwd,env=env,verified_sdk=verified_sdk,
                          prompt=prompt,publish=publish,observe=observe,formal=False)


def run_pi_session(*,bridge,argv,cwd,env,verified_sdk,prompt,publish=lambda result:None,observe=lambda event,data:None,execution_control=False):
 return _run_owned_session(bridge=bridge,argv=argv,cwd=cwd,env=env,verified_sdk=verified_sdk,
                          prompt=prompt,publish=publish,observe=observe,formal=True,execution_control=execution_control)


def _run_owned_session(*,bridge,argv,cwd,env,verified_sdk,prompt,publish,observe,formal,execution_control=False):
 """Run exactly one owned child. All timeouts below belong to this reader/writer.

Only safe error metadata is published; caller diagnostics stay Host-internal.
Successful return is a transport outcome, never permission to synthesize receipt.
"""
 if type(verified_sdk)is not VerifiedSdk:wire.fail('unverified_sdk')
 if any(type(x)is not str or not x for x in (verified_sdk.package,verified_sdk.entry)):wire.fail('unverified_sdk')
 if any(type(x)is not str or len(x)!=64 or any(c not in '0123456789abcdef'for c in x)for x in (verified_sdk.package_sha256,verified_sdk.entry_sha256)):wire.fail('unverified_sdk')
 (admit_pi_bridge if formal else admit_bridge)(bridge)
 # From here the supervisor owns all failures; no observer acknowledgement is
 # required and the caller must not run a second disposition around this call.
 ownership_accepted=True;spawn_started=False;identity=None;peer=None;framer=None
 proc=None;selector=None;primary=None;stage='preflight';terminated=False;cleanup_errors=[];control=None;control_finished=False
 started=time.monotonic();queue=bytearray();write_started=None;stderr=bytearray();stdout_bytes=0;stdout_eof=False;stderr_eof=False;end=False
 streaming=formal and bridge.runtime_record['contract_version']==3
 online=formal and bridge.runtime_record['contract_version'] in (2,3)
 source=formal and bridge.runtime_record['wire_protocol'].startswith('v4.pi-source/')
 discovery=formal and (source or bridge.runtime_record['wire_protocol'].startswith('v4.pi-discovery/'))
 session_seconds=wire.STREAM_SESSION_SECONDS if streaming else wire.DISCOVERY_SESSION_SECONDS if discovery else wire.SESSION_SECONDS
 memory=formal and bridge.runtime_record['wire_protocol'].startswith('v4.pi-memory/')
 pi_peer=(wire.PiMemoryOnlinePeer if online else wire.PiMemoryPeer) if memory else (wire.PiOnlinePeer if online else wire.PiPeer)
 if discovery:pi_peer=wire.PiDiscoveryOnlinePeer if online else wire.PiDiscoveryPeer
 if source:pi_peer=wire.PiSourceOnlinePeer if online else wire.PiSourcePeer
 pi_hello=(wire.PI_MEMORY_ONLINE_PROFILE if online else wire.PI_MEMORY_PROFILE) if memory else (wire.PI_ONLINE_PROFILE if online else wire.PI_PROFILE)
 if discovery:pi_hello=wire.PI_DISCOVERY_ONLINE_PROFILE if online else wire.PI_DISCOVERY_PROFILE
 if source:pi_hello=wire.PI_SOURCE_ONLINE_PROFILE if online else wire.PI_SOURCE_PROFILE
 if streaming:pi_peer=wire.PiStreamPeer;pi_hello=wire.PI_STREAM_PROFILE
 if formal and bridge.runtime_record['launch_profile'] in wire.PI_RESULT_LAUNCH_PROFILES:
  pi_peer=wire.PiCanonicalPeer;pi_hello=wire.PI_RESULT_PROFILE
 report={'protocol':pi_peer.protocol if formal else wire.PROTOCOL,'identity':identity,'transport_ok':False,'exit':None,'terminated':False,'disposition':None,'disposition_origin':None,'primary':None,'termination_error':None,'stdout_bytes':0,'stderr_bytes':0,'received_frames':0,'sent_frames':0}
 owner=None;operation=None;op_meta=None;stream_event=None;pending_failure=None
 stream_events=event_queue.Queue(maxsize=2);outbound=deque();receipt_notices=[]
 def enqueue(kind,body,reply=None):
  nonlocal write_started
  if queue:
   if not streaming:wire.fail('write_backpressure')
   if len(outbound)>=4:wire.fail('write_backpressure')
   outbound.append((kind,body,reply));return
  queue.extend(peer.send(kind,body,reply));write_started=time.monotonic()
 def terminate():
  if proc is None:return not spawn_started
  if proc.poll() is None:proc.kill()
  proc.wait(timeout=3);return True
 def finish_control():
  nonlocal control_finished
  if control is None or control_finished:return
  control_finished=True
  try:control.finish()
  except BaseException as error:cleanup_errors.append(('execution_control',error))
 def dispatch(op,args,seq):
  scope={'run':bridge.run_id,'epoch':bridge.claim.epoch}
  if op=='start':result=bridge.start_payload(prompt)
  elif op=='model_begin':result=bridge.begin_model(**scope,request_id='wire-'+str(seq),input_digest=args['input_digest'],purpose=args['purpose'],toolset_version=args['toolset_version'],request_digest=args['request_digest'])
  elif op=='provider_call':
   from .pi_online_transport import TIMEOUT_SECONDS,PiOnlineTransportError
   provider_deadline=time.monotonic()+TIMEOUT_SECONDS
   def emit(kind,value):
    while True:
     if time.monotonic()>=provider_deadline:raise PiOnlineTransportError('pi_online_timeout',send_started=True)
     control.handle.check()
     try:stream_events.put((kind,value),timeout=.1);return
     except event_queue.Full:continue
   result=bridge.provider_call(**scope,**args,on_stream=emit)
   result={'status':result['status'],'request_id':result['request_id'],'body_sha256':sha256(result['body'].encode()).hexdigest(),'byte_count':len(result['body'].encode()),'usage':bridge._online_usage(result),'response_recorded':True}
  elif op=='model_complete':
   if bridge.pending is None or args['step_id']!=bridge.pending['step_id']:wire.fail('attempt_step')
   result=bridge.complete_model(**scope,**args)
  elif op=='tool_request':result=bridge.invoke_read(**scope,**args)
  else:wire.fail('unsupported_operation')
  return result,bridge.closed,time.time()*1000
 def failure_send_started(error):
  from .pi_online_transport import PiOnlineTransportError
  seen=set()
  while error is not None and id(error) not in seen:
   seen.add(id(error))
   if type(error)is PiOnlineTransportError:return error.send_started
   error=error.__cause__ or error.__context__
  return True
 def stop_owner():
  nonlocal owner
  if owner is not None:
   # Waiting is intentional: a synchronous handler cannot be killed by a
   # Python timeout. No disposition or new owner is admitted before op exit.
   if control is not None:control.handle.stop_event.set()
   try:owner.shutdown(wait=True,cancel_futures=True)
   except BaseException as error:cleanup_errors.append(('operation_owner',error))
   owner=None
 def collect_receipt_notices():
  nonlocal stream_event
  # Consume actual private events before any observation attempt. No wire
  # sequence/credit is rolled back and no notification is invented from DB.
  current=stream_event;stream_event=None
  while True:
   if current is not None and current[0]=='receipt_committed':receipt_notices.append(current[1])
   try:current=stream_events.get_nowait()
   except event_queue.Empty:break
 def observe_receipt_notices():
  collect_receipt_notices()
  while receipt_notices:
   value=receipt_notices.pop(0)
   try:observe('provider_receipt_committed',value)
   except BaseException as error:cleanup_errors.append(('provider_receipt_observer',error))
 try:
  if streaming:execution_control=True
  if type(execution_control)is not bool:wire.fail('execution_control_invalid')
  if execution_control:
   if not formal:wire.fail('execution_control_invalid')
   from .runtime_execution_control import ExecutionControl
   from .pi_online_transport import PiOnlineTransport
   if online and type(bridge._offline_transport)is not PiOnlineTransport:wire.fail('execution_control_transport_unsupported')
   control=ExecutionControl(session_factory=bridge.factory,machine_token=bridge.machine_token,
       claim=bridge.claim,deadline=started+session_seconds)
   bridge._execution_control=control.start()
  verify_sdk(package=verified_sdk.package,entry=verified_sdk.entry,package_sha256=verified_sdk.package_sha256,entry_sha256=verified_sdk.entry_sha256)
  identity={'nonce':secrets.token_hex(16),'run_id':bridge.run_id,'epoch':str(bridge.claim.epoch)}
  report['identity']=identity;peer=(pi_peer if formal else wire.Peer)('host',identity);framer=wire.Framer(max_frames=wire.STREAM_FRAMES if streaming else wire.MAX_FRAMES)
  if time.monotonic()-started>=session_seconds:wire.fail('session_deadline')
  stage='launch';spawn_started=True;handshake_started=time.monotonic()
  proc=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
  observe('child_started',{'pid':proc.pid,'argv':argv})
  for pipe in (proc.stdin,proc.stdout,proc.stderr):os.set_blocking(pipe.fileno(),False)
  selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ,'stdout');selector.register(proc.stderr,selectors.EVENT_READ,'stderr')
  if streaming:owner=ThreadPoolExecutor(max_workers=1,thread_name_prefix='pi-operation-owner')
  enqueue('hello',{'sdk':wire.SDK,'profile':pi_hello if formal else 'tasks.list_mine@1'});stage='stream'
  while True:
   at=time.monotonic();elapsed=at-started
   # A successful owner can still be waiting for credit after a client abort.
   # It has exited its DB/physical work, so stop must not wait for that credit.
   if control is not None and (operation is None or (operation.done() and not operation.cancelled() and operation.exception() is None)) and pending_failure is None and not bridge.closed:control.handle.check()
   if elapsed>=session_seconds:wire.fail('session_deadline')
   if peer.phase in ('new','hello') and at-handshake_started>=wire.HANDSHAKE_SECONDS:wire.fail('handshake_deadline')
   if not stdout_eof:framer.check(at)
   if queue and at-write_started>=wire.PARTIAL_SECONDS:wire.fail('write_deadline')
   if streaming:
    if operation is not None and operation.done() and operation.exception() is not None:
     # No unsent byte owns a wire sequence. Once the operation is actually
     # stopped, cancellation must not wait for credit the peer will never grant.
     collect_receipt_notices()
    if not queue and outbound:
     kind,body,reply=outbound.popleft();enqueue(kind,body,reply)
    if pending_failure is not None and not queue and not outbound:raise pending_failure
    if stream_event is None:
     try:stream_event=stream_events.get_nowait()
     except event_queue.Empty:pass
    if stream_event is not None and not queue and not outbound:
     event,value=stream_event
     if event=='stream_open':
      enqueue('provider_open',{'stream_id':op_meta['args']['attempt_id'],'attempt_id':op_meta['args']['attempt_id'],'step_id':op_meta['args']['step_id'],'request_digest':op_meta['args']['request_digest'],**value},op_meta['seq']);stream_event=None
     elif event=='receipt_committed':
      stream_event=None
      observe('provider_receipt_committed',value)
     elif event=='stream_chunk' and peer.stream_state.available>=len(value):
      enqueue('provider_chunk',{'stream_id':op_meta['args']['attempt_id'],'chunk_seq':len(peer.stream_state.sizes)+1,'data_b64':base64.b64encode(value).decode()},op_meta['seq']);stream_event=None
    if operation is not None and operation.done() and stream_event is None and stream_events.empty() and not queue and not outbound:
     if op_meta['op']!='provider_call' or operation.exception() is not None or peer.stream_state.consumed==peer.stream_state.total:
      try:
       result,closed,committed_at=operation.result()
      except BaseException as error:
       if op_meta['op']=='provider_call':
        enqueue('provider_abort',{'stream_id':op_meta['args']['attempt_id'],'code':'cancelled' if control.handle.stop_event.is_set() else 'transport_invalid','send_started':failure_send_started(error)},op_meta['seq'])
        enqueue('response',{'op':'provider_call','ok':False,'result':None,'code':'host_operation_failed'},op_meta['seq']);pending_failure=error
       else:raise
      else:
       if closed:finish_control()
       if op_meta['op']=='start':enqueue('start',result)
       else:
        if op_meta['op']=='provider_call':enqueue('provider_end',{'stream_id':op_meta['args']['attempt_id'],'last_chunk_seq':len(peer.stream_state.sizes),'byte_count':result['byte_count'],'body_sha256':result['body_sha256']},op_meta['seq'])
        observe('host_operation',{'op':op_meta['op'],'seq':op_meta['seq'],'at_ms':committed_at})
        enqueue('response',{'op':op_meta['op'],'ok':True,'result':result,'code':None},op_meta['seq'])
      operation=None;op_meta=None
   try:selector.get_key(proc.stdin)
   except KeyError:
    if queue:selector.register(proc.stdin,selectors.EVENT_WRITE,'stdin')
   else:
    if not queue:selector.unregister(proc.stdin)
   for key,_ in selector.select(.02):
    if key.data=='stdin':
     try:count=os.write(proc.stdin.fileno(),queue)
     except BlockingIOError:continue
     if count<=0:wire.fail('write_closed')
     del queue[:count]
     continue
    try:data=os.read(key.fileobj.fileno(),8192)
    except BlockingIOError:continue
    if not data:
     selector.unregister(key.fileobj)
     if key.data=='stdout':framer.eof();stdout_eof=True
     else:stderr_eof=True
     continue
    observe('stdio_chunk',{'stream':key.data,'data':data})
    if key.data=='stderr':
     if len(stderr)+len(data)>wire.STDERR_BYTES:wire.fail('stderr_bytes')
     stderr.extend(data);continue
    stdout_bytes+=len(data)
    for frame in framer.feed(data):
     if time.monotonic()-started>=session_seconds:wire.fail('session_deadline')
     if queue and not streaming:wire.fail('peer_pipelining')
     peer.receive(frame);observe('frame_received',{'type':frame['type'],'seq':frame['seq']})
     kind=frame['type'];body=frame['body']
     if streaming:
      if kind in ('provider_credit','provider_cancel'):
       if kind=='provider_cancel' and peer.cancel_classification=='active':control.handle.stop_event.set()
       continue
      if kind=='ready' or kind=='request':
       if operation is not None:wire.fail('peer_pipelining')
       op_meta={'op':'start' if kind=='ready' else body['op'],'args':{} if kind=='ready' else body['arguments'],'seq':frame['seq']}
       operation=owner.submit(dispatch,op_meta['op'],op_meta['args'],op_meta['seq']);continue
     if kind=='ready':enqueue('start',bridge.start_payload(prompt) if formal else {'tool_name':'tasks.list_mine@1','input_schema':bridge.schema,'prompt':prompt})
     elif kind=='request':
      op=body['op'];args=body['arguments'];scope={'run':bridge.run_id,'epoch':bridge.claim.epoch}
      # Reserved identity/op fields are not merged from peer arguments.
      if op=='model_begin':
       result=bridge.begin_model(**scope,request_id='wire-'+str(frame['seq']),input_digest=args['input_digest'],
          **({'purpose':args['purpose'],'toolset_version':args['toolset_version'], **({'request_digest':args['request_digest']} if online else {})} if formal else {}))
      elif op=='provider_call' and online:
       result=bridge.provider_call(**scope,**args)
      elif op=='model_complete':
       if bridge.pending is None or args['step_id']!=bridge.pending['step_id']:wire.fail('attempt_step')
       result=bridge.complete_model(**scope,permit=args['permit'],message=args['message'],
          **({'attempt_id':args['attempt_id'],'step_id':args['step_id']} if formal else {}))
      elif op=='tool_request':
       result=bridge.invoke_read(**scope,call_id=args['call_id'],arguments=args['arguments'],
          **({'name':args['name'],'version':args['version'],'toolset_version':args['toolset_version'], **({'selection':args['selection']} if discovery and args['name']=='tool_search' else {})} if formal else {}))
      else:wire.fail('unsupported_operation')
      # Bridge operations remain on this single owner thread. A completed
      # terminal transaction wins over a heartbeat observing its finished row.
      if control is not None and bridge.closed:finish_control()
      observe('host_operation',{'op':op,'seq':frame['seq']})
      enqueue('response',{'op':op,'ok':True,'result':result,'code':None},frame['seq'])
     elif kind=='session_end':
      if body['status']!='ok':wire.fail('sdk_failed')
      end=True
     else:wire.fail('sdk_failed')
   if proc.poll() is not None and stdout_eof and stderr_eof:break
   if stdout_eof and not end:wire.fail('unexpected_eof')
  stage='terminal_validation';report['exit']=proc.wait(timeout=1);terminated=True
  if report['exit']!=0:wire.fail('child_exit')
  if not end or peer.phase!='ended' or queue:wire.fail('incomplete_session')
  if not bridge.closed or bridge.pending is not None or bridge.call is not None or bridge.close_attempts:wire.fail('host_not_completed')
  report['transport_ok']=True
 except BaseException as error:
  stop_owner()
  if streaming:observe_receipt_notices()
  finish_control()
  primary=error;bridge.closed=True
  try:terminated=terminate()
  except BaseException as termination_error:report['termination_error']=_sdk_exception_detail(termination_error)
  report['primary']={'stage':stage,**_sdk_exception_detail(error),'code':error.code if isinstance(error,wire.ProtocolError) else None}
  if not terminated:report['disposition']='unresolved';report['disposition_origin']='CHILD_TERMINATION_UNCONFIRMED'
  elif bridge.close_attempts:report['disposition']=bridge.close_attempts[0]['status'];report['disposition_origin']='PRESERVED_FIRST_HOST_ATTEMPT'
  else:
   report['disposition_origin']='FIRST_TRANSPORT_DISPOSITION'
   try:report['disposition']=bridge.abandon_unknown()
   except BaseException as recovery_error:
    report['disposition']='unresolved';report['recovery_error']=_sdk_exception_detail(recovery_error)
 finally:
  if streaming and peer is not None:peer.clear_stream_anchor()
  stop_owner()
  if control is not None:
   finish_control();report['execution_control']=control.report
  # Each owned resource gets one attempt even when a prior close succeeded then
  # raised. Cleanup has no authority to repeat disposition or rewrite DB facts.
  def close_resource(name,resource):
   if resource is None:return
   try:resource.close()
   except BaseException as error:cleanup_errors.append((name,error))
  close_resource('selector',selector)
  if proc is not None:
   try:report['exit']=proc.poll()
   except BaseException as error:cleanup_errors.append(('child_status',error))
   for name in ('stdin','stdout','stderr'):close_resource(name,getattr(proc,name,None))
  report.update(terminated=terminated,ownership_accepted=ownership_accepted,spawn_started=spawn_started,child_handle_obtained=proc is not None,elapsed_seconds=time.monotonic()-started,stdout_bytes=stdout_bytes,stderr_bytes=len(stderr),received_frames=framer.count if framer is not None else 0,sent_frames=peer.sent if peer is not None else 0,close_attempts=bridge.close_attempts,
                protocol_completed=report['transport_ok'],cleanup_failures=[{'resource':name,'error':_sdk_exception_detail(error)}for name,error in cleanup_errors])
  if cleanup_errors:report['transport_ok']=False
 # Keep the original error reachable; secondary cleanup order is also retained
 # as sanitized Host-local diagnostics, never exception text sent to the SDK.
 outcome=primary
 if cleanup_errors:
  outcome=RuntimeError('SDK_WIRE_CLEANUP_FAILED')
  outcome.primary_failure=report['primary'];outcome.cleanup_failures=report['cleanup_failures']
  outcome.__cause__=primary if primary is not None else cleanup_errors[0][1]
  outcome.__suppress_context__=True
 # Publication cannot erase the primary causal chain or trigger another close.
 try:publish(report)
 except BaseException as secondary:
  wrapped=RuntimeError('SDK_WIRE_EVIDENCE_PUBLICATION_FAILED')
  wrapped.primary_failure=report['primary'];wrapped.cleanup_failures=report['cleanup_failures']
  wrapped.publication_failure=_sdk_exception_detail(secondary);wrapped.close_attempts=bridge.close_attempts
  raise wrapped from (outcome if outcome is not None else secondary)
 if outcome is not None:raise outcome
 return report
