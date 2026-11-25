import {MAX_IMAGE_BYTES} from '../imageAssets';
import type {ProductImageBinding,ProductResultImage} from './contract';

export type ProductResultImageRequest=(binding:ProductImageBinding,options:{
 signal:AbortSignal; readResponse:(response:Response)=>Promise<Blob>;
})=>Promise<Blob>;

type ImagePreview={url:string;dispose:()=>void};
const invalid=():never=>{throw Error('商品图片没有通过本次读取核验。');};

/** Read one exact bounded image from the Host, never follow a vendor redirect or accept partial bytes. */
export async function verifyProductImageResponse(response:Response,image:ProductResultImage,signal:AbortSignal):Promise<Blob> {
 signal.throwIfAborted();
 const type=(response.headers.get('Content-Type')??'').split(';',1)[0].trim().toLowerCase();
 const length=response.headers.get('Content-Length');
 const noStore=(response.headers.get('Cache-Control')??'').split(',').some(p=>p.trim().toLowerCase()==='no-store');
 if(!Number.isSafeInteger(image.byte_size)||image.byte_size<1||image.byte_size>MAX_IMAGE_BYTES||
  !noStore||response.status!==200||response.redirected||response.headers.has('Content-Range')||type!==image.content_type||
  (length!==null&&(!/^\d+$/u.test(length)||Number(length)!==image.byte_size))){
  await response.body?.cancel().catch(()=>{});
  return invalid();
 }
 const reader=response.body?.getReader();
 if(!reader)return invalid();
 const cancel=()=>{void reader.cancel().catch(()=>{});};
 signal.addEventListener('abort',cancel,{once:true});
 const data=new Uint8Array(image.byte_size);
 let size=0;
 try {
  while(true){
   signal.throwIfAborted();
   const part=await reader.read();
   if(part.done)break;
   if(size+part.value.byteLength>data.length){await reader.cancel();return invalid();}
   data.set(part.value,size);size+=part.value.byteLength;
  }
 } finally {signal.removeEventListener('abort',cancel);reader.releaseLock();}
 signal.throwIfAborted();
 if(size!==data.length)return invalid();
 const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',data))].map(n=>n.toString(16).padStart(2,'0')).join('');
 signal.throwIfAborted();
 if(digest!==image.sha256)return invalid();
 return new Blob([data],{type});
}

export async function loadProductImagePreview(binding:ProductImageBinding,request:ProductResultImageRequest,
 signal:AbortSignal,stillAuthorized:()=>boolean):Promise<ImagePreview>{
 signal.throwIfAborted();
 if(!stillAuthorized())return invalid();
 // The adapter must bind the sole dedicated relative Host GET to the original Work + Run + image reference.
 let verified:Blob|null=null;
 const blob=await request(binding,{signal,readResponse:async r=>{
  verified=await verifyProductImageResponse(r,binding.image,signal);
  return verified;
 }});
 signal.throwIfAborted();
 if(!stillAuthorized()||!verified||blob!==verified)return invalid();
 const url=URL.createObjectURL(blob);
 let disposed=false;
 return {url,dispose:()=>{if(!disposed){disposed=true;URL.revokeObjectURL(url);}}};
}
