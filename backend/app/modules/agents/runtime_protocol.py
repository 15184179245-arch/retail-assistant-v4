"""Bounded candidate-only SDK stdio profile. Not the legacy v4.pi/1 contract.

The transport nonce is correlation, never a database identity or authorization.
This small profile admits only JSON safe integers (no decimal/exponent numbers).
"""
from __future__ import annotations
import json
import base64
from hashlib import sha256
from copy import deepcopy
import math
import time

PROTOCOL = 'v4.sdk-readonly/1'
SDK = {'engine':'@earendil-works/pi-coding-agent','version':'1.4.0','adapter_contract':'host-run-receipt-v1'}
FRAME_BYTES=262144; TOTAL_BYTES=8388608; MAX_FRAMES=256; MAX_DEPTH=32
STDERR_BYTES=262144; HANDSHAKE_SECONDS=3; PARTIAL_SECONDS=5; SESSION_SECONDS=45
DISCOVERY_SESSION_SECONDS=900
SAFE_INT=9007199254740991
OPS={'model_begin','model_complete','tool_request'}
CODES={'host_operation_failed','protocol_error','runtime_failed'}
class ProtocolError(ValueError):
 def __init__(self,code):self.code=code;super().__init__(code)
def fail(code):raise ProtocolError(code)
def exact(obj,keys):
 if type(obj)is not dict or set(obj)!=set(keys):fail('fields')
def identifier(value,maximum=128):
 if type(value)is not str or not 1<=len(value)<=maximum or any(not(c.isascii()and(c.isalnum()or c in '_.:-'))for c in value):fail('identifier')
def integer(value,low=0,high=SAFE_INT):
 if type(value)is not int or not low<=value<=high:fail('integer')
def _number(text):
 n=int(text)
 if abs(n)>SAFE_INT:fail('unsafe_integer')
 return n
def _noninteger(_):fail('non_integer_json_number')
def _pairs(rows):
 out={}
 for k,v in rows:
  if k in out:fail('duplicate_key')
  out[k]=v
 return out
def _shape(value,depth=0,finite_numbers=False):
 if isinstance(value,(dict,list)):
  if depth+1>MAX_DEPTH:fail('depth')
  children=list(value.values())+list(value.keys()) if type(value)is dict else value
  for v in children:_shape(v,depth+1,finite_numbers)
 elif type(value)is str:
  if any(0xD800<=ord(c)<=0xDFFF for c in value):fail('surrogate')
 elif type(value)is int:integer(value,-SAFE_INT)
 elif type(value)is float and finite_numbers:
  if not math.isfinite(value):fail("non_finite_number")
 elif value is not None and type(value)is not bool:fail('json_type')
def _decode_json(raw,finite_numbers=False):
 if type(raw)is not bytes or not raw or len(raw)+1>FRAME_BYTES:fail('frame_bytes')
 try:text=raw.decode('utf-8','strict')
 except UnicodeDecodeError:fail('utf8')
 if text.startswith('\ufeff'):fail('bom')
 # Bound nesting before the standard parser allocates recursive objects.
 depth=0;string=False;escape=False
 for c in text:
  if string:
   if escape:escape=False
   elif c=='\\':escape=True
   elif c=='"':string=False
  elif c=='"':string=True
  elif c in '[{':
   depth+=1
   if depth>MAX_DEPTH:fail('depth')
  elif c in ']}':depth-=1
 try:value=json.loads(text,object_pairs_hook=_pairs,parse_int=_number,parse_float=_finite_number if finite_numbers else _noninteger,parse_constant=_noninteger)
 except (json.JSONDecodeError,RecursionError):fail('json')
 _shape(value,finite_numbers=finite_numbers)
 return value
def _finite_number(text):
 n=float(text)
 if not math.isfinite(n):fail("non_finite_number")
 return n
def decode(raw):return _decode_json(raw)
def encode(value):
 _shape(value)
 raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')+b'\n'
 if len(raw)>FRAME_BYTES:fail('frame_bytes')
 return raw
class Framer:
 def __init__(self,max_frames=MAX_FRAMES):self.max_frames=max_frames;self.buffer=bytearray();self.started=None;self.total=0;self.count=0;self.closed=False
 def check(self,at=None):
  if self.closed:fail('framer_closed')
  at=time.monotonic() if at is None else at
  if self.started is not None and at-self.started>=PARTIAL_SECONDS:fail('partial_deadline')
 def feed(self,data,at=None):
  at=time.monotonic() if at is None else at;self.check(at)
  if type(data)is not bytes:fail('bytes_required')
  self.total+=len(data)
  if self.total>TOTAL_BYTES:fail('total_bytes')
  messages=[];offset=0
  while offset<len(data):
   if self.started is None:self.started=at
   end=data.find(b'\n',offset);end=len(data) if end<0 else end+1
   part=data[offset:end];offset=end
   if len(self.buffer)+len(part)>FRAME_BYTES:fail('frame_bytes')
   self.buffer.extend(part)
   if part.endswith(b'\n'):
    self.count+=1
    if self.count>self.max_frames:fail('frame_count')
    messages.append(decode(bytes(self.buffer[:-1])));self.buffer.clear();self.started=None
  return messages
 def eof(self):
  if self.buffer:fail('partial_eof')
  self.closed=True
