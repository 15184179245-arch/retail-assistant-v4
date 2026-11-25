import { authorizedRequest, captureSessionScope } from '../../lib/auth/session';

export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const IMAGE_CONTENT_TYPES = ['image/jpeg', 'image/png', 'image/webp'] as const;
export type ImageContentType = typeof IMAGE_CONTENT_TYPES[number];
export interface ImageAsset {
  id: string; category: string; source: string; original_name: string; content_type: ImageContentType;
  byte_size: number; sha256: string; created_by: string; created_at: string;
}
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function row(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw Error('图片元数据无法核对。');return value as Record<string,unknown>;}
function bounded(value:unknown,max:number):string{if(typeof value!=='string'||!value||[...value].length>max)throw Error('图片元数据无法核对。');return value;}
export function parseImageAsset(value:unknown):ImageAsset{
 const v=row(value);
 if(typeof v.id!=='string'||!uuid.test(v.id)||typeof v.created_by!=='string'||!uuid.test(v.created_by)||
   !IMAGE_CONTENT_TYPES.includes(v.content_type as ImageContentType)||!Number.isSafeInteger(v.byte_size)||(v.byte_size as number)<=0||(v.byte_size as number)>MAX_IMAGE_BYTES||
   typeof v.sha256!=='string'||!/^[0-9a-f]{64}$/.test(v.sha256)||typeof v.created_at!=='string'||!Number.isFinite(Date.parse(v.created_at)))throw Error('图片元数据无法核对。');
 return {id:v.id,created_by:v.created_by,content_type:v.content_type as ImageContentType,byte_size:v.byte_size as number,sha256:v.sha256,created_at:v.created_at,
   category:bounded(v.category,80),source:bounded(v.source,80),original_name:bounded(v.original_name,255)};
}
function extensionType(name:string):ImageContentType|undefined{
 if(/\.jpe?g$/i.test(name))return 'image/jpeg';
 if(/\.png$/i.test(name))return 'image/png';
 if(/\.webp$/i.test(name))return 'image/webp';
}
function unreportedType(type:string):boolean{return type===''||type==='application/octet-stream';}
export function validateImageFile(file:Pick<File,'name'|'size'|'type'>):void{
 if(!IMAGE_CONTENT_TYPES.includes(file.type as ImageContentType)&&!(unreportedType(file.type)&&extensionType(file.name)))
  throw Error('仅支持 JPEG、PNG 或 WebP 图片。');
 if(!Number.isSafeInteger(file.size)||file.size<=0||file.size>MAX_IMAGE_BYTES)throw Error('每张图片须大于0且不超过5MiB。');
 bounded(file.name,255);
}
/** Android document providers may expose no image MIME. Never trust the name alone. */
async function uploadContentType(file:File):Promise<ImageContentType>{
 if(!unreportedType(file.type))return file.type as ImageContentType;
 const hint=extensionType(file.name),head=new Uint8Array(await file.slice(0,12).arrayBuffer());
 const matches=(sig:number[],offset=0)=>sig.every((v,i)=>head[offset+i]===v);
 const valid=hint==='image/jpeg'&&matches([0xff,0xd8,0xff])||
   hint==='image/png'&&matches([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a])||
   hint==='image/webp'&&matches([82,73,70,70])&&matches([87,69,66,80],8);
 if(!hint||!valid)throw Error('图片的文件内容与扩展名不符，仅支持 JPEG、PNG 或 WebP。');
 // The Host still decodes and validates the complete original bytes.
 return hint;
}
async function digest(blob:Blob):Promise<string>{return [...new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))].map(n=>n.toString(16).padStart(2,'0')).join('');}
export async function uploadImageAsset(file:File,options:{category:string;source:string;baseUrl?:string;signal?:AbortSignal}):Promise<ImageAsset>{
 validateImageFile(file);const current=captureSessionScope();
 const contentType=await uploadContentType(file);
 if(!current()||options.signal?.aborted)throw Error('身份或图片选择已改变。');
 const expected=await digest(file);if(!current()||options.signal?.aborted)throw Error('身份或图片选择已改变。');
 const category=bounded(options.category,80),source=bounded(options.source,80);
 const asset=parseImageAsset(await authorizedRequest<unknown>('/api/v1/media/assets?'+new URLSearchParams({filename:file.name,category,source}),
  {method:'POST',rawBody:file,headers:{'Content-Type':contentType},baseUrl:options.baseUrl,signal:options.signal}));
 if(!current()||options.signal?.aborted)throw Error('身份或图片选择已改变。');
 if(asset.sha256!==expected||asset.byte_size!==file.size||asset.content_type!==contentType||asset.category!==category)throw Error('上传图片与保存回执不一致，尚未附到消息。');
 return asset;
}
export interface ImagePreview {url:string;dispose:()=>void}
// Only the bounded, already authorized image-body read gets extra time. At 64 KiB/s
// a 5 MiB image takes about 80 seconds before allowance for latency.
const previewDownloadTimeout = (bytes:number) =>
 Math.min(85_000,Math.max(15_000,5_000+Math.ceil(bytes*1000/(64*1024))));
/** No token in an img URL. Metadata and bytes use the same existing scoped request lifecycle. */
export async function loadImagePreview(asset:ImageAsset,options:{baseUrl?:string;signal?:AbortSignal}={}):Promise<ImagePreview>{
 const expected=parseImageAsset(asset),current=captureSessionScope();
 const blob=await authorizedRequest<Blob>('/api/v1/media/assets/'+expected.id+'/content',{
  baseUrl:options.baseUrl,signal:options.signal,timeoutMs:previewDownloadTimeout(expected.byte_size),readResponse:async response=>{
  const type=(response.headers.get('Content-Type')??'').split(';')[0].trim();
  const length=response.headers.get('Content-Length');
  if(type!==expected.content_type||length!==null&&(!/^\d+$/.test(length)||Number(length)>MAX_IMAGE_BYTES)){await response.body?.cancel();throw Error('图片响应无法核对。');}
  const reader=response.body?.getReader();if(!reader)throw Error('图片响应没有内容。');
  const chunks:BlobPart[]=[];let size=0;
  try{while(true){const part=await reader.read();if(part.done)break;size+=part.value.byteLength;
    if(size>expected.byte_size||size>MAX_IMAGE_BYTES){await reader.cancel();throw Error('图片大小超出已核对的记录。');}
    chunks.push(new Uint8Array(part.value).buffer);
  }}finally{reader.releaseLock();}
  if(size!==expected.byte_size)throw Error('图片大小与记录不一致。');return new Blob(chunks,{type});
 }});
 if(await digest(blob)!==expected.sha256||!current()||options.signal?.aborted)throw Error('图片内容或身份无法核对。');
 const url=URL.createObjectURL(blob);let disposed=false;
 return {url,dispose:()=>{if(!disposed){disposed=true;URL.revokeObjectURL(url);}}};
}
