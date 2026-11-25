import {createHash} from 'node:crypto';
/** Formal recovery-A wire. Never accepts the old experimental wire/profile. */
export const PROTOCOL = 'v4.pi-readonly/1' as const;
export const STREAM_PROTOCOL = 'v4.pi-readonly/3' as const;
export const RESULT_PROFILE = 'pi-readonly-online-stream-result-v1' as const;
export const RESULT_ABI = 'pi-tool-result/1' as const;
export const STREAM_PROFILE = 'pi-readonly-online-stream-v1' as const;
export const STREAM_FRAMES = 8192;
export const STREAM_SESSION_MS = 900000;
export const ONLINE_PROTOCOL = 'v4.pi-readonly/2' as const;
export const ONLINE_PROFILE = 'pi-readonly-online-v1' as const;
export const PROVIDER_PATH = '/inference/go/openai/v1/chat/completions' as const;
export const PROVIDER_BYTES = 131072;
export const PROFILE = 'pi-readonly-v1' as const;
export const MEMORY_PROFILE = 'pi-memory-v1' as const;
export const MEMORY_ONLINE_PROFILE = 'pi-memory-online-v1' as const;
export const MEMORY_PROTOCOL = 'v4.pi-memory/1' as const;
export const MEMORY_ONLINE_PROTOCOL = 'v4.pi-memory/2' as const;
export const DISCOVERY_PROFILE = 'pi-discovery-v1' as const;
export const DISCOVERY_ONLINE_PROFILE = 'pi-discovery-online-v1' as const;
export const DISCOVERY_PROTOCOL = 'v4.pi-discovery/1' as const;
export const DISCOVERY_ONLINE_PROTOCOL = 'v4.pi-discovery/2' as const;
export const SOURCE_PROFILE = 'pi-source-v1' as const;
export const SOURCE_PROTOCOL = 'v4.pi-source/1' as const;
export const SOURCE_ONLINE_PROTOCOL = 'v4.pi-source/2' as const;
export const SOURCE_ONLINE_PROFILE = 'pi-source-online-v1' as const;
export const SDK = Object.freeze({engine: '@earendil-works/pi-coding-agent', version: '1.4.0', adapter_contract: 'host-run-receipt-v1'});
export const DISCOVERY_SESSION_MS = 900000;
export const LIMITS = Object.freeze({frame: 262144, total: 8388608, frames: 256, depth: 32, stderr: 262144, handshake: 3000, partial: 5000, session: 45000});
export const NAMES = Object.freeze(['v4_tasks_list_mine_v1', 'v4_knowledge_search_v1', 'v4_catalog_search_v1', 'v4_memory_search_v1']);
export const MEMORY_NAMES = Object.freeze([...NAMES, 'v4_memory_save_v1', 'v4_memory_correct_v1', 'v4_memory_withdraw_v1']);
export const DISCOVERY_NAMES = Object.freeze([...MEMORY_NAMES, 'tool_search']);
export const SOURCE_NAMES = Object.freeze([...DISCOVERY_NAMES, 'v4_guanjia_read_prices_v1', 'v4_guanjia_read_stock_v1', 'v4_guanjia_sales_ranking_v1', 'v4_guanjia_sales_ranking_v2', 'v4_guanjia_local_sales_v1', 'v4_guanjia_category_discovery_v1', 'v4_guanjia_category_products_v1', 'v4_guanjia_category_products_status_v1', 'v4_research_search_v1', 'v4_research_read_v1', 'v4_research_describe_v1', 'v4_research_propose_v1', 'v4_research_probe_v1']);
export type Json = null | boolean | number | string | Json[] | {[key: string]: Json};
export type JsonObject = {[key: string]: Json};
export type Identity = {nonce: string; run_id: string; epoch: string};
export type Frame = Identity & {protocol: typeof STREAM_PROTOCOL | typeof PROTOCOL | typeof ONLINE_PROTOCOL | typeof MEMORY_PROTOCOL | typeof MEMORY_ONLINE_PROTOCOL | typeof DISCOVERY_PROTOCOL | typeof DISCOVERY_ONLINE_PROTOCOL | typeof SOURCE_PROTOCOL | typeof SOURCE_ONLINE_PROTOCOL; seq: number; type: string; reply_to: number | null; body: JsonObject};
export type Tool = {name: string; version: string; description: string; parameters: JsonObject};
export type Start = {tool_profile: typeof PROFILE | typeof MEMORY_PROFILE | typeof DISCOVERY_PROFILE | typeof SOURCE_PROFILE; toolset_version: string; tools: Tool[]; prompt: string; max_output_tokens?: number};
export class ProtocolError extends Error {
  constructor(readonly code: string) { super(code); this.name = 'ProtocolError'; }
}
export function fail(code: string): never { throw new ProtocolError(code); }
export function object(value: unknown): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
}
export function exact(value: unknown, keys: readonly string[]): JsonObject {
  if (!object(value) || Object.keys(value).length !== keys.length || keys.some(k => !Object.hasOwn(value, k))) fail('fields');
  return value;
}
export function get(value: JsonObject, key: string): Json {
  const item = value[key]; if (item === undefined) fail('fields'); return item;
}
export function integer(value: unknown, low = 0, high = Number.MAX_SAFE_INTEGER): asserts value is number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < low || value > high) fail('integer');
}
export function identifier(value: unknown, max = 128): asserts value is string {
  if (typeof value !== 'string' || value.length < 1 || value.length > max || !/^[A-Za-z0-9_.:-]+$/.test(value)) fail('identifier');
}
export function nativeId(value: unknown): asserts value is string {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(value)) fail('native_call_id');
}
export function digest(value: unknown, prefixed = false): asserts value is string {
  if (typeof value !== 'string' || !(prefixed ? /^sha256:[a-f0-9]{64}$/ : /^[a-f0-9]{64}$/).test(value)) fail('digest');
}
export function nameVersion(name: unknown, version: unknown, memory = false, discovery = false, source = false): void {
  if (typeof name !== 'string' || !(source ? SOURCE_NAMES : discovery ? DISCOVERY_NAMES : memory ? MEMORY_NAMES : NAMES).includes(name) || version !== (name === 'v4_guanjia_local_sales_v1' ? '3' : name === 'v4_guanjia_sales_ranking_v2' ? '2' : '1')) fail('tool_profile');
}
export function shape(value: unknown, depth = 0, finiteNumbers = false): asserts value is Json {
  if (Array.isArray(value) || object(value)) {
    if (depth + 1 > LIMITS.depth) fail('depth');
    for (const child of Array.isArray(value) ? value : [...Object.keys(value), ...Object.values(value)]) shape(child, depth + 1, finiteNumbers);
  } else if (typeof value === 'string') {
    for (let i = 0; i < value.length; i++) {
      const c = value.charCodeAt(i);
      if (c >= 0xd800 && c <= 0xdbff) {
        const next = value.charCodeAt(++i); if (!(next >= 0xdc00 && next <= 0xdfff)) fail('surrogate');
      } else if (c >= 0xdc00 && c <= 0xdfff) fail('surrogate');
    }
  } else if (typeof value === 'number') {
    if (finiteNumbers) { if (!Number.isFinite(value)) fail('non_finite_number'); }
    else integer(value, -Number.MAX_SAFE_INTEGER);
  }
  else if (value !== null && typeof value !== 'boolean') fail('json_type');
}
/** Decode before object assignment, rejecting duplicate decoded keys and floats. */
function decodeJson(raw: Buffer, finiteNumbers = false): Json {
  if (!Buffer.isBuffer(raw) || !raw.length || raw.length + 1 > LIMITS.frame) fail('frame_bytes');
  if (raw.subarray(0, 3).equals(Buffer.from([239, 187, 191]))) fail('bom');
  let text: string;
  try { text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(raw); } catch { return fail('utf8'); }
  let i = 0;
  const char = () => text[i] ?? '';
  const ws = () => { while (i < text.length && ' \t\r\n'.includes(char())) i++; };
  const string = (): string => {
    const start = i++; let escaped = false;
    while (i < text.length) {
      const c = text[i++]!;
      if (escaped) { escaped = false; continue; }
      if (c === '\\') { escaped = true; continue; }
      if (c === '"') {
        let v: unknown; try { v = JSON.parse(text.slice(start, i)); } catch { return fail('json'); }
        if (typeof v !== 'string') fail('json'); shape(v); return v;
      }
      if (c.charCodeAt(0) < 32) fail('json');
    }
    return fail('json');
  };
  const value = (depth: number): Json => {
    ws(); const c = char(); if (c === '"') return string();
    if (c === '{' || c === '[') {
      if (depth + 1 > LIMITS.depth) fail('depth');
      i++; ws(); const array = c === '[', end = array ? ']' : '}';
      const list: Json[] = [], record: JsonObject = {}, seen = new Set<string>();
      if (char() === end) { i++; return array ? list : record; }
      while (true) {
        if (array) list.push(value(depth + 1));
        else {
          ws(); if (char() !== '"') fail('json'); const key = string();
          if (seen.has(key)) fail('duplicate_key'); seen.add(key); ws();
          if (text[i++] !== ':') fail('json');
          Object.defineProperty(record, key, {value: value(depth + 1), enumerable: true, writable: true, configurable: true});
        }
        ws(); const next = text[i++]; if (next === end) return array ? list : record;
        if (next !== ',') fail('json');
      }
    }
    for (const [token, literal] of [['true', true], ['false', false], ['null', null]] as const) {
      if (text.startsWith(token, i)) { i += token.length; return literal; }
    }
    const start = i; if (char() === '-') i++;
    if (char() === '0') i++;
    else if (char() >= '1' && char() <= '9') { while (char() >= '0' && char() <= '9') i++; }
    else fail('json');
    if (char() === '.' || char() === 'e' || char() === 'E') {
      if (!finiteNumbers) fail('non_integer_json_number');
      const token = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(text.slice(start));
      if (!token) fail('json');
      i = start + token[0].length;
      const n = Number(token[0]); if (!Number.isFinite(n)) fail('non_finite_number'); return n;
    }
    const n = Number(text.slice(start, i)); if (!Number.isSafeInteger(n)) fail('unsafe_integer'); return n;
  };
  const result = value(0); ws(); if (i !== text.length) fail('json'); shape(result, 0, finiteNumbers); return result;
}
export function decode(raw: Buffer): Json { return decodeJson(raw); }
export function encode(value: unknown): Buffer {
  shape(value); const bytes = Buffer.from(JSON.stringify(value) + '\n');
  if (bytes.length > LIMITS.frame) fail('frame_bytes'); return bytes;
}
export class Framer {
  constructor(private readonly streaming = false) {}
  private buffer: Buffer = Buffer.alloc(0);
  private started: number | null = null;
  private total = 0; private count = 0; private closed = false;
  check(at = performance.now()): void {
    if (this.closed) fail('framer_closed');
    if (this.started !== null && at - this.started >= LIMITS.partial) fail('partial_deadline');
  }
  feed(data: Buffer, at = performance.now()): Json[] {
    this.check(at); if (!Buffer.isBuffer(data)) fail('bytes_required');
    this.total += data.length; if (this.total > LIMITS.total) fail('total_bytes');
    const out: Json[] = []; let offset = 0;
    while (offset < data.length) {
      if (this.started === null) this.started = at;
      let end = data.indexOf(10, offset); end = end < 0 ? data.length : end + 1;
      const part = data.subarray(offset, end); offset = end;
      if (this.buffer.length + part.length > LIMITS.frame) fail('frame_bytes');
      this.buffer = Buffer.concat([this.buffer, part]);
      if (part.at(-1) === 10) {
        if (++this.count > (this.streaming ? STREAM_FRAMES : LIMITS.frames)) fail('frame_count');
        out.push(decode(this.buffer.subarray(0, -1))); this.buffer = Buffer.alloc(0); this.started = null;
      }
    }
    return out;
  }
  eof(): void { if (this.buffer.length) fail('partial_eof'); this.closed = true; }
}
const OPS = ['model_begin', 'model_complete', 'tool_request'];
const CODES = ['host_operation_failed', 'protocol_error', 'runtime_failed'];
function validateWire(value: unknown, online = false, memory = false, discovery = false, source = false): asserts value is Frame {
  if (source && (!memory || !discovery)) fail('tool_profile');
  const f = exact(value, ['protocol', 'nonce', 'run_id', 'epoch', 'seq', 'type', 'reply_to', 'body']);
  const protocol = source ? (online ? SOURCE_ONLINE_PROTOCOL : SOURCE_PROTOCOL) : discovery ? (online ? DISCOVERY_ONLINE_PROTOCOL : DISCOVERY_PROTOCOL) : memory ? (online ? MEMORY_ONLINE_PROTOCOL : MEMORY_PROTOCOL) : (online ? ONLINE_PROTOCOL : PROTOCOL);
  const helloProfile = source ? (online ? SOURCE_ONLINE_PROFILE : SOURCE_PROFILE) : discovery ? (online ? DISCOVERY_ONLINE_PROFILE : DISCOVERY_PROFILE) : memory ? (online ? MEMORY_ONLINE_PROFILE : MEMORY_PROFILE) : (online ? ONLINE_PROFILE : PROFILE);
  const names = source ? SOURCE_NAMES : discovery ? DISCOVERY_NAMES : memory ? MEMORY_NAMES : NAMES;
  if (f['protocol'] !== protocol) fail('protocol');
  identifier(f['run_id']);
  if (typeof f['nonce'] !== 'string' || !/^[0-9a-f]{32}$/.test(f['nonce'])) fail('nonce');
  if (typeof f['epoch'] !== 'string' || !/^(0|[1-9][0-9]{0,29})$/.test(f['epoch'])) fail('epoch');
  integer(f['seq'], 1, LIMITS.frames); if (f['reply_to'] !== null) integer(f['reply_to'], 1, LIMITS.frames);
  const type = f['type']; const b = f['body'];
  if (type === 'hello' || type === 'ready') {
    const body = exact(b, ['sdk', 'profile']); const sdk = exact(body['sdk'], Object.keys(SDK));
    if (Object.entries(SDK).some(([k, v]) => sdk[k] !== v) || body['profile'] !== helloProfile) fail('sdk_identity');
  } else if (type === 'start') {
    const hasOutputLimit = online && object(b) && 'max_output_tokens' in b;
    const body = exact(b, ['tool_profile', 'toolset_version', 'tools', 'prompt', ...(hasOutputLimit ? ['max_output_tokens'] : [])]);
    if (hasOutputLimit) integer(body['max_output_tokens'],1,discovery ? 8192 : 2048);
    if (body['tool_profile'] !== (source ? SOURCE_PROFILE : discovery ? DISCOVERY_PROFILE : memory ? MEMORY_PROFILE : PROFILE)) fail('tool_profile'); digest(body['toolset_version'], true);
    if (typeof body['prompt'] !== 'string' || body['prompt'].length < 1) fail('start');
    const tools = body['tools']; if (!Array.isArray(tools) || tools.length > names.length) fail('tool_profile');
    const seen = new Set();
    for (const item of tools) {
      const t = exact(item, ['name', 'version', 'description', 'parameters']); nameVersion(t['name'], t['version'], memory, discovery, source);
      if (seen.has(t['name']) || typeof t['description'] !== 'string' || !object(t['parameters'])) fail('tool_profile'); seen.add(t['name']);
    }
  } else if (type === 'request') {
    const body = exact(b, ['op', 'arguments']); const op = body['op'];
    if (op === 'provider_call' && online) {
      providerRequest(body['arguments']);
    } else if (op === 'model_begin') {
      const a = exact(body['arguments'], ['purpose', 'input_digest', 'toolset_version', ...(online ? ['request_digest'] : [])]);
      if (typeof a['purpose'] !== 'string' || !['inference', 'retry', 'compaction'].includes(a['purpose'])) fail('purpose');
      digest(a['input_digest']); digest(a['toolset_version'], true);
      if (online) digest(a['request_digest']);
    } else if (op === 'model_complete') {
      const a = exact(body['arguments'], ['attempt_id', 'permit', 'step_id', 'message']);
      for (const k of ['attempt_id', 'permit', 'step_id']) identifier(a[k]); if (a['attempt_id'] !== a['step_id']) fail('attempt_binding');
      const m = exact(a['message'], ['role', 'content', 'usage', 'stopReason']);
      if (m['role'] !== 'assistant' || !Array.isArray(m['content']) || typeof m['stopReason'] !== 'string' || !['toolUse', 'stop', 'error', 'aborted'].includes(m['stopReason'])) fail('message');
      const usage = exact(m['usage'], ['input', 'output', ...(online ? ['cacheRead', 'cacheWrite'] : [])]); for (const n of Object.values(usage)) integer(n);
    } else if (op === 'tool_request') {
      const isSearch = discovery && object(body['arguments']) && body['arguments']['name'] === 'tool_search';
      const a = exact(body['arguments'], ['call_id', 'name', 'version', 'toolset_version', 'arguments', ...(isSearch ? ['selection'] : [])]);
      if (isSearch && (!Array.isArray(a['selection']) || a['selection'].length > 100 || a['selection'].some(name=>typeof name !== 'string' || name === 'tool_search' || !names.includes(name)))) fail('tool_profile');
      nativeId(a['call_id']); nameVersion(a['name'], a['version'], memory, discovery, source); digest(a['toolset_version'], true);
      if (!object(a['arguments'])) fail('arguments');
    } else fail('unsupported_operation');
  } else if (type === 'response') {
    if (f['reply_to'] === null) fail('reply_required');
    const body = exact(b, ['op', 'ok', 'result', 'code']);
    if (typeof body['op'] !== 'string' || ![...OPS, ...(online ? ['provider_call'] : [])].includes(body['op']) || typeof body['ok'] !== 'boolean') fail('response');
    if (body['ok']) {
      if (body['code'] !== null || !object(body['result'])) fail('response');
      if (body['op'] === 'provider_call' && online) {
        providerResponse(body['result']);
      } else if (body['op'] === 'model_begin') {
        const r = exact(body['result'], ['attempt_id', 'step_id', 'permit', 'limits']);
        for (const k of ['attempt_id', 'step_id', 'permit']) identifier(r[k]); if (r['attempt_id'] !== r['step_id']) fail('attempt_binding');
        const limits = exact(r['limits'], ['input', 'output']); for (const n of Object.values(limits)) integer(n, 1);
      } else if (body['op'] === 'model_complete') {
        const r = exact(body['result'], ['committed', 'finished', ...(online && object(body['result']) && 'argument_rejection' in body['result'] ? ['argument_rejection'] : [])]); if (r['committed'] !== true || typeof r['finished'] !== 'boolean') fail('commit_receipt');
      } else {
        const r = exact(body['result'], ['call_id', 'name', 'version', 'toolset_version', 'receipt_id', 'step_id', 'output_ref', 'result']);
        nativeId(r['call_id']); nameVersion(r['name'], r['version'], memory, discovery, source); digest(r['toolset_version'], true); digest(r['output_ref'], true);
        identifier(r['receipt_id']); identifier(r['step_id']); if (!object(r['result'])) fail('result');
      }
    } else if (body['result'] !== null || typeof body['code'] !== 'string' || !CODES.includes(body['code'])) fail('error_code');
  } else if (type === 'session_end') {
    const body = exact(b, ['status']); if (body['status'] !== 'ok' && body['status'] !== 'error') fail('status');
  } else if (type === 'error') {
    const body = exact(b, ['code']); if (typeof body['code'] !== 'string' || !CODES.includes(body['code'])) fail('error_code');
  } else fail('unsupported_type');
  shape(f);
}
export function validate(value: unknown): asserts value is Frame { validateWire(value); }
export function validateOnline(value: unknown): asserts value is Frame { validateWire(value, true); }
const FORBIDDEN_PROVIDER_KEYS = /^(url|base_?url|endpoint|headers?|authorization|api[_-]?key|key|proxy|provider)$/i;
function providerBody(value: Json): void {
  if (Array.isArray(value)) { for (const child of value) providerBody(child); }
  else if (object(value)) for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN_PROVIDER_KEYS.test(key)) fail('provider_control'); providerBody(child);
  }
}
function providerRequest(value: unknown): void {
  const a = exact(value, ['attempt_id', 'step_id', 'permit', 'input_digest', 'request_digest', 'request']);
  for (const key of ['attempt_id', 'step_id', 'permit']) identifier(a[key]);
  if (a['attempt_id'] !== a['step_id']) fail('attempt_binding'); digest(a['input_digest']); digest(a['request_digest']);
  const r = exact(a['request'], ['method', 'path', 'body']);
  if (r['method'] !== 'POST' || r['path'] !== PROVIDER_PATH || typeof r['body'] !== 'string') fail('provider_request');
  shape(r['body']);
  if (Buffer.byteLength(r['body'] as string) > PROVIDER_BYTES) fail('provider_bytes');
  const parsed = decodeJson(Buffer.from(r['body'] as string), true);
  if (!object(parsed)) fail('provider_request'); providerBody(parsed);
}
function providerResponse(value: unknown): void {
  const r = exact(value, ['status', 'request_id', 'body']); integer(r['status'], 200, 599);
  if (r['request_id'] !== null) identifier(r['request_id']);
  if (typeof r['body'] !== 'string' || Buffer.byteLength(r['body']) > PROVIDER_BYTES) fail('provider_bytes');
}
export class Peer {
  sent = 0; received = 0; phase = 'new'; private pending: [number, string] | null = null; private sentBytes = 0; streamState: ProviderStreamState | null = null;
  private streamAnchor:{identity:Identity;seq:number;state:ProviderStreamState}|null=null;
  private lastCancel:'active'|'late_complete'|null=null;
  get cancelClassification():'active'|'late_complete'|null{return this.lastCancel;}
  clearStreamAnchor():void{this.streamAnchor=null;this.lastCancel=null;}
  constructor(readonly role: 'host' | 'sdk', public identity: Identity | null = null, private readonly online = false, private readonly memory = false, private readonly discovery = false, private readonly source = false, readonly streaming = false, readonly canonicalResults = false) {}
  private transition(f: Frame, sending: boolean): void {
    const sender = sending ? this.role : this.role === 'host' ? 'sdk' : 'host'; const t = f.type; const b = f.body;
    if (this.streaming && t.startsWith('provider_')) {
      const anchor=this.streamAnchor,state=this.streamState;
      if(!anchor||!state||anchor.state!==state||anchor.seq!==f.reply_to
          ||anchor.identity.nonce!==f.nonce||anchor.identity.run_id!==f.run_id||anchor.identity.epoch!==f.epoch)fail('stream_pending');
      const late=t==='provider_cancel'&&state.completed;
      if(!late&&(this.pending?.[1]!=='provider_call'||f.reply_to!==this.pending[0]))fail('stream_pending');
      const classification=state.event(t,f.body,sender);if(t==='provider_cancel')this.lastCancel=classification??null;return;
    }
    if (this.streaming && t === 'response' && b['op'] === 'provider_call') this.streamState?.finish(b);
    if (t !== 'response' && f.reply_to !== null) fail('unexpected_reply');
    if (t === 'hello' && sender === 'host' && this.phase === 'new') this.phase = 'hello';
    else if (t === 'ready' && sender === 'sdk' && this.phase === 'hello') this.phase = 'ready';
    else if (t === 'start' && sender === 'host' && this.phase === 'ready') this.phase = 'running';
    else if (t === 'request' && sender === 'sdk' && this.phase === 'running' && this.pending === null) this.pending = [f.seq, String(f.body['op'])];
    else if (t === 'response' && sender === 'host' && this.phase === 'running' && this.pending?.[0] === f.reply_to && this.pending?.[1] === f.body['op']) this.pending = null;
    else if (t === 'session_end' && sender === 'sdk' && this.phase === 'running' && this.pending === null) this.phase = 'ended';
    else if (t === 'error' && sender === 'sdk' && ['ready', 'running'].includes(this.phase)) this.phase = 'failed';
    else fail('phase_direction_correlation');
    if(this.streaming&&['request','session_end','error'].includes(t)){
      this.clearStreamAnchor();
      if(t==='request'&&b['op']==='provider_call'){const state=new ProviderStreamState(b['arguments'] as JsonObject);this.streamState=state;this.streamAnchor={identity:{nonce:f.nonce,run_id:f.run_id,epoch:f.epoch},seq:f.seq,state};}
    }
  }
  receive(value: unknown): Frame {
    this.lastCancel=null;
    (this.canonicalResults ? validateCanonical : this.streaming ? validateStream : (v:unknown) => validateWire(v, this.online, this.memory, this.discovery, this.source))(value); const frame = value as Frame; const id = {nonce: frame.nonce, run_id: frame.run_id, epoch: frame.epoch};
    if (this.identity === null) { if (this.role !== 'sdk' || frame.type !== 'hello') fail('hello_required'); }
    else if (id.nonce !== this.identity.nonce || id.run_id !== this.identity.run_id || id.epoch !== this.identity.epoch) fail('identity');
    if (frame.seq !== this.received + 1) fail('sequence');
    this.transition(frame, false); this.identity = id; this.received = frame.seq; return frame;
  }
  send(type: string, body: JsonObject, reply_to: number | null = null): Buffer {
    this.lastCancel=null;
    if (this.identity === null) fail('identity_required');
    const f = {protocol: this.streaming ? STREAM_PROTOCOL : this.source ? (this.online ? SOURCE_ONLINE_PROTOCOL : SOURCE_PROTOCOL) : this.discovery ? (this.online ? DISCOVERY_ONLINE_PROTOCOL : DISCOVERY_PROTOCOL) : this.memory ? (this.online ? MEMORY_ONLINE_PROTOCOL : MEMORY_PROTOCOL) : (this.online ? ONLINE_PROTOCOL : PROTOCOL), ...this.identity, seq: this.sent + 1, type, reply_to, body}; (this.canonicalResults ? validateCanonical : this.streaming ? validateStream : (v:unknown) => validateWire(v, this.online, this.memory, this.discovery, this.source))(f);
    const raw = encode(f); if (this.sentBytes + raw.length > LIMITS.total) fail('total_bytes');
    this.transition(f, true); this.sent++; this.sentBytes += raw.length; return raw;
  }
}

