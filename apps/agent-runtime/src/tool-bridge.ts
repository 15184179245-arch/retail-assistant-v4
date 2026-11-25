/** SDK tools are descriptions and a Host gateway, never local handlers. */
import {createHash} from 'node:crypto';
import {WORKBENCH_PROFILE, PROFILE, MEMORY_PROFILE, DISCOVERY_PROFILE, SOURCE_PROFILE, exact, get, nameVersion, nativeId, identifier, digest, object, shape, ProtocolError,RESULT_ABI,canonicalEnvelope} from './protocol.js';
import type {Json, JsonObject, Start, Tool,Identity} from './protocol.js';

export interface HostChannel {
  readonly error: Error | null;
  readonly resultABI?:string|undefined;
  readonly resultRecord?:JsonObject|undefined;
  readonly resultIdentity?:Identity|null|undefined;
  request(op: 'model_begin' | 'model_complete' | 'tool_request', args: JsonObject): Promise<JsonObject>;
  fail(error: Error): void;
}

function compareCodepoints(left: string, right: string): number {
  const a = [...left], b = [...right];
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const diff = a[i]!.codePointAt(0)! - b[i]!.codePointAt(0)!;
    if (diff) return diff;
  }
  return a.length - b.length;
}
/** Same ensure_ascii=False/sort_keys/separators contract used by Host. */
export function canonical(value: Json): string {
  shape(value);
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (object(value)) return '{' + Object.keys(value).sort(compareCodepoints)
    .map(key => JSON.stringify(key) + ':' + canonical(get(value, key))).join(',') + '}';
  return JSON.stringify(value);
}
export function sha256(value: Json): string {
  return createHash('sha256').update(canonical(value)).digest('hex');
}

const rejectedArguments = new WeakMap<HostChannel, Map<string, number>>();
/** Only a normally returned, Host-committed model completion may install this. */
export function installArgumentRejection(channel:HostChannel, feedback:JsonObject,
    step:string, pin:string, message:JsonObject):void {
  exact(feedback,['step_id','message_digest','toolset_version','calls']);
  if (feedback['step_id'] !== step || feedback['toolset_version'] !== pin
      || feedback['message_digest'] !== sha256(message) || !Array.isArray(feedback['calls'])
      || !Array.isArray(message['content']) || message['stopReason'] !== 'toolUse') throw new ProtocolError('commit_receipt');
  const calls = message['content'].filter(item=>object(item) && item['type']==='toolCall') as JsonObject[];
  if (!calls.length || calls.length !== feedback['calls'].length || calls.some(call=>call['name']==='tool_search')) throw new ProtocolError('commit_receipt');
  const blocked = new Map<string,number>();
  for (let i=0;i<calls.length;i++) {
    const call=calls[i]!, denial=feedback['calls'][i];
    exact(denial,['call_id','name','code']);
    if (!object(denial) || denial['call_id'] !== call['id'] || denial['name'] !== call['name']
        || denial['code'] !== 'tool_arguments_schema_invalid' || !object(call['arguments'])) throw new ProtocolError('commit_receipt');
    const key=String(call['name'])+':'+sha256(call['arguments']);
    blocked.set(key,(blocked.get(key) ?? 0)+1);
  }
  rejectedArguments.set(channel,blocked);
}

