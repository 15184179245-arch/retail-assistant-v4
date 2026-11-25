// Importable controlled stdio entry. No arbitrary module/provider is chosen by wire.
import {Peer,Framer,SDK,LIMITS,ProtocolError} from './protocol.mjs';
import {writeSync} from 'node:fs';
export class Channel {
 constructor(input=process.stdin,output=process.stdout){
  // This candidate targets Node24's Linux nonblocking stdout pipe only.
  // No peer can supply a pathname or an arbitrary file descriptor.
  if(process.platform!=='linux'||output!==process.stdout||output.isTTY)throw new ProtocolError('stdio_profile');
  this.input=input;this.output=output;this.peer=new Peer('sdk');this.framer=new Framer();this.queue=[];this.readWait=null;this.error=null;this.started=performance.now();this.disposed=false;
  this.onData=data=>{try{for(const f of this.framer.feed(data)){this.peer.receive(f);if(this.readWait){const w=this.readWait;this.readWait=null;w.resolve(f);}else this.queue.push(f);}}catch(e){this.fail(e);}};
  this.onEnd=()=>{try{this.framer.eof();throw new ProtocolError('stdin_eof');}catch(e){this.fail(e);}};
  this.onError=()=>this.fail(new ProtocolError('stream_error'));
  input.on('data',this.onData);input.on('end',this.onEnd);input.on('error',this.onError);output.on('error',this.onError);
  this.timer=setInterval(()=>{try{const t=performance.now();if(t-this.started>=LIMITS.session)throw new ProtocolError('session_deadline');if(['new','hello'].includes(this.peer.phase)&&t-this.started>=LIMITS.handshake)throw new ProtocolError('handshake_deadline');this.framer.check(t);}catch(e){this.fail(e);}},20);
 }
 fail(error){if(this.error)return;this.error=error;this.input.pause();if(this.readWait){this.readWait.reject(error);this.readWait=null;}}
 async next(){if(this.error)throw this.error;if(this.queue.length)return this.queue.shift();if(this.readWait)throw new ProtocolError('concurrent_reader');return new Promise((resolve,reject)=>{this.readWait={resolve,reject};});}
 async send(type,body,reply=null){
  if(this.error)throw this.error;const raw=this.peer.send(type,body,reply);
  // Do not leave an uncancellable process.stdout stream backlog after failure.
  // Node initializes this Linux pipe nonblocking. EAGAIN keeps only this one
  // bounded frame in user memory; no libuv write remains queued at teardown.
  let offset=0;const started=performance.now();
  while(offset<raw.length){
   if(this.error)throw this.error;
   if(performance.now()-started>=LIMITS.partial){const e=new ProtocolError('write_deadline');this.fail(e);throw e;}
   try{const n=writeSync(this.output.fd,raw,offset,Math.min(4096,raw.length-offset));if(n<=0)throw new ProtocolError('write_closed');offset+=n;}
   catch(e){if(e.code!=='EAGAIN'&&e.code!=='EWOULDBLOCK'){this.fail(new ProtocolError('stream_error'));throw this.error;}}
   if(offset<raw.length)await new Promise(resolve=>setTimeout(resolve,5));
  }
 }
 async request(op,args){await this.send('request',{op,arguments:args});const f=await this.next();if(f.type!=='response'||f.body.op!==op)throw new ProtocolError('response_required');if(!f.body.ok)throw new ProtocolError(f.body.code);return f.body.result;}
 close(){if(this.disposed)return;this.disposed=true;clearInterval(this.timer);this.input.pause();this.input.removeListener('data',this.onData);this.input.removeListener('end',this.onEnd);this.input.removeListener('error',this.onError);this.output.removeListener('error',this.onError);}
}
export async function serve({verifiedSdk,run,input=process.stdin,output=process.stdout}){
 // The trusted launch verifies the installed public package, not a peer's claim.
 if(!verifiedSdk||Object.keys(SDK).some(k=>verifiedSdk[k]!==SDK[k]))throw new ProtocolError('unverified_sdk');
 const channel=new Channel(input,output);
 try{
  const hello=await channel.next();if(hello.type!=='hello')throw new ProtocolError('hello_required');
  await channel.send('ready',{sdk:SDK,profile:'tasks.list_mine@1'});
  const start=await channel.next();if(start.type!=='start')throw new ProtocolError('start_required');
  await run(start.body,channel);
  await channel.send('session_end',{status:'ok'});
 }catch(error){channel.fail(error);throw error;}finally{channel.close();}
}