/** Explicit opt-in only: default Peer remains the synthetic v1 grammar. */
export class OnlinePeer extends Peer {
  constructor(role: 'host' | 'sdk', identity: Identity | null = null) { super(role, identity, true); }
}

export function validateMemory(value: unknown): asserts value is Frame { validateWire(value, false, true); }
export function validateMemoryOnline(value: unknown): asserts value is Frame { validateWire(value, true, true); }
export class MemoryPeer extends Peer {
  constructor(role: 'host' | 'sdk', identity: Identity | null = null, online = false) { super(role, identity, online, true); }
}

export function validateDiscovery(value: unknown): asserts value is Frame { validateWire(value, false, true, true); }
export function validateDiscoveryOnline(value: unknown): asserts value is Frame { validateWire(value, true, true, true); }
export class DiscoveryPeer extends Peer {
  constructor(role:'host'|'sdk', identity:Identity|null=null, online=false) { super(role, identity, online, true, true); }
}

export function validateSource(value: unknown): asserts value is Frame { validateWire(value, false, true, true, true); }
export class SourcePeer extends Peer {
  constructor(role:'host'|'sdk', identity:Identity|null=null, online=false) { super(role, identity, online, true, true, true); }
}

export function validateSourceOnline(value: unknown): asserts value is Frame { validateWire(value, true, true, true, true); }