def validate(frame):
 exact(frame,{'protocol','nonce','run_id','epoch','seq','type','reply_to','body'})
 if frame['protocol']!=PROTOCOL:fail('protocol')
 for k in ['nonce','run_id']:identifier(frame[k])
 nonce=frame['nonce']
 if len(nonce)!=32 or any(c not in '0123456789abcdef'for c in nonce):fail('nonce')
 epoch=frame['epoch']
 if type(epoch)is not str or not epoch or len(epoch)>30 or not epoch.isascii() or not epoch.isdecimal() or (len(epoch)>1 and epoch[0]=='0'):fail('epoch')
 integer(frame['seq'],1,MAX_FRAMES)
 if frame['reply_to'] is not None:integer(frame['reply_to'],1,MAX_FRAMES)
 t=frame['type'];b=frame['body']
 if type(t)is not str:fail('type')
 if t in ('hello','ready'):
  exact(b,{'sdk','profile'});exact(b['sdk'],SDK)
  if b['sdk']!=SDK or b['profile']!='tasks.list_mine@1':fail('sdk_identity')
 elif t=='start':
  exact(b,{'tool_name','input_schema','prompt'})
  if b['tool_name']!='tasks.list_mine@1' or type(b['input_schema'])is not dict or type(b['prompt'])is not str or not 1<=len(b['prompt'])<=8000:fail('start')
 elif t=='request':
  exact(b,{'op','arguments'});op=b['op'];a=b['arguments']
  if type(op)is not str:fail('operation_type')
  if op=='model_begin':
   exact(a,{'input_digest'})
   if type(a['input_digest'])is not str or len(a['input_digest'])!=64 or any(c not in '0123456789abcdef'for c in a['input_digest']):fail('digest')
  elif op=='model_complete':
   exact(a,{'permit','step_id','message'});identifier(a['permit']);identifier(a['step_id'])
   exact(a['message'],{'role','content','usage','stopReason'})
   if a['message']['role']!='assistant' or type(a['message']['content'])is not list or a['message']['stopReason'] not in ('toolUse','stop'):fail('message')
   exact(a['message']['usage'],{'input','output'})
   for n in a['message']['usage'].values():integer(n)
  elif op=='tool_request':
   exact(a,{'call_id','arguments'});identifier(a['call_id'],100)
   if type(a['arguments'])is not dict:fail('arguments')
  else:fail('unsupported_operation')
 elif t=='response':
  if frame['reply_to'] is None:fail('reply_required')
  exact(b,{'op','ok','result','code'})
  if type(b['op'])is not str or b['op'] not in OPS or type(b['ok'])is not bool:fail('response')
  if b['ok']:
   if b['code'] is not None or type(b['result'])is not dict:fail('response')
  elif b['result'] is not None or type(b['code'])is not str or b['code'] not in CODES:fail('error_code')
 elif t=='session_end':
  exact(b,{'status'})
  if b['status'] not in ('ok','error'):fail('status')
 elif t=='error':
  exact(b,{'code'})
  if type(b['code'])is not str or b['code'] not in CODES:fail('error_code')
 else:fail('unsupported_type')
 _shape(frame)
