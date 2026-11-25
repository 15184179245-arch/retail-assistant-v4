import {MAX_IMAGE_BYTES,IMAGE_CONTENT_TYPES,type ImageContentType} from './imageAssets';
export interface ImageInput {asset_id:string}
export type ImageReference={asset_id:string;readable:false;unavailable_reason:string|null}|{
 asset_id:string;readable:true;sha256:string;content_type:ImageContentType;byte_size:number;width:number;height:number;original_name:string;unavailable_reason:null};
export interface InputPolicy {maxTextJsonBytes:number;imageSupported:boolean;imageAvailable:boolean;maxImages:number;maxImageBytes:number;maxTotalImageBytes:number}
export const LEGACY_INPUT_POLICY:InputPolicy={maxTextJsonBytes:131072,imageSupported:false,imageAvailable:false,maxImages:0,maxImageBytes:0,maxTotalImageBytes:0};
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function object(value:unknown):Record<string,unknown>{if(!value||typeof value!=='object'||Array.isArray(value))throw Error('附件合同无法核对。');return value as Record<string,unknown>;}
function positive(value:unknown,max:number):number{if(!Number.isSafeInteger(value)||(value as number)<=0||(value as number)>max)throw Error('输入额度合同无法核对。');return value as number;}
export function parseInputPolicy(status:Record<string,unknown>):InputPolicy{
 const supported=status.image_input_supported??false,available=status.image_input_available??false;
 if(typeof supported!=='boolean'||typeof available!=='boolean'||available&&!supported)throw Error('图片入口状态无法核对。');
 const p={...LEGACY_INPUT_POLICY,imageSupported:supported};
 if(status.input_limits===undefined)return p;
 const limits=object(status.input_limits);
 if(limits.max_text_json_bytes!==undefined)p.maxTextJsonBytes=positive(limits.max_text_json_bytes,8*1024*1024);
 if(!supported||limits.max_images===undefined||limits.max_image_bytes===undefined||limits.max_total_image_bytes===undefined)return p;
 p.maxImages=positive(limits.max_images,4);p.maxImageBytes=positive(limits.max_image_bytes,MAX_IMAGE_BYTES);p.maxTotalImageBytes=positive(limits.max_total_image_bytes,4*MAX_IMAGE_BYTES);
 if(p.maxTotalImageBytes<p.maxImageBytes)throw Error('图片额度合同无法核对。');p.imageAvailable=available;return p;
}
export function parseImageInputs(value:unknown):ImageInput[]{
 if(value==null)return [];
 if(!Array.isArray(value)||value.length>4)throw Error('每条消息最多4张图片。');const seen=new Set<string>();
 return value.map(item=>{const v=object(item);if(Object.keys(v).length!==1||typeof v.asset_id!=='string'||!uuid.test(v.asset_id)||seen.has(v.asset_id))throw Error('图片引用重复或无法核对。');seen.add(v.asset_id);return{asset_id:v.asset_id};});
}
export function parseImageReferences(value:unknown):ImageReference[]{
 if(value===undefined)return[];
 if(value==null)return [];
 if(!Array.isArray(value)||value.length>4)throw Error('图片回执无法核对。');const seen=new Set<string>();
 return value.map(item=>{const v=object(item);if(typeof v.asset_id!=='string'||!uuid.test(v.asset_id)||seen.has(v.asset_id)||typeof v.readable!=='boolean')throw Error('图片回执无法核对。');seen.add(v.asset_id);
  if(!v.readable)return{asset_id:v.asset_id,readable:false,unavailable_reason:'image_unavailable'};
  if(typeof v.sha256!=='string'||!/^[0-9a-f]{64}$/.test(v.sha256)||!IMAGE_CONTENT_TYPES.includes(v.content_type as ImageContentType)||typeof v.original_name!=='string'||!v.original_name||[...v.original_name].length>255)throw Error('图片回执无法核对。');
  return{asset_id:v.asset_id,readable:true,sha256:v.sha256,content_type:v.content_type as ImageContentType,byte_size:positive(v.byte_size,MAX_IMAGE_BYTES),width:positive(v.width,Number.MAX_SAFE_INTEGER),height:positive(v.height,Number.MAX_SAFE_INTEGER),original_name:v.original_name,unavailable_reason:null};
 });
}
export function sameImageInputs(actual:ImageReference[]|undefined,expected:ImageInput[]|undefined):boolean{return JSON.stringify((actual??[]).map(i=>i.asset_id))===JSON.stringify((expected??[]).map(i=>i.asset_id));}