export function validateStream(value: unknown): asserts value is Frame {
  const f = exact(value,['protocol','nonce','run_id','epoch','seq','type','reply_to','body']);
  if (f['protocol'] !== STREAM_PROTOCOL) fail('protocol');
  integer(f['seq'],1,STREAM_FRAMES); if (f['reply_to'] !== null) integer(f['reply_to'],1,STREAM_FRAMES);
  const mapped: JsonObject = {...f,protocol:ONLINE_PROTOCOL,seq:1,reply_to:f['reply_to'] === null ? null : 1};
  const type=f['type'];
  if (typeof type === 'string' && type.startsWith('provider_')) {
    // Identity/depth/number rules are identical; validate the outer shell via
    // a harmless old grammar frame, then validate exact event keys below.
    validateWire({...mapped,type:'response',reply_to:1,body:{op:'provider_call',ok:false,result:null,code:'host_operation_failed'}},true);
    const keys:Record<string,string[]>={provider_open:['stream_id','attempt_id','step_id','request_digest','status','request_id'],provider_chunk:['stream_id','chunk_seq','data_b64'],provider_end:['stream_id','last_chunk_seq','byte_count','body_sha256'],provider_abort:['stream_id','code','send_started'],provider_credit:['stream_id','through_chunk_seq','bytes_consumed'],provider_cancel:['stream_id','reason']};
    if (!keys[type]) fail('unsupported_type'); const b=exact(f['body'],keys[type]!); identifier(b['stream_id']);
    if (type === 'provider_open') {identifier(b['attempt_id']);identifier(b['step_id']);digest(b['request_digest']);if(b['status']!==200)fail('provider_status');if(b['request_id']!==null)identifier(b['request_id']);}
    if (type === 'provider_chunk') {integer(b['chunk_seq'],1,STREAM_FRAMES);streamBytes(b['data_b64']);}
    if (type === 'provider_end') {integer(b['last_chunk_seq'],0,STREAM_FRAMES);integer(b['byte_count'],0,PROVIDER_BYTES);digest(b['body_sha256']);}
    if (type === 'provider_credit') {integer(b['through_chunk_seq'],1,STREAM_FRAMES);integer(b['bytes_consumed'],1,PROVIDER_BYTES);}
    if (type === 'provider_abort' && (!['cancelled','deadline','transport_invalid','backpressure'].includes(String(b['code'])) || typeof b['send_started']!=='boolean'))fail('stream_abort');
    if (type === 'provider_cancel' && !['explicit_cancel','runtime_abort'].includes(String(b['reason'])))fail('stream_cancel');
    if (f['reply_to']===null)fail('reply_required'); shape(f);return;
  }
  if ((type==='hello'||type==='ready') && object(f['body'])) {
    if (f['body']['profile']!==STREAM_PROFILE) fail('sdk_identity'); mapped['body']={...f['body'],profile:ONLINE_PROFILE};
  }
  if (type==='response' && object(f['body']) && f['body']['op']==='provider_call' && f['body']['ok']===true) {
    streamSummary(f['body']['result']); mapped['body']={...f['body'],result:{status:200,request_id:null,body:''}};
  }
  validateWire(mapped,true);shape(f);
}
export function streamBytes(value:unknown):Buffer {
  if(typeof value!=='string'||value.length>5464)fail('stream_base64');const bytes=Buffer.from(value,'base64');
  if(!bytes.length||bytes.length>4096||bytes.toString('base64')!==value)fail('stream_base64');return bytes;
}
export function streamSummary(value:unknown):JsonObject {
 const r=exact(value,['status','request_id','body_sha256','byte_count','usage','response_recorded']);
 if(r['status']!==200||r['response_recorded']!==true)fail('stream_receipt');if(r['request_id']!==null)identifier(r['request_id']);digest(r['body_sha256']);integer(r['byte_count'],0,PROVIDER_BYTES);
 const usage=exact(r['usage'],['input','output','cacheRead','cacheWrite']);for(const n of Object.values(usage))integer(n);return r;
}
export class ProviderStreamState {
 readonly id:Json;private opened=false;private ended=false;private aborted=false;private sizes:number[]=[];private ack=0;private consumed=0;private total=0;private hash=createHash('sha256');private finalHash='';private requestId:Json=null;private lateCancelled=false;
 constructor(private readonly args:JsonObject){this.id=args['attempt_id']??null;}
 get available():number{return 8192-this.total+this.consumed;}
 get completed():boolean{return this.ended&&!this.aborted;}
 event(type:string,b:JsonObject,sender:string):'active'|'late_complete'|undefined {
  if(b['stream_id']!==this.id)fail('stream_binding');
  if(type==='provider_credit') {if(sender!=='sdk'||!this.opened||this.aborted)fail('stream_direction');const through=b['through_chunk_seq'] as number;if(through<=this.ack||through>this.sizes.length)fail('stream_credit');const count=this.sizes.slice(0,through).reduce((a,n)=>a+n,0);if(count!==b['bytes_consumed'])fail('stream_credit');this.ack=through;this.consumed=count;return;}
  if(type==='provider_cancel'){if(sender!=='sdk'||this.aborted)fail('stream_direction');if(this.ended){if(this.lateCancelled)fail('stream_cancel_replay');this.lateCancelled=true;return 'late_complete';}return 'active';}
  if(sender!=='host'||this.ended||this.aborted)fail('stream_direction');
  if(type==='provider_abort'){this.aborted=true;return;}
  if(type==='provider_open'){if(this.opened||b['attempt_id']!==this.id||b['step_id']!==this.id||b['request_digest']!==this.args['request_digest'])fail('stream_binding');this.opened=true;this.requestId=b['request_id']!;return;}
  if(!this.opened)fail('stream_open_required');
  if(type==='provider_chunk'){const raw=streamBytes(b['data_b64']);if(b['chunk_seq']!==this.sizes.length+1||raw.length>this.available||this.total+raw.length>PROVIDER_BYTES)fail('stream_chunk');this.sizes.push(raw.length);this.total+=raw.length;this.hash.update(raw);return;}
  if(type==='provider_end'){this.finalHash=this.hash.digest('hex');if(b['last_chunk_seq']!==this.sizes.length||b['byte_count']!==this.total||b['body_sha256']!==this.finalHash)fail('stream_digest');this.ended=true;return;}
  fail('unsupported_type');
 }
 finish(b:JsonObject):void {if(b['ok']===false){if(!this.aborted)fail('stream_terminal');return;}const r=streamSummary(b['result']);if(!this.ended||this.aborted||r['body_sha256']!==this.finalHash||r['byte_count']!==this.total||r['request_id']!==this.requestId)fail('stream_terminal');}
}
export class StreamPeer extends Peer {constructor(role:'host'|'sdk',identity:Identity|null=null){super(role,identity,true,false,false,false,true);}}


