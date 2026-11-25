// Candidate-only bounded SDK wire; the nonce is correlation, not authorization.
export const PROTOCOL='v4.sdk-readonly/1';
export const SDK=Object.freeze({engine:'@earendil-works/pi-coding-agent',version:'1.4.0',adapter_contract:'host-run-receipt-v1'});
export const LIMITS=Object.freeze({frame:262144,total:8388608,frames:256,depth:32,stderr:262144,handshake:3000,partial:5000,session:45000});
export class ProtocolError extends Error {constructor(code){super(code);this.code=code;this.name='ProtocolError';}}
const fail=code=>{throw new ProtocolError(code);};
const object=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
const exact=(x,keys)=>{if(!object(x)||Object.keys(x).length!==keys.length||keys.some(k=>!Object.hasOwn(x,k)))fail('fields');};
const integer=(x,min=0,max=Number.MAX_SAFE_INTEGER)=>{if(!Number.isSafeInteger(x)||x<min||x>max)fail('integer');};
const identifier=(x,max=128)=>{if(typeof x!=='string'||x.length<1||x.length>max||![...x].every(c=>'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-'.includes(c)))fail('identifier');};
export function shape(x,depth=0){
 if(x!==null&&typeof x==='object'){
  if(depth+1>LIMITS.depth)fail('depth');
  if(!Array.isArray(x)&&Object.getPrototypeOf(x)!==Object.prototype&&Object.getPrototypeOf(x)!==null)fail('json_type');
  for(const v of Array.isArray(x)?x:[...Object.keys(x),...Object.values(x)])shape(v,depth+1);
 }else if(typeof x==='string'){
  for(let i=0;i<x.length;i++){const c=x.charCodeAt(i);if(c>=0xd800&&c<=0xdbff){const d=x.charCodeAt(++i);if(!(d>=0xdc00&&d<=0xdfff))fail('surrogate');}else if(c>=0xdc00&&c<=0xdfff)fail('surrogate');}
 }else if(typeof x==='number')integer(x,-Number.MAX_SAFE_INTEGER);
 else if(x!==null&&typeof x!=='boolean')fail('json_type');
}
// A structural JSON parser detects duplicate decoded keys before assignment.
// No eval, regex duplicate detection, last-wins JSON.parse, or new dependency.
export function decode(raw){
 if(!Buffer.isBuffer(raw)||!raw.length||raw.length+1>LIMITS.frame)fail('frame_bytes');
 if(raw.subarray(0,3).equals(Buffer.from([239,187,191])))fail('bom');
 let s;try{s=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(raw);}catch{fail('utf8');}
 let i=0;const ws=()=>{while(i<s.length&&' \t\r\n'.includes(s[i]))i++;};
 const string=()=>{const start=i++;let escaped=false;while(i<s.length){const c=s[i++];if(escaped){escaped=false;continue;}if(c==='\\'){escaped=true;continue;}if(c==='"'){let v;try{v=JSON.parse(s.slice(start,i));}catch{fail('json');}shape(v);return v;}if(c.charCodeAt(0)<32)fail('json');}fail('json');};
 const value=depth=>{
  ws();const c=s[i];
  if(c==='"')return string();
  if(c==='{'||c==='['){
   if(depth+1>LIMITS.depth)fail('depth');i++;ws();const array=c==='[',end=array?']':'}',out=array?[]:{},seen=new Set();
   if(s[i]===end){i++;return out;}
   while(true){
    if(array)out.push(value(depth+1));
    else {ws();if(s[i]!=='"')fail('json');const k=string();if(seen.has(k))fail('duplicate_key');seen.add(k);ws();if(s[i++]!==':')fail('json');const v=value(depth+1);Object.defineProperty(out,k,{value:v,enumerable:true,writable:true,configurable:true});}
    ws();const next=s[i++];if(next===end)return out;if(next!==',')fail('json');
   }
  }
  for(const [token,v]of [['true',true],['false',false],['null',null]])if(s.startsWith(token,i)){i+=token.length;return v;}
  const start=i;if(s[i]==='-')i++;
  if(s[i]==='0')i++;else if(s[i]>='1'&&s[i]<='9'){while(s[i]>='0'&&s[i]<='9')i++;}else fail('json');
  if(s[i]==='.'||s[i]==='e'||s[i]==='E')fail('non_integer_json_number');
  const n=Number(s.slice(start,i));if(!Number.isSafeInteger(n))fail('unsafe_integer');return n;
 };
 const result=value(0);ws();if(i!==s.length)fail('json');shape(result);return result;
}
export function encode(value){shape(value);const b=Buffer.from(JSON.stringify(value)+'\n');if(b.length>LIMITS.frame)fail('frame_bytes');return b;}
export class Framer {
 constructor(){this.buffer=Buffer.alloc(0);this.started=null;this.total=0;this.count=0;this.closed=false;}
 check(at=performance.now()){if(this.closed)fail('framer_closed');if(this.started!==null&&at-this.started>=LIMITS.partial)fail('partial_deadline');}
 feed(data,at=performance.now()){
  this.check(at);if(!Buffer.isBuffer(data))fail('bytes_required');this.total+=data.length;if(this.total>LIMITS.total)fail('total_bytes');const out=[];let offset=0;
  while(offset<data.length){if(this.started===null)this.started=at;let end=data.indexOf(10,offset);end=end<0?data.length:end+1;const part=data.subarray(offset,end);offset=end;if(this.buffer.length+part.length>LIMITS.frame)fail('frame_bytes');this.buffer=Buffer.concat([this.buffer,part]);if(part.at(-1)===10){if(++this.count>LIMITS.frames)fail('frame_count');out.push(decode(this.buffer.subarray(0,-1)));this.buffer=Buffer.alloc(0);this.started=null;}}
  return out;
 }
 eof(){if(this.buffer.length)fail('partial_eof');this.closed=true;}
}
const ops=['model_begin','model_complete','tool_request'],codes=['host_operation_failed','protocol_error','runtime_failed'];
export function validate(f){
 exact(f,['protocol','nonce','run_id','epoch','seq','type','reply_to','body']);if(f.protocol!==PROTOCOL)fail('protocol');identifier(f.nonce);identifier(f.run_id);
 if(f.nonce.length!==32||![...f.nonce].every(c=>'0123456789abcdef'.includes(c)))fail('nonce');
 if(typeof f.epoch!=='string'||!f.epoch.length||f.epoch.length>30||![...f.epoch].every(c=>'0123456789'.includes(c))||(f.epoch.length>1&&f.epoch[0]==='0'))fail('epoch');
 integer(f.seq,1,LIMITS.frames);if(f.reply_to!==null)integer(f.reply_to,1,LIMITS.frames);const b=f.body;
 if(f.type==='hello'||f.type==='ready'){exact(b,['sdk','profile']);exact(b.sdk,Object.keys(SDK));if(Object.keys(SDK).some(k=>b.sdk[k]!==SDK[k])||b.profile!=='tasks.list_mine@1')fail('sdk_identity');}
 else if(f.type==='start'){exact(b,['tool_name','input_schema','prompt']);if(b.tool_name!=='tasks.list_mine@1'||!object(b.input_schema)||typeof b.prompt!=='string'||b.prompt.length<1||[...b.prompt].length>8000)fail('start');}
 else if(f.type==='request'){
  exact(b,['op','arguments']);const a=b.arguments;
  if(b.op==='model_begin'){exact(a,['input_digest']);if(typeof a.input_digest!=='string'||a.input_digest.length!==64||![...a.input_digest].every(c=>'0123456789abcdef'.includes(c)))fail('digest');}
  else if(b.op==='model_complete'){exact(a,['permit','step_id','message']);identifier(a.permit);identifier(a.step_id);exact(a.message,['role','content','usage','stopReason']);if(a.message.role!=='assistant'||!Array.isArray(a.message.content)||!['toolUse','stop'].includes(a.message.stopReason))fail('message');exact(a.message.usage,['input','output']);for(const n of Object.values(a.message.usage))integer(n);}
  else if(b.op==='tool_request'){exact(a,['call_id','arguments']);identifier(a.call_id,100);if(!object(a.arguments))fail('arguments');}else fail('unsupported_operation');
 }else if(f.type==='response'){if(f.reply_to===null)fail('reply_required');exact(b,['op','ok','result','code']);if(!ops.includes(b.op)||typeof b.ok!=='boolean')fail('response');if(b.ok){if(b.code!==null||!object(b.result))fail('response');}else if(b.result!==null||!codes.includes(b.code))fail('error_code');}
 else if(f.type==='session_end'){exact(b,['status']);if(!['ok','error'].includes(b.status))fail('status');}
 else if(f.type==='error'){exact(b,['code']);if(!codes.includes(b.code))fail('error_code');}
 else fail('unsupported_type');shape(f);
}
export class Peer {
 constructor(role,identity=null){if(!['host','sdk'].includes(role))fail('role');this.role=role;this.identity=identity;this.sent=0;this.received=0;this.phase='new';this.pending=null;this.sentBytes=0;}
 transition(f,sending){const sender=sending?this.role:(this.role==='host'?'sdk':'host'),t=f.type,b=f.body;
  if(t!=='response'&&f.reply_to!==null)fail('unexpected_reply');
  if(t==='hello'&&sender==='host'&&this.phase==='new')this.phase='hello';
  else if(t==='ready'&&sender==='sdk'&&this.phase==='hello')this.phase='ready';
  else if(t==='start'&&sender==='host'&&this.phase==='ready')this.phase='running';
  else if(t==='request'&&sender==='sdk'&&this.phase==='running'&&this.pending===null)this.pending=[f.seq,b.op];
  else if(t==='response'&&sender==='host'&&this.phase==='running'&&this.pending?.[0]===f.reply_to&&this.pending?.[1]===b.op)this.pending=null;
  else if(t==='session_end'&&sender==='sdk'&&this.phase==='running'&&this.pending===null)this.phase='ended';
  else if(t==='error'&&sender==='sdk'&&['ready','running'].includes(this.phase))this.phase='failed';else fail('phase_direction_correlation');
 }
 receive(f){validate(f);const identity=Object.fromEntries(['nonce','run_id','epoch'].map(k=>[k,f[k]]));if(this.identity===null){if(this.role!=='sdk'||f.type!=='hello')fail('hello_required');}else if(Object.keys(identity).some(k=>identity[k]!==this.identity[k]))fail('identity');if(f.seq!==this.received+1)fail('sequence');this.transition(f,false);this.identity=identity;this.received=f.seq;return f;}
 send(type,body,reply_to=null){if(this.identity===null)fail('identity_required');const f={protocol:PROTOCOL,...this.identity,seq:this.sent+1,type,reply_to,body};validate(f);const raw=encode(f);if(this.sentBytes+raw.length>LIMITS.total)fail('total_bytes');this.transition(f,true);this.sent++;this.sentBytes+=raw.length;return raw;}
}