class Peer:
 def __init__(self,role,identity=None):
  if role not in ('host','sdk'):fail('role')
  self.role=role;self.identity=identity;self.sent=0;self.received=0;self.phase='new';self.pending=None;self.sent_bytes=0
 def _transition(self,f,sending):
  sender=self.role if sending else ('sdk'if self.role=='host'else'host');t=f['type'];b=f['body']
  if t!='response' and f['reply_to'] is not None:fail('unexpected_reply')
  if t=='hello' and sender=='host' and self.phase=='new':self.phase='hello'
  elif t=='ready' and sender=='sdk' and self.phase=='hello':self.phase='ready'
  elif t=='start' and sender=='host' and self.phase=='ready':self.phase='running'
  elif t=='request' and sender=='sdk' and self.phase=='running' and self.pending is None:self.pending=(f['seq'],b['op'])
  elif t=='response' and sender=='host' and self.phase=='running' and self.pending==(f['reply_to'],b['op']):self.pending=None
  elif t=='session_end' and sender=='sdk' and self.phase=='running' and self.pending is None:self.phase='ended'
  elif t=='error' and sender=='sdk' and self.phase in ('ready','running'):self.phase='failed'
  else:fail('phase_direction_correlation')
 def receive(self,f):
  validate(f)
  identity={k:f[k]for k in ('nonce','run_id','epoch')}
  if self.identity is None:
   if self.role!='sdk' or f['type']!='hello':fail('hello_required')
  elif identity!=self.identity:fail('identity')
  if f['seq']!=self.received+1:fail('sequence')
  self._transition(f,False);self.identity=identity;self.received=f['seq'];return f
 def send(self,kind,body,reply_to=None):
  if self.identity is None:fail('identity_required')
  f={'protocol':PROTOCOL,**self.identity,'seq':self.sent+1,'type':kind,'reply_to':reply_to,'body':body};validate(f);raw=encode(f)
  if self.sent_bytes+len(raw)>TOTAL_BYTES:fail('total_bytes')
  self._transition(f,True);self.sent+=1;self.sent_bytes+=len(raw);return raw


# Formal AGT-028 is a distinct protocol. Old experiment codecs and Peers above
# remain unchanged so an old wire name/profile can never become a formal Run.
PI_PROTOCOL = "v4.pi-readonly/1"
PI_PROFILE = "pi-readonly-v1"
PI_NAMES = frozenset(("v4_tasks_list_mine_v1", "v4_knowledge_search_v1", "v4_catalog_search_v1", "v4_memory_search_v1"))
PI_MEMORY_PROTOCOL = "v4.pi-memory/1"
PI_MEMORY_ONLINE_PROTOCOL = "v4.pi-memory/2"
PI_MEMORY_PROFILE = "pi-memory-v1"
PI_MEMORY_ONLINE_PROFILE = "pi-memory-online-v1"
PI_MEMORY_NAMES = PI_NAMES | frozenset('v4_memory_' + op + '_v1' for op in ('save', 'correct', 'withdraw'))
PI_DISCOVERY_PROTOCOL = 'v4.pi-discovery/1'
PI_DISCOVERY_ONLINE_PROTOCOL = 'v4.pi-discovery/2'
PI_DISCOVERY_PROFILE = 'pi-discovery-v1'
PI_DISCOVERY_ONLINE_PROFILE = 'pi-discovery-online-v1'
PI_DISCOVERY_NAMES = PI_MEMORY_NAMES | frozenset({'tool_search'})
PI_SOURCE_PROTOCOL = 'v4.pi-source/1'
PI_SOURCE_ONLINE_PROTOCOL = 'v4.pi-source/2'
PI_SOURCE_ONLINE_PROFILE = 'pi-source-online-v1'
PI_SOURCE_PROFILE = 'pi-source-v1'
PI_SOURCE_NAMES = PI_DISCOVERY_NAMES | frozenset({'v4_guanjia_read_prices_v1', 'v4_guanjia_read_stock_v1', 'v4_guanjia_sales_ranking_v1', 'v4_guanjia_sales_ranking_v2', 'v4_guanjia_local_sales_v1','v4_guanjia_category_discovery_v1','v4_guanjia_category_products_v1','v4_guanjia_category_products_status_v1', 'v4_research_search_v1', 'v4_research_read_v1', 'v4_research_describe_v1', 'v4_research_propose_v1', 'v4_research_probe_v1'})
PI_PURPOSES = frozenset(("inference", "retry", "compaction"))


def _pi_digest(value, *, prefixed=False):
    prefix = "sha256:" if prefixed else ""
    if (type(value) is not str or len(value) != len(prefix) + 64
            or not value.startswith(prefix)
            or any(c not in "0123456789abcdef" for c in value[len(prefix):])):
        fail("digest")


def _pi_name_version(name, version, memory=False, discovery=False, source=False):
    if type(name) is not str or name not in (PI_SOURCE_NAMES if source else PI_DISCOVERY_NAMES if discovery else PI_MEMORY_NAMES if memory else PI_NAMES) or version != ("3" if name == "v4_guanjia_local_sales_v1" else "2" if name == "v4_guanjia_sales_ranking_v2" else "1"):
        fail("tool_profile")


def _pi_native_id(value):
    if (type(value) is not str or not 1 <= len(value) <= 100
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in value)):
        fail("native_call_id")


