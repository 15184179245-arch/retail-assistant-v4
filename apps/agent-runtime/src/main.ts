import {CanonicalPeer,RESULT_PROFILE,RESULT_ABI} from './protocol.js';
import {StreamPeer, STREAM_PROFILE, STREAM_PROTOCOL, STREAM_SESSION_MS, streamBytes} from './protocol.js';
/** Formal package stdio entry. No fixture bootstrap, provider path or old wire. */
import {DISCOVERY_SESSION_MS, DiscoveryPeer, DISCOVERY_PROFILE, DISCOVERY_ONLINE_PROFILE, DISCOVERY_PROTOCOL, DISCOVERY_ONLINE_PROTOCOL} from './protocol.js';
import {SourcePeer, SOURCE_PROFILE, SOURCE_PROTOCOL, SOURCE_ONLINE_PROFILE, SOURCE_ONLINE_PROTOCOL} from './protocol.js';
import {readFileSync, writeSync, lstatSync, realpathSync, appendFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {dirname, resolve, isAbsolute, join} from 'node:path';
import {Peer, OnlinePeer, MemoryPeer, MEMORY_PROFILE, MEMORY_ONLINE_PROFILE, MEMORY_PROTOCOL, MEMORY_ONLINE_PROTOCOL, ONLINE_PROFILE, ONLINE_PROTOCOL, Framer, SDK, PROFILE, LIMITS, ProtocolError, exact, get, object, shape} from './protocol.js';
import type {Frame, JsonObject, Start} from './protocol.js';
import type {HostChannel} from './tool-bridge.js';

type Waiter = {resolve(frame: Frame): void; reject(error: Error): void};
export class Channel implements HostChannel {
  readonly peer: Peer;
  readonly framer:Framer;
  readonly controller = new AbortController();
  error: Error | null = null;
  private queue: Frame[] = [];
  private waiter: Waiter | null = null;
  private disposed = false;
  private writing = false;
  private sendQueue: {type:string;body:JsonObject;reply:number|null;resolve():void;reject(e:unknown):void}[]=[];
  private draining=false;
  private stream: {id:string;requestSeq:number;raw:Uint8Array[];seqs:number[];consumed:number;ended:boolean;readerFinished:boolean;cancelled:boolean;pull:((value?:void)=>void)|null;controller:ReadableStreamDefaultController<Uint8Array>|null;open(value:Response):void;reject(error:Error):void;terminal(value:JsonObject):void;rejectTerminal(error:Error):void;body:ReadableStream<Uint8Array>;detachAbort():void}|null=null;
  private readonly started = performance.now();
  private readonly timer: ReturnType<typeof setInterval>;
  private readonly onData = (data: Buffer): void => {
    try {
      for (const raw of this.framer.feed(data)) {
        const frame = this.peer.receive(raw);
        if (this.streaming && (frame.type.startsWith('provider_') || (frame.type==='response' && frame.body['op']==='provider_call'))) {this.streamFrame(frame);continue;}
        if (this.waiter) { const waiter = this.waiter; this.waiter = null; waiter.resolve(frame); }
        else { if (this.queue.length) throw new ProtocolError('peer_pipelining'); this.queue.push(frame); }
      }
    } catch (error) { this.fail(error instanceof Error ? error : new ProtocolError('stream_error')); }
  };
  private readonly onEnd = (): void => {
    try { this.framer.eof(); this.fail(new ProtocolError('stdin_eof')); }
    catch (error) { this.fail(error instanceof Error ? error : new ProtocolError('stream_error')); }
  };
  private readonly onError = (): void => this.fail(new ProtocolError('stream_error'));
  constructor(online = false, memory = false, discovery = false, source = false, readonly streaming = false, readonly resultRecord?:JsonObject) {
    this.framer=new Framer(streaming);
    if (source && !discovery) throw new ProtocolError('tool_profile');
    this.peer = resultRecord ? new CanonicalPeer('sdk') : streaming ? new StreamPeer('sdk') : source ? new SourcePeer('sdk',null,online) : discovery ? new DiscoveryPeer('sdk',null,online) : memory ? new MemoryPeer('sdk', null, online) : online ? new OnlinePeer('sdk') : new Peer('sdk');
    if (!['linux','darwin'].includes(process.platform) || process.stdout.isTTY || process.stdout.fd !== 1) throw new ProtocolError('stdio_profile');
    process.stdin.on('data', this.onData); process.stdin.on('end', this.onEnd);
    process.stdin.on('error', this.onError); process.stdout.on('error', this.onError);
    this.timer = setInterval(() => {
      try {
        const elapsed = performance.now() - this.started;
        if (elapsed >= (streaming ? STREAM_SESSION_MS : discovery ? DISCOVERY_SESSION_MS : LIMITS.session)) throw new ProtocolError('session_deadline');
        if (['new','hello'].includes(this.peer.phase) && elapsed >= LIMITS.handshake) throw new ProtocolError('handshake_deadline');
        this.framer.check();
      } catch (error) { this.fail(error instanceof Error ? error : new ProtocolError('stream_error')); }
    }, 20);
  }
  get resultABI():string|undefined{return this.resultRecord?RESULT_ABI:undefined;}
  get resultIdentity(){return this.peer.identity;}
  fail(error: Error): void {
    this.error ??= error; this.controller.abort();
    this.stream?.detachAbort();if(this.streaming)this.peer.clearStreamAnchor();
    if(this.stream){this.stream.reject(this.error);this.stream.rejectTerminal(this.error);try{this.stream.controller?.error(this.error);}catch{}this.stream.pull?.();}
    for(const queued of this.sendQueue.splice(0))queued.reject(this.error);
    if (this.waiter) { this.waiter.reject(this.error); this.waiter = null; }
  }
  async next(): Promise<Frame> {
    if (this.error) throw this.error;
    const frame = this.queue.shift(); if (frame) return frame;
    if (this.waiter) throw new ProtocolError('concurrent_reader');
    return new Promise<Frame>((resolve, reject) => { this.waiter = {resolve, reject}; });
  }
  async send(type:string,body:JsonObject,reply:number|null=null):Promise<void>{
    if(!this.streaming)return this.sendDirect(type,body,reply);
    if(this.error)throw this.error;if(this.sendQueue.length>=16)throw new ProtocolError('control_backpressure');
    const pending=new Promise<void>((resolve,reject)=>this.sendQueue.push({type,body,reply,resolve,reject}));
    if(!this.draining){this.draining=true;void(async()=>{try{while(this.sendQueue.length){const item=this.sendQueue.shift()!;try{await this.sendDirect(item.type,item.body,item.reply);item.resolve();}catch(e){item.reject(e);throw e;}}}catch(e){this.fail(e instanceof Error?e:new ProtocolError('stream_error'));}finally{this.draining=false;}})();}
    return pending;
  }
  private async sendDirect(type: string, body: JsonObject,reply:number|null): Promise<void> {
    if (this.error) throw this.error;
    if (this.writing) throw new ProtocolError('concurrent_writer');
    this.writing = true;
    try {
      const raw = this.peer.send(type, body,reply); let offset = 0;
      const started = performance.now();
      while (offset < raw.length) {
        if (this.error) throw this.error;
        if (performance.now() - started >= LIMITS.partial) throw new ProtocolError('write_deadline');
        try {
          const count = writeSync(1, raw, offset, Math.min(4096, raw.length-offset));
          if (count <= 0) throw new ProtocolError('write_closed'); offset += count;
        } catch (error) {
          const code = error instanceof Error && 'code' in error ? error.code : undefined;
          if (code !== 'EAGAIN' && code !== 'EWOULDBLOCK') throw error;
        }
        if (offset < raw.length) await new Promise<void>(resolve => setTimeout(resolve, 5));
      }
    } catch (error) { this.fail(error instanceof Error ? error : new ProtocolError('stream_error')); throw this.error; }
    finally { this.writing = false; }
  }
  async request(op: 'model_begin' | 'model_complete' | 'tool_request' | 'provider_call', args: JsonObject): Promise<JsonObject> {
    await this.send('request', {op, arguments: args});
    const frame = await this.next();
    if (frame.type !== 'response' || frame.body['op'] !== op || frame.body['ok'] !== true
        || !object(frame.body['result'])) throw new ProtocolError('host_operation_failed');
    return frame.body['result'];
  }
  private drainRemainder():void{
    const s=this.stream;if(!s)return;
    if(!s.readerFinished){s.readerFinished=true;try{const root=process.env['PI_PRIVATE_ROOT'];if(root)appendFileSync(join(root,'native-events.jsonl'),JSON.stringify({kind:'native_reader_cleanup',at_ms:Date.now()})+'\n',{encoding:'utf8',mode:0o600});}catch{}}
    s.pull?.();s.pull=null;
    if(s.cancelled||this.error)return;
    while(s.raw.length){const raw=s.raw.shift()!,seq=s.seqs.shift()!;s.consumed+=raw.length;
      void this.send('provider_credit',{stream_id:s.id,through_chunk_seq:seq,bytes_consumed:s.consumed},s.requestSeq).catch(e=>this.fail(e instanceof Error?e:new ProtocolError('stream_credit')));
    }
  }
  private pumpStream():void{
    const s=this.stream;if(s?.readerFinished){this.drainRemainder();return;}if(!s||!s.controller||!s.pull)return;
    if(s.raw.length){const raw=s.raw.shift()!,seq=s.seqs.shift()!;s.consumed+=raw.length;s.controller.enqueue(raw);const pull=s.pull;s.pull=null;pull();
      void this.send('provider_credit',{stream_id:s.id,through_chunk_seq:seq,bytes_consumed:s.consumed},s.requestSeq).catch(e=>this.fail(e instanceof Error?e:new ProtocolError('stream_credit')));
    }else if(s.ended){s.controller.close();const pull=s.pull;s.pull=null;pull();}
  }
  private streamFrame(frame:Frame):void{
    const s=this.stream;if(!s)throw new ProtocolError('stream_pending');const b=frame.body;
    if(frame.type==='provider_open')s.open(new Response(s.body,{status:200,headers:{'content-type':'text/event-stream'}}));
    else if(frame.type==='provider_chunk'){s.raw.push(streamBytes(b['data_b64']));s.seqs.push(b['chunk_seq'] as number);this.pumpStream();}
    else if(frame.type==='provider_end'){s.ended=true;s.detachAbort();this.pumpStream();}
    else if(frame.type==='provider_abort'){s.detachAbort();const error=new ProtocolError('provider_unknown');s.reject(error);s.rejectTerminal(error);s.controller?.error(error);s.pull?.();s.pull=null;}
    else if(frame.type==='response'){if(b['ok']!==true||!object(b['result']))throw new ProtocolError('host_operation_failed');s.terminal(b['result']);}
    else throw new ProtocolError('stream_direction');
  }
  async openProvider(args:JsonObject,signal?:AbortSignal):Promise<{response:Response;terminal:Promise<JsonObject>}>{
    if(!this.streaming||this.stream)throw new ProtocolError('stream_concurrent');
    let open!:(r:Response)=>void,reject!:(e:Error)=>void,terminal!:(r:JsonObject)=>void,rejectTerminal!:(e:Error)=>void;
    const responsePromise=new Promise<Response>((a,b)=>{open=a;reject=b;});
    const terminalPromise=new Promise<JsonObject>((a,b)=>{terminal=a;rejectTerminal=b;});
    void terminalPromise.catch(()=>{});void responsePromise.catch(()=>{});
    const body=new ReadableStream<Uint8Array>({start:controller=>{queueMicrotask(()=>{if(this.stream)this.stream.controller=controller;});},pull:()=>new Promise<void>(resolve=>{if(!this.stream){resolve();return;}this.stream.pull=resolve;this.pumpStream();}),cancel:()=>{this.drainRemainder();}},{highWaterMark:0});
    const s=this.stream={id:args['attempt_id'] as string,requestSeq:this.peer.sent+1,raw:[],seqs:[],consumed:0,ended:false,readerFinished:false,cancelled:false,pull:null,controller:null,open,reject,terminal,rejectTerminal,body,detachAbort:()=>{}};
    const requestSeq=s.requestSeq,identity=this.peer.identity?{...this.peer.identity}:null;
    const abort=()=>{if(this.stream===s&&s.requestSeq===requestSeq&&identity
      &&this.peer.identity?.nonce===identity.nonce&&this.peer.identity.run_id===identity.run_id
      &&this.peer.identity.epoch===identity.epoch)void this.cancelProvider();};
    s.detachAbort=()=>{signal?.removeEventListener('abort',abort);};signal?.addEventListener('abort',abort,{once:true});
    try{await this.send('request',{op:'provider_call',arguments:args});const response=await responsePromise;const terminalResult=terminalPromise.finally(()=>{s.detachAbort();});void terminalResult.catch(()=>{});return {response,terminal:terminalResult};}
    catch(e){s.detachAbort();this.fail(e instanceof Error?e:new ProtocolError('provider_unknown'));throw e;}
  }
  finishProvider():void{this.stream?.detachAbort();this.stream=null;}
  async cancelProvider():Promise<void>{const s=this.stream;if(!s||s.ended||this.error)return;s.cancelled=true;try{await this.send('provider_cancel',{stream_id:s.id,reason:'runtime_abort'},s.requestSeq);}catch(e){this.fail(e instanceof Error?e:new ProtocolError('provider_unknown'));}}
  close(): void {
    if (this.disposed) return;
    this.disposed = true; clearInterval(this.timer); this.controller.abort();
    this.stream?.detachAbort();if(this.streaming)this.peer.clearStreamAnchor();
    process.stdin.pause(); process.stdin.removeListener('data', this.onData);
    process.stdin.removeListener('end', this.onEnd); process.stdin.removeListener('error', this.onError);
    process.stdout.removeListener('error', this.onError);
  }
}

export type Launch = {root: string; privateRoot: string; provider: string; model: string;
  syntheticCase: 'tasks' | 'knowledge' | 'catalog' | 'memory' | 'memory-save' | 'memory-correct' | 'memory-withdraw' | 'source-prices' | 'source-stock'; online?: boolean; streaming?:boolean; resultProfile?:boolean; manifestSha?:string; memory?: boolean; discovery?:boolean; source?:boolean};
function verifiedLaunch(): Launch {
  if (process.argv.length !== 4 || !['linux','darwin'].includes(process.platform) || process.arch !== 'x64'
      || process.versions.node.split('.')[0] !== '24') throw new ProtocolError('launch_profile');
  const [manifestPath, expected] = process.argv.slice(2);
  if (!manifestPath || !isAbsolute(manifestPath) || !expected || !/^[a-f0-9]{64}$/.test(expected)) throw new ProtocolError('launch_profile');
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
  if (manifestPath !== join(root, 'pi-release.json') || process.argv[1] !== join(root, 'dist/src/main.js')) throw new ProtocolError('launch_profile');
  const bytes = readFileSync(manifestPath);
  if (bytes.length > 16*1024*1024 || createHash('sha256').update(bytes).digest('hex') !== expected) throw new ProtocolError('launch_digest');
  const data: unknown = JSON.parse(bytes.toString('utf8'));
  if (!object(data)) throw new ProtocolError('launch_profile');
  const memory = data['profile'] === `host-pi-memory-node24-${process.platform}-v1` || data['profile'] === `host-pi-memory-node24-${process.platform}-online-v1`;
  const sourceProfile = data['profile'] === `host-pi-source-node24-${process.platform}-v1` || data['profile'] === `host-pi-source-node24-${process.platform}-online-v1`;
  const discovery = sourceProfile || data['profile'] === `host-pi-discovery-node24-${process.platform}-v1` || data['profile'] === `host-pi-discovery-node24-${process.platform}-online-v1`;
  const family = sourceProfile ? 'source' : discovery ? 'discovery' : memory ? 'memory' : 'readonly';
  const resultProfile=data['profile']===`host-pi-readonly-node24-${process.platform}-online-stream-result-v1`;
  const streaming=resultProfile||data['profile']===`host-pi-readonly-node24-${process.platform}-online-stream-v1`;
  const online = streaming || data['profile'] === `host-pi-${family}-node24-${process.platform}-online-v1`;
  if (data['profile'] !== `host-pi-${family}-node24-${process.platform}-${resultProfile ? 'online-stream-result-v1' : streaming ? 'online-stream-v1' : online ? 'online-v1' : 'v1'}`
      || data['wire_protocol'] !== (streaming ? STREAM_PROTOCOL : sourceProfile ? (online ? SOURCE_ONLINE_PROTOCOL : SOURCE_PROTOCOL) : discovery ? (online ? DISCOVERY_ONLINE_PROTOCOL : DISCOVERY_PROTOCOL) : memory ? (online ? MEMORY_ONLINE_PROTOCOL : MEMORY_PROTOCOL) : (online ? ONLINE_PROTOCOL : 'v4.pi-readonly/1'))) throw new ProtocolError('launch_profile');
  const sdk = exact(data['sdk'], ['engine','version','adapter_contract']);
  if (Object.entries(SDK).some(([key, value]) => sdk[key] !== value)) throw new ProtocolError('sdk_identity');
  const provider = exact(data['provider'], online ? ['kind','provider','model'] : ['kind','case']);
  const selected = online ? 'tasks' : provider['case'];
  if (online ? provider['kind'] !== 'host-proxy' || provider['provider'] !== 'opencode-go' || provider['model'] !== 'deepseek-v4.1-flash'
      : provider['kind'] !== 'synthetic' || (selected !== 'tasks' && selected !== 'knowledge' && selected !== 'catalog' && selected !== 'memory' && selected !== 'memory-save' && selected !== 'memory-correct' && selected !== 'memory-withdraw' && !(sourceProfile && (selected === 'source-prices' || selected === 'source-stock')))) throw new ProtocolError('provider_disabled');
  const metadata: unknown = JSON.parse(readFileSync(join(root,'node_modules/@earendil-works/pi-coding-agent/package.json'),'utf8'));
  if (!object(metadata) || metadata['name'] !== SDK.engine || metadata['version'] !== SDK.version) throw new ProtocolError('sdk_identity');
  const privateRoot = process.env['PI_PRIVATE_ROOT'], providerKey = process.env['PI_PROVIDER_KEY'], model = process.env['PI_MODEL_ID'];
  if (!privateRoot || !isAbsolute(privateRoot) || !privateRoot.startsWith(join(root,'runtime')+'/')
      || resolve(privateRoot) !== privateRoot || process.cwd() !== join(privateRoot,'cwd')
      || !providerKey || !/^[!-~]{1,200}$/.test(providerKey) || !model || !/^[!-~]{1,200}$/.test(model)) throw new ProtocolError('launch_profile');
  if (online && (providerKey !== 'opencode-go' || model !== 'deepseek-v4.1-flash')) throw new ProtocolError('physical_model_binding');
  const expectedEnv: Record<string,string> = {PATH:'/usr/bin:/bin', HOME:join(privateRoot,'home'),
    TMPDIR:join(privateRoot,'tmp'), XDG_CONFIG_HOME:join(privateRoot,'home'),
    XDG_CACHE_HOME:join(privateRoot,'home/cache'), LANG:'C.UTF-8', LC_ALL:'C.UTF-8', TZ:'UTC',
    PI_PRIVATE_ROOT:privateRoot, PI_PROVIDER_KEY:providerKey, PI_MODEL_ID:model};
  if (process.platform === 'darwin') expectedEnv['__CF_USER_TEXT_ENCODING'] = `0x${process.getuid!().toString(16).toUpperCase()}:0x0:0x0`;
  if (Object.keys(process.env).length !== Object.keys(expectedEnv).length
      || Object.entries(expectedEnv).some(([key,value])=>process.env[key] !== value)) throw new ProtocolError('launch_environment');
  const roles: Record<string,string> = {preload:'dist/src/permission-guard.js', main:'dist/src/main.js',
    protocol:'dist/src/protocol.js', runtime:'dist/src/runtime.js', provider:'dist/src/provider.js',
    tool_bridge:'dist/src/tool-bridge.js', package:'package.json', lock:'package-lock.json', ...(discovery ? {capabilities:'dist/src/capabilities.js'} : {})};
  const reads = [manifestPath,join(root,'node_modules'),privateRoot,...Object.values(roles).map(path=>join(root,path))];
  const expectedFlags = ['--permission','--no-addons','--jitless',
    ...reads.map(path=>'--allow-fs-read='+path),'--allow-fs-write='+privateRoot,
    '--import',join(root,roles['preload']!)];
  if (JSON.stringify(process.execArgv) !== JSON.stringify(expectedFlags)) throw new ProtocolError('launch_flags');
  const installed: unknown = (globalThis as unknown as Record<symbol,unknown>)[Symbol.for('v4.pi.readonly.guard')];
  if (!object(installed) || installed['installed'] !== true
      || installed['profile'] !== `host-pi-${family}-node24-${process.platform}-${resultProfile ? 'online-stream-result-v1' : streaming ? 'online-stream-v1' : online ? 'online-v1' : 'v1'}`) throw new ProtocolError('launch_guard');
  const manifestRoles = data['roles'], files = data['files'];
  if (!object(manifestRoles) || !Array.isArray(files)) throw new ProtocolError('launch_source');
  for (const [role,relative] of Object.entries(roles)) {
    const descriptor = manifestRoles[role], path = join(root,relative), stats = lstatSync(path);
    const rows = files.filter(row=>object(row) && row['root'] === 'release' && row['path'] === relative);
    if (!object(descriptor) || descriptor['root'] !== 'release' || descriptor['path'] !== relative
        || rows.length !== 1 || !stats.isFile() || realpathSync(path) !== path) throw new ProtocolError('launch_source');
    const row: unknown = rows[0], source = readFileSync(path);
    if (!object(row) || row['bytes'] !== source.length || row['mode'] !== (stats.mode & 0o777)
        || row['sha256'] !== createHash('sha256').update(source).digest('hex')) throw new ProtocolError('launch_source');
  }
  // Host verifies the full installed SDK closure. Child rechecks only the fixed
  // loaded release modules; neither check eliminates replacement races.
  return {root, privateRoot, provider: providerKey, model, syntheticCase: selected as Launch['syntheticCase'], online, streaming, resultProfile, manifestSha:expected, memory, discovery, source:sourceProfile};
}

async function main(): Promise<void> {
  const launch = verifiedLaunch();
  const channel = new Channel(launch.online, launch.memory, launch.discovery, launch.source,launch.streaming,launch.resultProfile?{contract_version:3,wire_protocol:STREAM_PROTOCOL,launch_profile:`host-pi-readonly-node24-${process.platform}-online-stream-result-v1`,source_manifest_sha256:launch.manifestSha!}:undefined);
  try {
    const hello = await channel.next();
    if (hello.type !== 'hello') throw new ProtocolError('hello_required');
    await channel.send('ready', {sdk: SDK, profile: launch.resultProfile ? RESULT_PROFILE : launch.streaming ? STREAM_PROFILE : launch.source ? (launch.online ? SOURCE_ONLINE_PROFILE : SOURCE_PROFILE) : launch.discovery ? (launch.online ? DISCOVERY_ONLINE_PROFILE : DISCOVERY_PROFILE) : launch.memory ? (launch.online ? MEMORY_ONLINE_PROFILE : MEMORY_PROFILE) : (launch.online ? ONLINE_PROFILE : PROFILE)});
    const frame = await channel.next();
    if (frame.type !== 'start') throw new ProtocolError('start_required');
    const start = frame.body; shape(start);
    // Source/flags/preload admission already completed. A ready frame means
    // protocol readiness, not a loaded model. Cold SDK imports must not consume
    // the short hello/ready deadline; the whole session remains time bounded.
    const runtime = await import('./runtime.js');
    if (channel.error) throw channel.error;
    let auditSequence = 0;
    await runtime.runFormalSession(start as unknown as Start, channel, launch, event=>{
      // Private synthetic-run diagnostics only. No prompt, tool arguments,
      // model output, credentials or internal lease/epoch are serialized.
      appendFileSync(join(launch.privateRoot,'native-events.jsonl'),
        JSON.stringify({sequence:++auditSequence, ...event})+'\n', {mode:0o600});
    });
    if (channel.error) throw channel.error;
    await channel.send('session_end', {status: 'ok'});
  } finally { channel.close(); }
}

// Importing Channel for tests does not start a process, SDK session or I/O.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch((error:unknown) => {
  if(error instanceof ProtocolError && /^[a-z_]{1,64}$/.test(error.code)){try{const root=process.env['PI_PRIVATE_ROOT'];if(root)appendFileSync(join(root,'launch-error.jsonl'),JSON.stringify({code:error.code})+'\n',{encoding:'utf8',mode:0o600});}catch{}} process.stderr.write('pi_runtime_failed\n'); process.exitCode = 1; });
}
