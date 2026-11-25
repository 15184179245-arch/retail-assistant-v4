import {authorizedRequest} from '../../../lib/auth/session';
import {parseProductImageManifest,type ProductImageTarget} from './contract';
import type {ProductResultImageRequest} from './content';

const uuid=/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;
function id(value:string|null):string {
 if(!value||!uuid.test(value)||/[\r\n]/u.test(value))throw Error('商品图片标识无法核对。');
 return value;
}
export function productImageEndpoint(target:{workId:string;runId:string|null},imageRef?:string):string {
 return '/api/v1/assistant/work/'+id(target.workId)+'/product-images'+(imageRef===undefined?'':'/'+id(imageRef))+
  '?run_id='+id(target.runId);
}
export function sameOriginProductImages(baseUrl:string):boolean {
 if(typeof location==='undefined')return false;
 try{
  const base=new URL(baseUrl||'/',location.origin);
  return base.origin===location.origin&&base.pathname==='/'&&!base.search&&!base.hash&&!base.username&&!base.password;
 }catch{return false;}
}

export async function readProductImageManifest(target:ProductImageTarget,signal:AbortSignal) {
 const raw=await authorizedRequest<unknown>(productImageEndpoint(target),{
  method:'GET',signal,cache:'no-store',redirect:'error',
 });
 return parseProductImageManifest(raw,target);
}

export const readProductImageContent:ProductResultImageRequest=(binding,{signal,readResponse})=>
 authorizedRequest<Blob>(productImageEndpoint(binding,binding.image.image_ref),{
  method:'GET',signal,readResponse,cache:'no-store',redirect:'error',
  headers:{Accept:binding.image.content_type},
  timeoutMs:Math.min(85_000,Math.max(15_000,5_000+Math.ceil(binding.image.byte_size*1000/(64*1024)))),
 });