def _validate_pi_wire(frame, online=False, memory=False, discovery=False, source=False):
    if source and (not discovery or not memory):
        fail('tool_profile')
    protocol = (PI_MEMORY_ONLINE_PROTOCOL if online else PI_MEMORY_PROTOCOL) if memory else (PI_ONLINE_PROTOCOL if online else PI_PROTOCOL)
    profile = PI_MEMORY_PROFILE if memory else PI_PROFILE
    hello_profile = (PI_MEMORY_ONLINE_PROFILE if online else PI_MEMORY_PROFILE) if memory else (PI_ONLINE_PROFILE if online else PI_PROFILE)
    if discovery:
        protocol = PI_DISCOVERY_ONLINE_PROTOCOL if online else PI_DISCOVERY_PROTOCOL
        profile = PI_DISCOVERY_PROFILE
        hello_profile = PI_DISCOVERY_ONLINE_PROFILE if online else PI_DISCOVERY_PROFILE
    if source:
        protocol = PI_SOURCE_ONLINE_PROTOCOL if online else PI_SOURCE_PROTOCOL
        profile = PI_SOURCE_PROFILE
        hello_profile = PI_SOURCE_ONLINE_PROFILE if online else PI_SOURCE_PROFILE
    names = PI_SOURCE_NAMES if source else PI_DISCOVERY_NAMES if discovery else PI_MEMORY_NAMES if memory else PI_NAMES
    exact(frame, {"protocol", "nonce", "run_id", "epoch", "seq", "type", "reply_to", "body"})
    if frame["protocol"] != protocol:
        fail("protocol")
    identifier(frame["run_id"])
    nonce = frame["nonce"]
    if type(nonce) is not str or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
        fail("nonce")
    epoch = frame["epoch"]
    if (type(epoch) is not str or not 1 <= len(epoch) <= 30
            or not epoch.isascii() or not epoch.isdecimal()
            or len(epoch) > 1 and epoch[0] == "0"):
        fail("epoch")
    integer(frame["seq"], 1, MAX_FRAMES)
    if frame["reply_to"] is not None:
        integer(frame["reply_to"], 1, MAX_FRAMES)
    t, body = frame["type"], frame["body"]
    if type(t) is not str:
        fail("type")
    if t in ("hello", "ready"):
        exact(body, {"sdk", "profile"})
        exact(body["sdk"], SDK)
        if body["sdk"] != SDK or body["profile"] != hello_profile:
            fail("sdk_identity")
    elif t == "start":
        has_output_limit = online and type(body) is dict and 'max_output_tokens' in body
        exact(body, {"tool_profile", "toolset_version", "tools", "prompt"}
              | ({'max_output_tokens'} if has_output_limit else set()))
        if has_output_limit:
            value = body['max_output_tokens']
            if type(value) is not int or not 1 <= value <= (8192 if discovery else 2048):
                fail('start')
        if body["tool_profile"] != profile:
            fail("tool_profile")
        _pi_digest(body["toolset_version"], prefixed=True)
        if type(body["prompt"]) is not str or not body["prompt"]:
            fail("start")
        if type(body["tools"]) is not list or len(body["tools"]) > len(names):
            fail("tool_profile")
        seen = set()
        for tool in body["tools"]:
            exact(tool, {"name", "version", "description", "parameters"})
            _pi_name_version(tool["name"], tool["version"], memory, discovery, source)
            if (tool["name"] in seen or type(tool["description"]) is not str
                    or type(tool["parameters"]) is not dict):
                fail("tool_profile")
            seen.add(tool["name"])
    elif t == "request":
        exact(body, {"op", "arguments"})
        op, args = body["op"], body["arguments"]
        if op == "provider_call" and online:
            _pi_provider_request(args)
        elif op == "model_begin":
            exact(args, {"purpose", "input_digest", "toolset_version"} | ({"request_digest"} if online else set()))
            if type(args["purpose"]) is not str or args["purpose"] not in PI_PURPOSES:
                fail("purpose")
            _pi_digest(args["input_digest"])
            _pi_digest(args["toolset_version"], prefixed=True)
            if online:
                _pi_digest(args["request_digest"])
        elif op == "model_complete":
            exact(args, {"attempt_id", "permit", "step_id", "message"})
            for key in ("attempt_id", "permit", "step_id"):
                identifier(args[key])
            if args["attempt_id"] != args["step_id"]:
                fail("attempt_binding")
            message = args["message"]
            exact(message, {"role", "content", "usage", "stopReason"})
            if (message["role"] != "assistant" or type(message["content"]) is not list
                    or message["stopReason"] not in ("toolUse", "stop", "error", "aborted")):
                fail("message")
            exact(message["usage"], {"input", "output"} | ({"cacheRead", "cacheWrite"} if online else set()))
            for n in message["usage"].values():
                integer(n)
            # Tool decisions are interpreted only AFTER Host usage settlement.
            # Transport validation must not turn an invalid decision into zero cost.
        elif op == "tool_request":
            is_search = discovery and type(args) is dict and args.get('name') == 'tool_search'
            exact(args, {"call_id", "name", "version", "toolset_version", "arguments"} | ({'selection'} if is_search else set()))
            if is_search and (type(args['selection']) is not list or len(args['selection']) > 100
                    or any(type(name) is not str or name not in names - {'tool_search'} for name in args['selection'])):
                fail('tool_profile')
            _pi_native_id(args["call_id"])
            _pi_name_version(args["name"], args["version"], memory, discovery, source)
            _pi_digest(args["toolset_version"], prefixed=True)
            if type(args["arguments"]) is not dict:
                fail("arguments")
        else:
            fail("unsupported_operation")
    elif t == "response":
        if frame["reply_to"] is None:
            fail("reply_required")
        exact(body, {"op", "ok", "result", "code"})
        if type(body["op"]) is not str or body["op"] not in (OPS | {"provider_call"} if online else OPS) or type(body["ok"]) is not bool:
            fail("response")
        if body["ok"]:
            if body["code"] is not None or type(body["result"]) is not dict:
                fail("response")
            result = body["result"]
            if body["op"] == "provider_call" and online:
                _pi_provider_response(result)
            elif body["op"] == "model_begin":
                exact(result, {"attempt_id", "step_id", "permit", "limits"})
                for k in ("attempt_id", "step_id", "permit"):
                    identifier(result[k])
                if result["attempt_id"] != result["step_id"]:
                    fail("attempt_binding")
                exact(result["limits"], {"input", "output"})
                for n in result["limits"].values():
                    integer(n, 1)
            elif body["op"] == "model_complete":
                exact(result, {"committed", "finished"} | ({"argument_rejection"} if online and "argument_rejection" in result else set()))
                if result["committed"] is not True or type(result["finished"]) is not bool:
                    fail("commit_receipt")
            else:
                exact(result, {"call_id", "name", "version", "toolset_version", "receipt_id", "step_id", "output_ref", "result"})
                _pi_native_id(result["call_id"])
                _pi_name_version(result["name"], result["version"], memory, discovery, source)
                _pi_digest(result["toolset_version"], prefixed=True)
                _pi_digest(result["output_ref"], prefixed=True)
                identifier(result["receipt_id"])
                identifier(result["step_id"])
                if type(result["result"]) is not dict:
                    fail("result")
        elif body["result"] is not None or type(body["code"]) is not str or body["code"] not in CODES:
            fail("error_code")
    elif t == "session_end":
        exact(body, {"status"})
        if body["status"] not in ("ok", "error"):
            fail("status")
    elif t == "error":
        exact(body, {"code"})
        if type(body["code"]) is not str or body["code"] not in CODES:
            fail("error_code")
    else:
        fail("unsupported_type")
    _shape(frame)


