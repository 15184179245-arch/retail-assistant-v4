/** Per-physical-attempt Host gate. This module contains no network transport.
 * AGT-045 Foundation integration: formalized provider gate bindings.
 */
import {get, exact, integer, identifier, digest, ProtocolError, shape} from './protocol.js';
import type {JsonObject} from './protocol.js';
import {installArgumentRejection} from './tool-bridge.js';
import type {HostChannel} from './tool-bridge.js';

export type Purpose = 'inference' | 'retry' | 'compaction';
export type Permit = Readonly<{attemptId: string; stepId: string; token: string;
  maxInput: number; maxOutput: number}>;
export type KnownMessage = {role: 'assistant'; content: JsonObject[];
  usage: {input: number; output: number}; stopReason: 'toolUse' | 'stop' | 'error' | 'aborted'};
export type PhysicalResult<T> = {native: T; accounting: KnownMessage};

export class PhysicalCallGate {
  private stopped = false;
  private active = false;
  private readonly permits = new Set<string>();
  private readonly attempts = new Set<string>();
  constructor(private readonly channel: HostChannel, readonly pin: string) { digest(pin, true); }
  close(): void { this.stopped = true; }
  async call<T>(purpose: Purpose, inputDigest: string,
                produce: (permit: Permit) => Promise<PhysicalResult<T>>,
                signal?: AbortSignal): Promise<T> {
    if (this.stopped || this.active || this.channel.error || signal?.aborted) {
      this.stopped = true; const failure = new ProtocolError('dispatch_stopped');
      this.channel.fail(failure); throw failure;
    }
    if (!['inference', 'retry', 'compaction'].includes(purpose)) throw new ProtocolError('purpose');
    digest(inputDigest); this.active = true;
    try {
      const response = await this.channel.request('model_begin', {
        purpose, input_digest: inputDigest, toolset_version: this.pin,
      });
      exact(response, ['attempt_id', 'step_id', 'permit', 'limits']);
      const attempt = get(response, 'attempt_id'), step = get(response, 'step_id'), token = get(response, 'permit');
      identifier(attempt); identifier(step); identifier(token);
      const limits = exact(get(response, 'limits'), ['input', 'output']);
      const input = get(limits, 'input'), output = get(limits, 'output'); integer(input); integer(output);
      if (attempt !== step || this.attempts.has(attempt) || this.permits.has(token)) throw new ProtocolError('attempt_binding');
      this.attempts.add(attempt); this.permits.add(token);
      if (this.stopped || this.channel.error || signal?.aborted) throw new ProtocolError('dispatch_stopped');
      // The callback is invoked exactly once, only after a real Host response.
      // It cannot request an internal retry with this permit. SDK retry and
      // compaction adapters must enter call() again for each physical attempt.
      const result = await produce(Object.freeze({attemptId: attempt, stepId: step, token,
        maxInput: input, maxOutput: output}));
      const message = result.accounting;
      exact(message, ['role', 'content', 'usage', 'stopReason']); shape(message);
      exact(message.usage, ['input', 'output']); integer(message.usage.input); integer(message.usage.output);
      if (message.role !== 'assistant' || !Array.isArray(message.content)
          || !['toolUse', 'stop', 'error', 'aborted'].includes(message.stopReason)) throw new ProtocolError('physical_result');
      // Known usage is not discarded merely because the local abort signal
      // changed after completion. Host may account it then refuse execution.
      const committed = await this.channel.request('model_complete', {
        attempt_id: attempt, step_id: step, permit: token, message,
      });
      exact(committed, ['committed', 'finished']);
      if (committed['committed'] !== true || typeof committed['finished'] !== 'boolean') throw new ProtocolError('commit_receipt');
      if (this.stopped || this.channel.error || signal?.aborted) throw new ProtocolError('dispatch_stopped');
      return result.native;
    } catch (error) {
      this.stopped = true;
      const failure = error instanceof Error ? error : new ProtocolError('physical_result');
      this.channel.fail(failure); throw failure;
    } finally { this.active = false; }
  }
}

/** v2 transport: the only fetch admitted by the native OpenAI adapter.
 * No URL, headers, credentials or transport functions are delegated to Host.
 */
import {createHash} from 'node:crypto';
import {PROVIDER_PATH, PROVIDER_BYTES, object, streamSummary} from './protocol.js';
export interface OnlineHostChannel extends HostChannel {
  readonly streaming?:boolean;
  reportInputCapacity?():Promise<void>;
  openProvider?(args:JsonObject,signal?:AbortSignal):Promise<{response:Response;terminal:Promise<JsonObject>}>;
  finishProvider?():void;
  request(op: 'model_begin' | 'model_complete' | 'tool_request' | 'provider_call', args: JsonObject): Promise<JsonObject>;
}
export type OnlineKnownMessage = Omit<KnownMessage, 'usage'> & {
  usage: {input: number; output: number; cacheRead: number; cacheWrite: number};
};
export const ONLINE_BASE = 'https://opencode.ai/inference/go/openai/v1';
export const ONLINE_PROVIDER = 'opencode-go';
export const ONLINE_MODEL = 'deepseek-v4.1-flash';
export const PROXY_PLACEHOLDER = 'host-proxy-no-credential';

