/** Only the narrow Host-generated protocol. A model stream, HTML or arbitrary SSE is not a UI document. */
export type WatchFrame={kind:'snapshot';value:unknown}|{kind:'checked';digest:string}|{kind:'end'|'reset'|'use-snapshot'};
const FRAME_MAX=1048576,TOTAL_MAX=8*FRAME_MAX;
const bad=()=>{throw Error('业务结果流格式异常。');};
function parse(data:Uint8Array):WatchFrame {
 const text=new TextDecoder('utf-8',{fatal:true}).decode(data).replace(/\r\n/g,'\n').replace(/\n\n$/, '');
 const match=/^event: (snapshot|checked|end|reset|use-snapshot)\ndata: ([^\r\n]+)$/.exec(text);if(!match)return bad();
 const value:unknown=JSON.parse(match[2]);if(!value||typeof value!=='object'||Array.isArray(value))return bad();
 const v=value as Record<string,unknown>;
 if(match[1]==='snapshot')return {kind:'snapshot',value};
 if(match[1]==='checked'){if(Object.keys(v).length!==1||typeof v.snapshot_sha256!=='string'||!/^[a-f0-9]{64}$/.test(v.snapshot_sha256))return bad();return {kind:'checked',digest:v.snapshot_sha256};}
 if(Object.keys(v).length)return bad();return {kind:match[1]==='end'?'end':match[1]==='reset'?'reset':'use-snapshot'};
}
export async function consumePublicWatch(response:Response,onFrame:(f:WatchFrame)=>void) {
 if(!/^text\/event-stream(?:\s*;|$)/i.test(response.headers.get('content-type')??'')||!response.body)return bad();
 const reader=response.body.getReader(),buf=new Uint8Array(FRAME_MAX);let n=0,sum=0,count=0,terminal=false;
 try {
  while(!terminal){const {value,done}=await reader.read();if(done)break;if(!value)continue;
   if((sum+=value.byteLength)>TOTAL_MAX)return bad();
   for(const byte of value){
    if(terminal || n>=FRAME_MAX)return bad();buf[n++]=byte;
    if(byte!==10 || !(n>=2&&buf[n-2]===10 || n>=4&&buf[n-2]===13&&buf[n-3]===10&&buf[n-4]===13))continue;
    if(++count>16)return bad();const frame=parse(buf.subarray(0,n));n=0;
    if(count===1 && frame.kind==='checked')return bad();
    onFrame(frame);terminal=frame.kind==='end'||frame.kind==='reset'||frame.kind==='use-snapshot';
   }
  }
  if(!terminal||n!==0)return bad();
 }finally{try{await reader.cancel();}catch{/* Already aborted. */}reader.releaseLock();}
}