export class ToolGateway {
  private readonly tools: readonly Tool[];
  private readonly used = new Set<string>();
  private stopped = false;
  readonly pin: string;
  readonly discovery: boolean;
  private active: string[];
  constructor(start: Start, private readonly channel: HostChannel) {
    if (start.tool_profile !== WORKBENCH_PROFILE && start.tool_profile !== PROFILE && start.tool_profile !== MEMORY_PROFILE && start.tool_profile !== DISCOVERY_PROFILE && start.tool_profile !== SOURCE_PROFILE) throw new ProtocolError('tool_profile');
    digest(start.toolset_version, true); this.pin = start.toolset_version;
    this.tools = structuredClone(start.tools);
    this.discovery = start.tool_profile === WORKBENCH_PROFILE || start.tool_profile === DISCOVERY_PROFILE || start.tool_profile === SOURCE_PROFILE;
    this.active = this.discovery ? ['tool_search'] : this.names();
    const names = new Set<string>();
    for (const tool of this.tools) {
      nameVersion(tool.name, tool.version, start.tool_profile === MEMORY_PROFILE, this.discovery, start.tool_profile === SOURCE_PROFILE, start.tool_profile === WORKBENCH_PROFILE);
      if (names.has(tool.name) || !object(tool.parameters)) throw new ProtocolError('tool_profile');
      names.add(tool.name);
    }
    if (this.discovery && !names.has('tool_search')) throw new ProtocolError('tool_profile');
  }
  close(): void { this.stopped = true; }
  names(): string[] { return this.tools.map(t => t.name); }
  assertActive(names: readonly string[]): void {
    if (names.length !== this.active.length || names.some((name, i) => name !== this.active[i])) {
      this.close(); throw new ProtocolError('tool_profile');
    }
  }
  async commitDiscovery(id:string, args:JsonObject, selection:string[], signal?:AbortSignal):Promise<void> {
    if (!this.discovery) throw new ProtocolError('tool_profile');
    const tool = this.tools.find(t=>t.name==='tool_search');
    if (!tool || new Set(selection).size !== selection.length || selection.some(name=>this.active.includes(name) || !this.names().includes(name)))
      throw new ProtocolError('tool_profile');
    const reply=await this.execute(tool,id,args,signal,selection);
    const result:unknown=JSON.parse(reply.content[0]!.text);
    if (!object(result) || result['authority'] !== 'metadata_only' || result['catalog_digest'] !== this.pin
        || JSON.stringify(result['loaded']) !== JSON.stringify(selection)) throw new ProtocolError('host_receipt_binding');
    this.active.push(...selection);
  }
  descriptors(): {name: string; label: string; description: string; parameters: JsonObject;
    executionMode: 'sequential'; prepareArguments(raw: unknown): JsonObject;
    execute(id: string, args: JsonObject, signal?: AbortSignal): Promise<{content: {type: 'text'; text: string}[]; details: JsonObject;isError?:boolean}>}[] {
    return this.tools.map(tool => ({
      name: tool.name, label: tool.name, description: tool.description,
      parameters: structuredClone(tool.parameters), executionMode: 'sequential' as const,
      // Input schema is checked by SDK and again by Host's original registry.
      // No defaults/coercion or schema rewrites here.
      prepareArguments: (raw: unknown): JsonObject => {
        shape(raw); if (!object(raw)) throw new ProtocolError('arguments');
        const blocked = rejectedArguments.get(this.channel), key=tool.name+':'+sha256(raw);
        const count=blocked?.get(key) ?? 0;
        if (count) {
          blocked!.set(key,count-1);
          throw new Error('tool_arguments_schema_invalid: batch rejected; correct arguments to the declared schema');
        }
        return structuredClone(raw);
      },
      execute: (id: string, args: JsonObject, signal?: AbortSignal) => this.execute(tool, id, args, signal),
    }));
  }
  private async execute(tool: Tool, id: string, args: JsonObject, signal?: AbortSignal, selection?:string[]) {
    try {
      if (this.stopped || this.channel.error || signal?.aborted) throw new ProtocolError('dispatch_stopped');
      if (!this.active.includes(tool.name) || (tool.name==='tool_search') !== (selection !== undefined)) throw new ProtocolError('tool_not_loaded');
      nativeId(id); shape(args);
      if (!object(args) || this.used.has(id)) throw new ProtocolError('native_call_replay');
      // Once sent, an uncertain result never permits the same native call again.
      this.used.add(id);
      const result = await this.channel.request('tool_request', {
        call_id: id, name: tool.name, version: tool.version, toolset_version: this.pin,
        arguments: structuredClone(args), ...(selection !== undefined ? {selection:[...selection]} : {}),
      });
      const resultMode=this.channel.resultABI===RESULT_ABI;
      exact(result, ['call_id', 'name', 'version', 'toolset_version', 'receipt_id', 'step_id', 'output_ref', 'result',...(resultMode?['canonical_ref']:[])]);
      let payload=result['result'];let isError=false;
      if(resultMode){
        const envelope=canonicalEnvelope(payload),binding=envelope['binding'] as JsonObject;
        if(!this.channel.resultRecord||!this.channel.resultIdentity||canonical(envelope['runtime_record']!)!==canonical(this.channel.resultRecord)
          ||binding['run_id']!==this.channel.resultIdentity.run_id||String(binding['execution_epoch'])!==this.channel.resultIdentity.epoch
          ||binding['native_call_id']!==id||binding['tool_name']!==tool.name||binding['tool_version']!==tool.version||binding['toolset_version']!==this.pin
          ||binding['input_digest']!==sha256(args)||binding['pi_receipt_id']!==result['receipt_id']||binding['tool_step_id']!==result['step_id']
          ||envelope['output_ref']!==result['output_ref']||envelope['payload_sha256']!==sha256(envelope['payload']!)
          ||envelope['source_refs_sha256']!==sha256(envelope['source_refs']!)||result['canonical_ref']!=='sha256:'+sha256(envelope))throw new ProtocolError('canonical_binding');
        payload=envelope['payload'];isError=envelope['result_kind']==='tool_error';
      }
      if (result['call_id'] !== id || result['name'] !== tool.name || result['version'] !== tool.version
          || result['toolset_version'] !== this.pin || !object(payload)
          || result['output_ref'] !== 'sha256:' + sha256(payload)) throw new ProtocolError('host_receipt_binding');
      identifier(get(result, 'receipt_id'), 36); identifier(get(result, 'step_id'), 36);
      if (signal?.aborted || this.channel.error) throw new ProtocolError('dispatch_stopped');
      return {content: [{type: 'text' as const, text: canonical(payload as Json)}], ...(resultMode?{isError}:{}), details: {
        source: 'host-run', call_id: id, receipt_id: get(result, 'receipt_id'), step_id: get(result, 'step_id'),
        output_ref: get(result, 'output_ref'), toolset_version: this.pin, ...(resultMode?{canonical_ref:get(result,'canonical_ref')}:{}),
      }};
    } catch (error) {
      this.close(); const failure = error instanceof Error ? error : new ProtocolError('host_operation_failed');
      this.channel.fail(failure); throw failure;
    }
  }
}