PI_ONLINE_PROTOCOL = "v4.pi-readonly/2"
PI_ONLINE_PROFILE = "pi-readonly-online-v1"
PI_PROVIDER_PATH = "/inference/go/openai/v1/chat/completions"
PI_PROVIDER_BYTES = 131072


def _pi_provider_body(value):
    if type(value) is list:
        for child in value:
            _pi_provider_body(child)
    elif type(value) is dict:
        for key, child in value.items():
            if key.lower() in {"url", "baseurl", "base_url", "endpoint", "header", "headers", "authorization", "apikey", "api_key", "api-key", "key", "proxy", "provider"}:
                fail("provider_control")
            _pi_provider_body(child)


def _pi_provider_request(args):
    exact(args, {"attempt_id", "step_id", "permit", "input_digest", "request_digest", "request"})
    for key in ("attempt_id", "step_id", "permit"):
        identifier(args[key])
    if args["attempt_id"] != args["step_id"]:
        fail("attempt_binding")
    _pi_digest(args["input_digest"])
    _pi_digest(args["request_digest"])
    request = args["request"]
    exact(request, {"method", "path", "body"})
    if request["method"] != "POST" or request["path"] != PI_PROVIDER_PATH or type(request["body"]) is not str:
        fail("provider_request")
    _shape(request["body"])
    raw = request["body"].encode("utf-8")
    if len(raw) > PI_PROVIDER_BYTES:
        fail("provider_bytes")
    parsed = _decode_json(raw, finite_numbers=True)
    if type(parsed) is not dict:
        fail("provider_request")
    _pi_provider_body(parsed)


def _pi_provider_response(result):
    exact(result, {"status", "request_id", "body"})
    integer(result["status"], 200, 599)
    if result["request_id"] is not None:
        identifier(result["request_id"])
    _shape(result["body"])
    if type(result["body"]) is not str or len(result["body"].encode("utf-8")) > PI_PROVIDER_BYTES:
        fail("provider_bytes")