export function canonicalEnvelope(value:unknown):JsonObject {
 const e=exact(value,['record_version','result_abi','runtime_record','scope','binding','result_kind','payload','payload_sha256','output_ref','source_refs','source_refs_sha256','captured_at']);shape(e);
 if(e['record_version']!==1||e['result_abi']!==RESULT_ABI)fail('canonical_abi');
 const record=exact(e['runtime_record'],['contract_version','wire_protocol','launch_profile','source_manifest_sha256']);
 if(record['contract_version']!==3||record['wire_protocol']!==STREAM_PROTOCOL||!['host-pi-readonly-node24-darwin-online-stream-result-v1','host-pi-readonly-node24-linux-online-stream-result-v1'].includes(String(record['launch_profile'])))fail('canonical_profile');digest(record['source_manifest_sha256']);
 const scope=exact(e['scope'],['organization_id','store_id','requester_id','agent_id']);for(const v of Object.values(scope))identifier(v,36);
 const b=exact(e['binding'],['run_id','model_step_id','tool_step_id','pi_receipt_id','native_call_id','tool_key','tool_name','tool_version','toolset_version','input_digest','execution_epoch']);
 for(const key of ['run_id','model_step_id','tool_step_id','pi_receipt_id'])identifier(b[key],36);nativeId(b['native_call_id']);integer(b['execution_epoch'],1);digest(b['toolset_version'],true);digest(b['input_digest']);
 const names:Record<string,string>={'tasks.list_mine':'v4_tasks_list_mine_v1','knowledge.search':'v4_knowledge_search_v1','catalog.search':'v4_catalog_search_v1','memory.search':'v4_memory_search_v1'};
 if(typeof b['tool_key']!=='string'||names[b['tool_key']]!==b['tool_name']||b['tool_version']!=='1')fail('canonical_tool');
 if(!object(e['payload'])||!Array.isArray(e['source_refs'])||e['source_refs'].some(v=>!object(v)))fail('canonical_payload');
 if(!['success','tool_error'].includes(String(e['result_kind'])))fail('canonical_kind');
 if(e['result_kind']==='tool_error'){
  const payload=exact(e['payload'],['code','field','hint']);
  if(b['tool_key']!=='memory.search'||payload['code']!=='memory_query_invalid'||payload['field']!=='query'||payload['hint']!=='请使用1至256个有效字符和不超过16个查询词，移除空字符后重试。'||e['source_refs'].length)fail('canonical_error_payload');
 }
 digest(e['payload_sha256']);digest(e['source_refs_sha256']);digest(e['output_ref'],true);
 if(e['output_ref']!=='sha256:'+e['payload_sha256']||typeof e['captured_at']!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(e['captured_at'])||!Number.isFinite(Date.parse(e['captured_at'])))fail('canonical_binding');
 if(Buffer.byteLength(JSON.stringify(e))>LIMITS.frame)fail('canonical_bytes');return e;
}
export function validateCanonical(value:unknown):asserts value is Frame{
 const f=exact(value,['protocol','nonce','run_id','epoch','seq','type','reply_to','body']);const mapped:JsonObject=structuredClone(f);
 if(f['type']==='hello'||f['type']==='ready'){
  if(!object(f['body'])||f['body']['profile']!==RESULT_PROFILE)fail('sdk_identity');mapped['body']={...f['body'],profile:STREAM_PROFILE};
 }
 if(f['type']==='response'&&object(f['body'])&&f['body']['op']==='tool_request'&&f['body']['ok']===true){
  const r=exact(f['body']['result'],['call_id','name','version','toolset_version','receipt_id','step_id','output_ref','canonical_ref','result']);digest(r['canonical_ref'],true);const e=canonicalEnvelope(r['result']),b=e['binding'] as JsonObject;
  if(b['run_id']!==f['run_id']||String(b['execution_epoch'])!==f['epoch']||b['native_call_id']!==r['call_id']||b['tool_name']!==r['name']||b['tool_version']!==r['version']||b['toolset_version']!==r['toolset_version']||b['pi_receipt_id']!==r['receipt_id']||b['tool_step_id']!==r['step_id']||e['output_ref']!==r['output_ref'])fail('canonical_binding');
  const old:JsonObject={...r};delete old['canonical_ref'];old['result']=e['payload']!;mapped['body']={...f['body'],result:old};
 }
 validateStream(mapped);shape(f);
}
export class CanonicalPeer extends Peer {constructor(role:'host'|'sdk',identity:Identity|null=null){super(role,identity,true,false,false,false,true,true);}}
