export type ProductUnitRow={
 kind:'unit';standard_id:string;standard_name:string;relation:string;
 barcode_state:'reported'|'partial'|'not_reported';verified_barcode_count:number|null;
};
export type ProductBarcodeRow={kind:'barcodes';standard_id:string;start_ordinal:number;barcodes:string[]};
export type ProductBarcodePage={
 type:'product_barcode_page';block_id:string;sequence_no:number;origin_block_id:string;
 product_code:string;product_name:string;offset:number;returned_count:number;total_records:number;
 total_units:number;total_verified_barcodes:number;next_offset:number|null;
 verified_through:number;all_pages_verified:boolean;rows:(ProductUnitRow|ProductBarcodeRow)[];
};
const maxUnits=500,maxRecords=maxUnits*(maxUnits+1),maxPageRecords=8192;
const bad=():never=>{throw Error('商品条码来源页格式异常。');};
function exact(value:unknown,keys:string[]):Record<string,unknown>{
 if(!value||typeof value!=='object'||Array.isArray(value))return bad();
 const record=value as Record<string,unknown>;
 if(Object.keys(record).length!==keys.length||keys.some(key=>!Object.hasOwn(record,key)))return bad();
 return record;
}
function count(value:unknown,min=0,max=maxRecords):number{
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<min||value>max)return bad();
 return value;
}
function text(value:unknown,limit:number):string{
 if(typeof value!=='string'||!value||value.length>limit*2||[...value].length>limit||/[\x00-\x1f\x7f-\x9f\p{Cs}]/u.test(value))return bad();
 return value;
}

/** Only use this decoder for the bound, current authenticated public snapshot. */
export function parseProductBarcodePage(raw:unknown,target:{runId:string|null;state:unknown;sequenceNo:number;executionStatus?:string}):ProductBarcodePage{
 if(!target.runId||target.state!=='available'||(target.executionStatus!=='completed'&&target.executionStatus!=='failed'))return bad();
 const p=exact(raw,['type','block_id','sequence_no','origin_block_id','product_code','product_name','offset','returned_count',
  'total_records','total_units','total_verified_barcodes','next_offset','verified_through','all_pages_verified','rows']);
 if(p.type!=='product_barcode_page'||!Array.isArray(p.rows)||p.rows.length<1||p.rows.length>maxPageRecords)return bad();
 const offset=count(p.offset),returned=count(p.returned_count,1,maxPageRecords),end=offset+returned;
 const totalUnits=count(p.total_units,1,maxUnits),totalBarcodes=count(p.total_verified_barcodes,0,maxUnits*maxUnits),total=count(p.total_records,1);
 const through=count(p.verified_through,1,total),next=p.next_offset===null?null:count(p.next_offset,1,total);
 if(end>through||total!==totalUnits+totalBarcodes||next!==(end<total?end:null)||p.all_pages_verified!==(through===total))return bad();
 let found=0;
 const rows=p.rows.map((value):ProductUnitRow|ProductBarcodeRow=>{
  if(!value||typeof value!=='object')return bad();
  if((value as {kind:unknown}).kind==='unit'){
   const r=exact(value,['kind','standard_id','standard_name','relation','barcode_state','verified_barcode_count']);
   const state=r.barcode_state;
   if(state!=='reported'&&state!=='partial'&&state!=='not_reported')return bad();
   const verified=r.verified_barcode_count===null?null:count(r.verified_barcode_count,0,maxUnits);
   if((state==='not_reported')!==(verified===null))return bad();
   found++;
   return {kind:'unit',standard_id:text(r.standard_id,120),standard_name:text(r.standard_name,500),
    relation:text(r.relation,500),barcode_state:state,verified_barcode_count:verified};
  }
  const r=exact(value,['kind','standard_id','start_ordinal','barcodes']);
  const start=count(r.start_ordinal,0,maxUnits-1);
  if(r.kind!=='barcodes'||!Array.isArray(r.barcodes)||!r.barcodes.length||start+r.barcodes.length>maxUnits)return bad();
  found+=r.barcodes.length;
  if(found>returned)return bad();
  return {kind:'barcodes',standard_id:text(r.standard_id,120),start_ordinal:start,barcodes:r.barcodes.map(code=>text(code,200))};
 });
 if(found!==returned)return bad();
 return {type:'product_barcode_page',block_id:text(p.block_id,120),origin_block_id:text(p.origin_block_id,120),
  sequence_no:count(p.sequence_no,1,target.sequenceNo),product_code:text(p.product_code,500),product_name:text(p.product_name,500),
  offset,returned_count:returned,total_records:total,total_units:totalUnits,total_verified_barcodes:totalBarcodes,
  next_offset:next,verified_through:through,all_pages_verified:through===total,rows};
}

