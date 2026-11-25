import {IMAGE_CONTENT_TYPES,MAX_IMAGE_BYTES,type ImageContentType} from '../imageAssets';

export type ProductResultImage={
 type:'product_image_result';block_id:string;sequence_no:number;image_ref:string;
 content_type:ImageContentType;byte_size:number;sha256:string;width:number;height:number;
};
export type ProductImageTarget={workId:string;runId:string|null;executionStatus?:string};
export type ProductImageBinding={workId:string;runId:string;image:ProductResultImage};
type Envelope={work_id:unknown;run_id:unknown;state:unknown;sequence_no:unknown};
const required=['type','block_id','sequence_no','image_ref','content_type','byte_size','sha256','width','height'] as const;
const uuid=/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u;
const digest=/^[0-9a-f]{64}$/u;
const maxPixels=16_000_000;
const invalid=():never=>{throw Error('商品图片的授权回执无法核对。');};
function integer(value:unknown,max:number):number {
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<1||value>max)return invalid();
 return value;
}
function ref(value:unknown,pattern:RegExp):string {
 if(typeof value!=='string'||!pattern.test(value)||/[\r\n]/u.test(value))return invalid();
 return value;
}

/** Candidate wire decoder, deliberately not registered until the guarded Host route is frozen. */
export function parseProductResultImage(value:unknown,envelope:Envelope,target:ProductImageTarget):ProductImageBinding {
 if(target.executionStatus!=='completed'||envelope.state!=='available'||envelope.run_id!==target.runId||
  envelope.work_id!==target.workId)return invalid();
 const workId=ref(target.workId,uuid),runId=ref(target.runId,uuid);
 if(!value||typeof value!=='object'||Array.isArray(value))return invalid();
 const v=value as Record<string,unknown>;
 if(v.type!=='product_image_result'||Object.keys(v).length!==required.length||
  !required.every(k=>Object.hasOwn(v,k))||!IMAGE_CONTENT_TYPES.includes(v.content_type as ImageContentType))return invalid();
 const width=integer(v.width,maxPixels),height=integer(v.height,maxPixels);
 if(width*height>maxPixels)return invalid();
 return {workId,runId,image:{type:'product_image_result',
  block_id:ref(v.block_id,uuid),image_ref:ref(v.image_ref,uuid),
  sequence_no:integer(v.sequence_no,integer(envelope.sequence_no,Number.MAX_SAFE_INTEGER)),
  content_type:v.content_type as ImageContentType,byte_size:integer(v.byte_size,MAX_IMAGE_BYTES),
  sha256:ref(v.sha256,digest),width,height}};
}

export function parseProductImageManifest(value:unknown,target:ProductImageTarget):ProductImageBinding[] {
 const fields=['version','work_id','run_id','sequence_no','state','images'];
 if(!value||typeof value!=='object'||Array.isArray(value))return invalid();
 const v=value as Record<string,unknown>;
 if(Object.keys(v).length!==fields.length||!fields.every(k=>Object.hasOwn(v,k))||v.version!==1||
  v.work_id!==target.workId||v.run_id!==target.runId||target.executionStatus!=='completed'||
  !['available','not_recorded','withheld'].includes(v.state as string)||
  !Array.isArray(v.images)||v.images.length>4||!Number.isSafeInteger(v.sequence_no)||(v.sequence_no as number)<0||
  (v.state==='available')!==(v.images.length>0))return invalid();
 ref(v.work_id,uuid);ref(v.run_id,uuid);
 const refs=new Set<string>(),steps=new Set<string>();
 return v.images.map(raw=>{
  const item=parseProductResultImage(raw,{work_id:v.work_id,run_id:v.run_id,state:v.state,sequence_no:v.sequence_no},target);
  if(refs.has(item.image.image_ref)||steps.has(item.image.block_id))return invalid();
  refs.add(item.image.image_ref);steps.add(item.image.block_id);return item;
 });
}