def validate_pi(frame):
    _validate_pi_wire(frame)


def validate_pi_online(frame):
    _validate_pi_wire(frame, online=True)


class PiPeer(Peer):
    """Same bounded transport state machine; distinct, non-fallback grammar."""

    protocol = PI_PROTOCOL
    validator = staticmethod(validate_pi)

    def receive(self, frame):
        self.validator(frame)
        identity = {k: frame[k] for k in ("nonce", "run_id", "epoch")}
        if self.identity is None:
            if self.role != "sdk" or frame["type"] != "hello":
                fail("hello_required")
        elif identity != self.identity:
            fail("identity")
        if frame["seq"] != self.received + 1:
            fail("sequence")
        self._transition(frame, False)
        self.identity = identity
        self.received = frame["seq"]
        return frame

    def send(self, kind, body, reply_to=None):
        if self.identity is None:
            fail("identity_required")
        frame = {"protocol": self.protocol, **self.identity, "seq": self.sent + 1,
                 "type": kind, "reply_to": reply_to, "body": body}
        self.validator(frame)
        raw = encode(frame)
        if self.sent_bytes + len(raw) > TOTAL_BYTES:
            fail("total_bytes")
        self._transition(frame, True)
        self.sent += 1
        self.sent_bytes += len(raw)
        return raw


class PiOnlinePeer(PiPeer):
    """Explicit online opt-in, never a fallback for the default v1 peer."""
    protocol = PI_ONLINE_PROTOCOL
    validator = staticmethod(validate_pi_online)


def validate_pi_memory(frame):
    _validate_pi_wire(frame, memory=True)


def validate_pi_memory_online(frame):
    _validate_pi_wire(frame, online=True, memory=True)


class PiMemoryPeer(PiPeer):
    protocol = PI_MEMORY_PROTOCOL
    validator = staticmethod(validate_pi_memory)


class PiMemoryOnlinePeer(PiPeer):
    protocol = PI_MEMORY_ONLINE_PROTOCOL
    validator = staticmethod(validate_pi_memory_online)


def validate_pi_discovery(frame):
    _validate_pi_wire(frame, memory=True, discovery=True)


def validate_pi_discovery_online(frame):
    _validate_pi_wire(frame, online=True, memory=True, discovery=True)


class PiDiscoveryPeer(PiPeer):
    protocol = PI_DISCOVERY_PROTOCOL
    validator = staticmethod(validate_pi_discovery)


class PiDiscoveryOnlinePeer(PiPeer):
    protocol = PI_DISCOVERY_ONLINE_PROTOCOL
    validator = staticmethod(validate_pi_discovery_online)


def validate_pi_source(frame):
    _validate_pi_wire(frame, memory=True, discovery=True, source=True)


class PiSourcePeer(PiPeer):
    protocol = PI_SOURCE_PROTOCOL
    validator = staticmethod(validate_pi_source)


def validate_pi_source_online(frame):
    _validate_pi_wire(frame, online=True, memory=True, discovery=True, source=True)


class PiSourceOnlinePeer(PiPeer):
    protocol = PI_SOURCE_ONLINE_PROTOCOL
    validator = staticmethod(validate_pi_source_online)


# Explicit /3 native-byte streaming; old validators and peers keep old limits.
PI_STREAM_PROTOCOL = 'v4.pi-readonly/3'
PI_STREAM_PROFILE = 'pi-readonly-online-stream-v1'
STREAM_FRAMES = 8192
STREAM_SESSION_SECONDS = 900
_STREAM_FIELDS = {
 'provider_open': {'stream_id','attempt_id','step_id','request_digest','status','request_id'},
 'provider_chunk': {'stream_id','chunk_seq','data_b64'},
 'provider_end': {'stream_id','last_chunk_seq','byte_count','body_sha256'},
 'provider_abort': {'stream_id','code','send_started'},
 'provider_credit': {'stream_id','through_chunk_seq','bytes_consumed'},
 'provider_cancel': {'stream_id','reason'},
}
def stream_bytes(value):
 if type(value)is not str or len(value)>5464:fail('stream_base64')
 try:raw=base64.b64decode(value,validate=True)
 except Exception:fail('stream_base64')
 if not 1<=len(raw)<=4096 or base64.b64encode(raw).decode()!=value:fail('stream_base64')
 return raw

def stream_summary(value):
 exact(value,{'status','request_id','body_sha256','byte_count','usage','response_recorded'})
 if value['status']!=200 or value['response_recorded']is not True:fail('stream_receipt')
 if value['request_id']is not None:identifier(value['request_id'])
 _pi_digest(value['body_sha256']);integer(value['byte_count'],0,PI_PROVIDER_BYTES)
 exact(value['usage'],{'input','output','cacheRead','cacheWrite'})
 for n in value['usage'].values():integer(n)
 return value