/** A successor must be a contiguous saved page of the same opaque origin. */
export function validateProductBarcodeCoverage(pages:ProductBarcodePage[]):void{
 const groups=new Map<string,ProductBarcodePage[]>();
 for(const page of pages){
  const group=groups.get(page.origin_block_id);
  if(group)group.push(page);
  else groups.set(page.origin_block_id,[page]);
 }
 for(const group of groups.values()){
  const ordered=group.sort((a,b)=>a.offset-b.offset);
  const first=ordered[0];
  if(first.offset!==0||first.block_id!==first.origin_block_id)return bad();
  let end=0,sequence=0;
  for(const page of ordered){
   if(page.offset!==end||page.sequence_no<=sequence||page.product_code!==first.product_code||page.product_name!==first.product_name||
      page.total_records!==first.total_records||page.total_units!==first.total_units||page.total_verified_barcodes!==first.total_verified_barcodes||
      page.verified_through!==first.verified_through||page.all_pages_verified!==first.all_pages_verified)return bad();
   end+=page.returned_count;
   sequence=page.sequence_no;
  }
  if(end!==first.verified_through)return bad();
 }
}

export type ProductBarcodeRemainderPage=Pick<ProductBarcodePage,'offset'|'returned_count'|'total_records'|'total_units'|
 'total_verified_barcodes'|'next_offset'|'rows'>;
export type ProductBarcodeRemainderBatch={
 recordedAt:string;verifiedThrough:number;nextCursor:string|null;pages:ProductBarcodeRemainderPage[];
};
export function canReadProductRemainder(page:ProductBarcodePage):boolean {
 return !page.all_pages_verified&&page.next_offset===page.verified_through;
}

/** An independent local read cannot masquerade as pages saved by the original Run. */
export function parseProductBarcodeRemainder(value:unknown,target:{workId:string;runId:string;anchor:ProductBarcodePage;
 expectedOffset:number;recordedAt?:string}):ProductBarcodeRemainderBatch{
 const a=target.anchor;
 const r=exact(value,['version','origin','work_id','run_id','origin_block_id','anchor_block_id','product_code','product_name',
  'source_recorded_at','anchor_offset','verified_through','all_pages_verified','next_cursor','pages']);
 if(!canReadProductRemainder(a)||r.version!==1||r.origin!=='stored_original'||r.work_id!==target.workId||r.run_id!==target.runId||
    r.origin_block_id!==a.origin_block_id||r.anchor_block_id!==a.block_id||r.product_code!==a.product_code||
    r.product_name!==a.product_name||r.anchor_offset!==a.verified_through||!Array.isArray(r.pages)||!r.pages.length||r.pages.length>8)return bad();
 const recordedAt=text(r.source_recorded_at,40);
 if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/u.test(recordedAt)||
    !Number.isFinite(Date.parse(recordedAt))||(target.recordedAt!==undefined&&recordedAt!==target.recordedAt))return bad();
 const through=count(r.verified_through,target.expectedOffset+1,a.total_records);
 const end=through===a.total_records;
 const cursor=r.next_cursor===null?null:text(r.next_cursor,71);
 if(r.all_pages_verified!==end||end!==(cursor===null)||(cursor!==null&&!/^sha256:[a-f0-9]{64}$/u.test(cursor)))return bad();
 let offset=target.expectedOffset;
 const pages=r.pages.map(value=>{
  const p=exact(value,['offset','returned_count','total_records','total_units','total_verified_barcodes','next_offset','rows']);
  if(p.offset!==offset||p.total_records!==a.total_records||p.total_units!==a.total_units||p.total_verified_barcodes!==a.total_verified_barcodes)return bad();
  const page=parseProductBarcodePage({...p,type:'product_barcode_page',block_id:a.block_id,origin_block_id:a.origin_block_id,
   sequence_no:1,product_code:a.product_code,product_name:a.product_name,verified_through:through,all_pages_verified:end},
   {runId:target.runId,state:'available',executionStatus:'completed',sequenceNo:1});
  offset+=page.returned_count;
  return {offset:page.offset,returned_count:page.returned_count,total_records:page.total_records,total_units:page.total_units,
   total_verified_barcodes:page.total_verified_barcodes,next_offset:page.next_offset,rows:page.rows};
 });
 if(offset!==through)return bad();
 return {recordedAt,verifiedThrough:through,nextCursor:cursor,pages};
}

export const productArchiveProof = (value:unknown):string|null =>
 typeof value==='string'&&/^sha256:[a-f0-9]{64}$/u.test(value)?value:null;

export function matchesProductBarcodeArchiveCheck(value:unknown,target:{workId:string;runId:string;anchor:ProductBarcodePage;
 recordedAt:string;archiveProof:string}):boolean{
 try{
  const anchor=target.anchor;
  const reply=exact(value,['version','origin','work_id','run_id','origin_block_id','anchor_block_id','anchor_offset','source_recorded_at','archive_proof']);
  return reply.version===1&&reply.origin==='stored_original'&&reply.work_id===target.workId&&reply.run_id===target.runId&&
   reply.origin_block_id===anchor.origin_block_id&&reply.anchor_block_id===anchor.block_id&&reply.anchor_offset===anchor.verified_through&&
   reply.source_recorded_at===target.recordedAt&&productArchiveProof(reply.archive_proof)===target.archiveProof;
 }catch{return false;}
}