export class OnlinePhysicalCallGate {
  private stopped = false;
  private active = false;
  private readonly permits = new Set<string>();
  private readonly attempts = new Set<string>();
  constructor(private readonly channel: OnlineHostChannel, readonly pin: string) { digest(pin, true); }
  close(): void { this.stopped = true; }
  async call<T>(inputDigest: string,
    produce: (fetch: typeof globalThis.fetch) => Promise<{native:T; accounting:OnlineKnownMessage}>,
    signal?: AbortSignal): Promise<T> {
    let sent = false;
    let permit: Permit | undefined;
    let observedUsage: OnlineKnownMessage['usage'] | undefined;
    let streamTerminal:Promise<JsonObject>|undefined;
    const check = () => {
      if (this.stopped || this.channel.error || signal?.aborted) throw new ProtocolError('dispatch_stopped');
    };
    try {
      check(); if (this.active) throw new ProtocolError('dispatch_stopped');
      digest(inputDigest); this.active = true;
      const fetch: typeof globalThis.fetch = async (url, init) => {
        try {
          check();
          if (sent) throw new ProtocolError('provider_retry_forbidden');
          // Consume local one-shot before any validation/await: a rejected or
          // uncertain fetch can never acquire another permit on this invocation.
          sent = true;
          if (typeof url !== 'string' || url !== ONLINE_BASE+'/chat/completions'
              || init?.method !== 'POST' || typeof init.body !== 'string'
              || (init.redirect !== undefined && init.redirect !== 'error')) throw new ProtocolError('provider_request');
          const bytes = Buffer.from(init.body, 'utf8');
          shape(init.body);
          if (bytes.length > PROVIDER_BYTES) {
            // Serialized by the SDK, including system/tools/history. Report via
            // the existing finite error frame BEFORE acquiring any Host permit.
            await this.channel.reportInputCapacity?.();
            throw new ProtocolError('provider_bytes');
          }
          if (!bytes.length) throw new ProtocolError('provider_bytes');
          const body: unknown = JSON.parse(init.body);
          if (!object(body) || body['model'] !== ONLINE_MODEL || body['stream'] !== true) throw new ProtocolError('physical_model_binding');
          const headers = new Headers(init.headers);
          if (headers.get('authorization') !== 'Bearer '+PROXY_PLACEHOLDER) throw new ProtocolError('provider_credentials');
          const requestDigest = createHash('sha256').update(bytes).digest('hex');
          const response = await this.channel.request('model_begin', {
            purpose:'inference', input_digest:inputDigest, request_digest:requestDigest, toolset_version:this.pin,
          });
          exact(response, ['attempt_id','step_id','permit','limits']);
          const attempt = get(response,'attempt_id'), step = get(response,'step_id'), token = get(response,'permit');
          identifier(attempt); identifier(step); identifier(token);
          const limits = exact(get(response,'limits'), ['input','output']);
          const input = get(limits,'input'), output = get(limits,'output'); integer(input,1); integer(output,1);
          if (attempt !== step || this.attempts.has(attempt) || this.permits.has(token)) throw new ProtocolError('attempt_binding');
          this.attempts.add(attempt); this.permits.add(token);
          permit = Object.freeze({attemptId:attempt, stepId:step, token, maxInput:input, maxOutput:output});
          const requestedOutput = body['max_tokens'] ?? body['max_completion_tokens'];
          integer(requestedOutput,1,output);
          check();
          const argumentsBody:JsonObject={attempt_id:attempt, step_id:step, permit:token, input_digest:inputDigest, request_digest:requestDigest,
            request:{method:'POST',path:PROVIDER_PATH,body:init.body}};
          if(this.channel.streaming){
            if(!this.channel.openProvider)throw new ProtocolError('stream_unsupported');
            const opened=await this.channel.openProvider(argumentsBody,signal);streamTerminal=opened.terminal;return opened.response;
          }
          const received = await this.channel.request('provider_call', {
            attempt_id:attempt, step_id:step, permit:token, input_digest:inputDigest, request_digest:requestDigest,
            request:{method:'POST', path:PROVIDER_PATH, body:init.body},
          });
          exact(received,['status','request_id','body']);
          integer(received['status'],200,599);
          if (received['request_id'] !== null) identifier(received['request_id']);
          const raw = received['body'];
          if (typeof raw !== 'string' || Buffer.byteLength(raw) > PROVIDER_BYTES) throw new ProtocolError('provider_bytes');
          shape(raw);
          // Errors are deliberately opaque. Do not pass upstream error text to
          // SDK diagnostics or create an SDK retryable HTTP response.
          if (received['status'] !== 200) throw new ProtocolError('provider_status');
          // SDK defaults absent usage to zero; explicitly reject that ambiguity.
          // This verification does not replace native SSE/content parsing.
          for (const line of raw.split(/\r?\n/)) {
            if (!line.startsWith('data:')) continue;
            const text = line.slice(5).trim(); if (!text || text === '[DONE]') continue;
            const chunk: unknown = JSON.parse(text);
            if (!object(chunk) || !object(chunk['usage'])) continue;
            if (observedUsage) throw new ProtocolError('provider_usage');
            const usage = chunk['usage'];
            const prompt = usage['prompt_tokens'], completion = usage['completion_tokens'];
            const details = usage['prompt_tokens_details'];
            if (details !== undefined && !object(details)) throw new ProtocolError('provider_usage');
            // Native SDK zero defaults are not evidence for Go's missing
            // standard write field. Require each explicit Go cache dimension.
            if (usage['opencode'] !== undefined) {
              const go = usage['opencode'];
              if (!object(go) || go['billing_source'] !== 'go' || !object(go['tokens'])
                  || !object(details)
                  || !object(usage['completion_tokens_details'])) throw new ProtocolError('provider_usage');
              const tokens = go['tokens'], completionDetails = usage['completion_tokens_details'];
              for (const key of ['input','output','reasoning','cache_read','cache_write_5m','cache_write_1h']) integer(tokens[key]);
              const read = details['cached_tokens']; integer(read); integer(prompt); integer(completion);
              const reasoning = completionDetails['reasoning_tokens']; integer(reasoning);
              if ((read as number) > (prompt as number) || tokens['cache_read'] !== read
                  || tokens['input'] !== (prompt as number)-(read as number)
                  || tokens['output'] !== completion || tokens['reasoning'] !== reasoning
                  || (reasoning as number) > (completion as number) || usage['total_tokens'] !== (prompt as number)+(completion as number)
                  || ['cache_write_5m','cache_write_1h'].some(key => tokens[key] !== 0)
                  || (details['cache_write_tokens'] !== undefined && details['cache_write_tokens'] !== null && details['cache_write_tokens'] !== 0)
                  || ['prompt_cache_hit_tokens','cached_tokens'].some(key => usage[key] !== undefined && usage[key] !== read))
                throw new ProtocolError('provider_usage');
            }
            const cacheRead = (object(details) ? details['cached_tokens'] : undefined) ?? usage['prompt_cache_hit_tokens'] ?? usage['cached_tokens'] ?? 0;
            const cacheWrite = (object(details) ? details['cache_write_tokens'] : undefined) ?? 0;
            integer(prompt); integer(completion); integer(cacheRead); integer(cacheWrite);
            if (cacheWrite !== 0 || cacheRead > prompt || prompt > input || completion > output) throw new ProtocolError('provider_usage');
            observedUsage = {input:prompt-cacheRead, output:completion, cacheRead, cacheWrite};
          }
          if (!observedUsage) throw new ProtocolError('provider_usage');
          check();
          return new Response(raw, {status:200, headers:{'content-type':'text/event-stream',
            ...(received['request_id'] === null ? {} : {'x-request-id':received['request_id'] as string})}});
        } catch (error) {
          this.stopped = true;
          const failure = error instanceof ProtocolError ? error : new ProtocolError('provider_unknown');
          this.channel.fail(failure); throw failure;
        }
      };
      const result = await produce(fetch);
      if(streamTerminal){const summary=streamSummary(await streamTerminal);observedUsage=summary['usage'] as OnlineKnownMessage['usage'];this.channel.finishProvider?.();
        if(!permit||observedUsage.input+observedUsage.cacheRead+observedUsage.cacheWrite>permit.maxInput||observedUsage.output>permit.maxOutput)throw new ProtocolError('provider_usage');}
      check();
      if (!sent || !permit || !observedUsage) throw new ProtocolError('provider_not_sent');
      const message = result.accounting;
      exact(message,['role','content','usage','stopReason']); shape(message);
      exact(message.usage,['input','output','cacheRead','cacheWrite']);
      if (message.role !== 'assistant' || !Array.isArray(message.content)
          || !['stop','toolUse'].includes(message.stopReason)
          || Object.entries(observedUsage).some(([key,value]) => message.usage[key as keyof typeof observedUsage] !== value)) throw new ProtocolError('provider_usage');
      const committed = await this.channel.request('model_complete', {
        attempt_id:permit.attemptId, step_id:permit.stepId, permit:permit.token, message,
      });
      exact(committed,['committed','finished', ...('argument_rejection' in committed ? ['argument_rejection'] : [])]);
      if (committed['committed'] !== true || typeof committed['finished'] !== 'boolean') throw new ProtocolError('commit_receipt');
      if ('argument_rejection' in committed) {
        const feedback=committed['argument_rejection'];
        if (!object(feedback) || committed['finished'] !== false) throw new ProtocolError('commit_receipt');
        installArgumentRejection(this.channel,feedback,permit.stepId,this.pin,message as unknown as JsonObject);
      }
      check(); return result.native;
    } catch (error) {
      this.stopped = true;
      // Adapter errors can include request data. Surface only fixed local codes.
      const failure = error instanceof ProtocolError ? error : new ProtocolError('provider_unknown');
      this.channel.fail(failure); throw failure;
    } finally { this.active = false; }
  }
}