def validate_pi_stream(frame):
 exact(frame,{'protocol','nonce','run_id','epoch','seq','type','reply_to','body'})
 if frame['protocol']!=PI_STREAM_PROTOCOL:fail('protocol')
 integer(frame['seq'],1,STREAM_FRAMES)
 if frame['reply_to']is not None:integer(frame['reply_to'],1,STREAM_FRAMES)
 mapped=deepcopy(frame);mapped.update(protocol=PI_ONLINE_PROTOCOL,seq=1,reply_to=None if frame['reply_to']is None else 1)
 kind=frame['type'];body=frame['body']
 if type(kind)is str and kind.startswith('provider_'):
  _validate_pi_wire({**mapped,'type':'response','reply_to':1,'body':{'op':'provider_call','ok':False,'result':None,'code':'host_operation_failed'}},online=True)
  if kind not in _STREAM_FIELDS:fail('unsupported_type')
  exact(body,_STREAM_FIELDS[kind]);identifier(body['stream_id'])
  if frame['reply_to']is None:fail('reply_required')
  if kind=='provider_open':
   identifier(body['attempt_id']);identifier(body['step_id']);_pi_digest(body['request_digest'])
   if body['status']!=200:fail('provider_status')
   if body['request_id']is not None:identifier(body['request_id'])
  elif kind=='provider_chunk':integer(body['chunk_seq'],1,STREAM_FRAMES);stream_bytes(body['data_b64'])
  elif kind=='provider_end':integer(body['last_chunk_seq'],0,STREAM_FRAMES);integer(body['byte_count'],0,PI_PROVIDER_BYTES);_pi_digest(body['body_sha256'])
  elif kind=='provider_credit':integer(body['through_chunk_seq'],1,STREAM_FRAMES);integer(body['bytes_consumed'],1,PI_PROVIDER_BYTES)
  elif kind=='provider_abort':
   if body['code']not in ('cancelled','deadline','transport_invalid','backpressure') or type(body['send_started'])is not bool:fail('stream_abort')
  elif kind=='provider_cancel':
   if body['reason']not in ('explicit_cancel','runtime_abort'):fail('stream_cancel')
  _shape(frame);return
 if kind in ('hello','ready'):
  if type(body)is not dict or body.get('profile')!=PI_STREAM_PROFILE:fail('sdk_identity')
  mapped['body']['profile']=PI_ONLINE_PROFILE
 if kind=='response' and type(body)is dict and body.get('op')=='provider_call' and body.get('ok')is True:
  stream_summary(body.get('result'));mapped['body']['result']={'status':200,'request_id':None,'body':''}
 _validate_pi_wire(mapped,online=True);_shape(frame)

class ProviderStreamState:
 def __init__(self,args):
  self.args=deepcopy(args);self.id=args['attempt_id'];self.opened=False;self.ended=False;self.aborted=False;self.sizes=[];self.ack=0;self.consumed=0;self.total=0;self.hash=sha256();self.final_hash=None;self.request_id=None;self.late_cancelled=False
 @property
 def available(self):return 8192-self.total+self.consumed
 def event(self,kind,body,sender):
  if body['stream_id']!=self.id:fail('stream_binding')
  if kind=='provider_credit':
   if sender!='sdk' or not self.opened or self.aborted:fail('stream_direction')
   through=body['through_chunk_seq']
   if not self.ack<through<=len(self.sizes) or body['bytes_consumed']!=sum(self.sizes[:through]):fail('stream_credit')
   self.ack=through;self.consumed=body['bytes_consumed'];return
  if kind=='provider_cancel':
   if sender!='sdk' or self.aborted:fail('stream_direction')
   if self.ended:
    if self.late_cancelled:fail('stream_cancel_replay')
    self.late_cancelled=True;return 'late_complete'
   return 'active'
  if sender!='host' or self.ended or self.aborted:fail('stream_direction')
  if kind=='provider_abort':self.aborted=True;return
  if kind=='provider_open':
   if self.opened or body['attempt_id']!=self.id or body['step_id']!=self.id or body['request_digest']!=self.args['request_digest']:fail('stream_binding')
   self.opened=True;self.request_id=body['request_id'];return
  if not self.opened:fail('stream_open_required')
  if kind=='provider_chunk':
   raw=stream_bytes(body['data_b64'])
   if body['chunk_seq']!=len(self.sizes)+1 or len(raw)>self.available or self.total+len(raw)>PI_PROVIDER_BYTES:fail('stream_chunk')
   self.sizes.append(len(raw));self.total+=len(raw);self.hash.update(raw);return
  if kind=='provider_end':
   self.final_hash=self.hash.hexdigest()
   if body['last_chunk_seq']!=len(self.sizes) or body['byte_count']!=self.total or body['body_sha256']!=self.final_hash:fail('stream_digest')
   self.ended=True;return
  fail('unsupported_type')
 def finish(self,body):
  if body['ok']is False:
   if not self.aborted:fail('stream_terminal')
   return
  result=stream_summary(body['result'])
  if not self.ended or self.aborted or result['body_sha256']!=self.final_hash or result['byte_count']!=self.total or result['request_id']!=self.request_id:fail('stream_terminal')

class PiStreamPeer(PiPeer):
 protocol=PI_STREAM_PROTOCOL
 validator=staticmethod(validate_pi_stream)
 def __init__(self,role,identity=None):
  super().__init__(role,identity);self.stream_state=None;self._stream_anchor=None;self._cancel_classification=None
 @property
 def cancel_classification(self):return self._cancel_classification
 def clear_stream_anchor(self):self._stream_anchor=None;self._cancel_classification=None
 def receive(self,frame):
  self._cancel_classification=None
  return super().receive(frame)
 def send(self,kind,body,reply_to=None):
  self._cancel_classification=None
  return super().send(kind,body,reply_to)
 def _transition(self,frame,sending):
  kind=frame['type'];body=frame['body'];sender=self.role if sending else ('sdk'if self.role=='host'else'host')
  if kind.startswith('provider_'):
   anchor=self._stream_anchor;state=self.stream_state
   if (anchor is None or state is None or anchor[2] is not state
       or anchor[0]!={k:frame[k]for k in ('nonce','run_id','epoch')} or anchor[1]!=frame['reply_to']):fail('stream_pending')
   late=kind=='provider_cancel' and state.ended and not state.aborted
   if not late and self.pending!=(frame['reply_to'],'provider_call'):fail('stream_pending')
   classification=state.event(kind,body,sender)
   if kind=='provider_cancel':self._cancel_classification=classification
   return
  if kind=='response' and body['op']=='provider_call':
   if self.stream_state is None:fail('stream_pending')
   self.stream_state.finish(body)
  super()._transition(frame,sending)
  if kind in ('request','session_end','error'):
   self.clear_stream_anchor()
   if kind=='request' and body['op']=='provider_call':
    self.stream_state=ProviderStreamState(body['arguments'])
    self._stream_anchor=({k:frame[k]for k in ('nonce','run_id','epoch')},frame['seq'],self.stream_state)


# Result ABI is an explicit profile, never a broader old-/3 result union.
PI_RESULT_PROFILE = 'pi-readonly-online-stream-result-v1'
PI_RESULT_LAUNCH_PROFILES = frozenset('host-pi-readonly-node24-'+p+'-online-stream-result-v1' for p in ('darwin','linux'))


def validate_pi_canonical(frame):
    from . import canonical_result
    exact(frame,{'protocol','nonce','run_id','epoch','seq','type','reply_to','body'})
    mapped=deepcopy(frame)
    if frame['type'] in ('hello','ready'):
        if type(frame['body'])is not dict or frame['body'].get('profile')!=PI_RESULT_PROFILE:fail('sdk_identity')
        mapped['body']['profile']=PI_STREAM_PROFILE
    body=frame['body']
    if frame['type']=='response' and type(body)is dict and body.get('op')=='tool_request' and body.get('ok')is True:
        result=body.get('result')
        exact(result,{'call_id','name','version','toolset_version','receipt_id','step_id','output_ref','canonical_ref','result'})
        try:
            value=canonical_result.validate_envelope(result['result'])
            if canonical_result.canonical_reference(value)!=result['canonical_ref']:fail('canonical_reference')
        except canonical_result.CanonicalResultError:fail('canonical_result')
        binding=value['binding']
        if (binding['run_id']!=frame['run_id'] or str(binding['execution_epoch'])!=frame['epoch']
                or (binding['native_call_id'],binding['tool_name'],binding['tool_version'],binding['toolset_version'],binding['pi_receipt_id'],binding['tool_step_id'],value['output_ref'])
                != (result['call_id'],result['name'],result['version'],result['toolset_version'],result['receipt_id'],result['step_id'],result['output_ref'])):
            fail('canonical_binding')
        mapped['body']['result']={key:item for key,item in result.items()if key!='canonical_ref'}
        mapped['body']['result']['result']=value['payload']
    validate_pi_stream(mapped)
    _shape(frame)


class PiCanonicalPeer(PiStreamPeer):
    validator=staticmethod(validate_pi_canonical)
