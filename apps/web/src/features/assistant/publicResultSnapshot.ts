/** Only authenticated Host snapshots. Never parse model answer text or provider SSE. */
import {parseTransferBlock,type TransferBlock} from './operationalTransfer';
import {parseAuthorizedCustomerReceivablesPage,type CustomerReceivablesPage} from './customerReceivablesPage';
import {parseAuthorizedPurchaseOrderDetail,type PurchaseOrderDetailPage} from './purchaseOrderDetailPage';
import {parseOperationalReceiptPage,type OperationalReceiptPage} from './operationalReceiptPage';
import {parseOperationalReceiptDetail,type OperationalReceiptDetailPage} from './operationalReceiptDetail';
import {parseProductBarcodePage,validateProductBarcodeCoverage,type ProductBarcodePage} from './productBarcodePage';
export type Scalar = string | number | boolean | null;
export interface ResultTable {
 type:'report_table'; block_id:string; sequence_no:number; title:string;
 columns:{key:string;label:string}[]; rows:{row_id:string;cells:Scalar[]}[];
 source:{kind:'guanjia_endpoint';action:'sales_query'|'sales_lines';canonical_ref:string;response_sha256:string;recorded_at:string};
 coverage:{offset:number;row_count:number;reported_total:number;page_exhausted:boolean;all_pages_verified:false;bill_state_filter:12|null};
 units:{currency:null;tax_basis:null}; notice:string;
}
export interface PartialStockObservation {
 type:'partial_stock_observation'; block_id:string; sequence_no:number; title:'已核实的库存观察';
 product:{code:string;name:string};
 stock:{quantity_raw:string;unit:null;observed_at:string};
 price_status:'unverified';
}
export interface PartialEndpointObservation {
 type:'partial_endpoint_observation'; block_id:string; sequence_no:number;
 action:'sales_returns'|'purchase_receipts_query'; returned_count:number; recorded_at:string;
}
export type ResultBlock = ResultTable | PartialStockObservation | PartialEndpointObservation | CustomerReceivablesPage | PurchaseOrderDetailPage | OperationalReceiptPage | OperationalReceiptDetailPage | TransferBlock | ProductBarcodePage;
export function isPartialObservation(block:ResultBlock):block is PartialStockObservation|PartialEndpointObservation {
 return block.type==='partial_stock_observation'||block.type==='partial_endpoint_observation';
}
export interface ResultSnapshot {version:1;work_id:string;run_id:string|null;sequence_no:number;state:'available'|'not_recorded'|'withheld';blocks:ResultBlock[];snapshot_sha256:string}
export interface ResultTarget {workId:string;runId:string|null;executionStatus?:string}
const invalid=():never=>{throw Error('公开业务结果格式异常。');};
function object(v:unknown):Record<string,unknown>{if(!v||typeof v!=='object'||Array.isArray(v))return invalid();return v as Record<string,unknown>;}
function keys(v:Record<string,unknown>,allowed:string[]){if(Object.keys(v).length!==allowed.length||Object.keys(v).some(k=>!allowed.includes(k)))invalid();}
function string(v:unknown):string{if(typeof v!=='string')return invalid();return v;}
function integer(v:unknown):number{if(typeof v!=='number'||!Number.isSafeInteger(v)||v<0)return invalid();return v;}
function hash(v:unknown):string{const s=string(v);if(!/^[a-f0-9]{64}$/.test(s))return invalid();return s;}
function uuid(v:unknown):string{const s=string(v);if(!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(s))return invalid();return s;}
function array(v:unknown):unknown[]{if(!Array.isArray(v))return invalid();return v;}
function table(value:unknown):ResultTable{
 const b=object(value);keys(b,['type','block_id','sequence_no','title','columns','rows','source','coverage','units','notice']);
 if(b.type!=='report_table')return invalid();string(b.block_id);integer(b.sequence_no);string(b.title);string(b.notice);
 const cs=array(b.columns);if(!cs.length)return invalid();const columnKeys=new Set<string>();
 for(const v of cs){const c=object(v);keys(c,['key','label']);const key=string(c.key);if(columnKeys.has(key))return invalid();columnKeys.add(key);string(c.label);}
 const rows=array(b.rows),rowIds=new Set<string>();
 for(const v of rows){const r=object(v);keys(r,['row_id','cells']);const id=string(r.row_id);if(rowIds.has(id))return invalid();rowIds.add(id);const cells=array(r.cells);if(cells.length!==cs.length)return invalid();for(const c of cells){if(c!==null&&typeof c!=='string'&&typeof c!=='boolean'&&(typeof c!=='number'||!Number.isFinite(c)))return invalid();}}
 const s=object(b.source);keys(s,['kind','action','canonical_ref','response_sha256','recorded_at']);
 if(s.kind!=='guanjia_endpoint'||!['sales_query','sales_lines'].includes(string(s.action)))return invalid();
 hash(s.response_sha256);if(!/^sha256:[a-f0-9]{64}$/.test(string(s.canonical_ref)))return invalid();if(!Number.isFinite(Date.parse(string(s.recorded_at))))return invalid();
 const c=object(b.coverage);keys(c,['offset','row_count','reported_total','page_exhausted','all_pages_verified','bill_state_filter']);
 const offset=integer(c.offset),n=integer(c.row_count),total=integer(c.reported_total);
 if(n!==rows.length||(n>0&&offset+n>total)||c.all_pages_verified!==false||c.page_exhausted!==(offset+n===total)||c.bill_state_filter!==(s.action==='sales_lines'?12:null))return invalid();
 const u=object(b.units);keys(u,['currency','tax_basis']);if(u.currency!==null||u.tax_basis!==null)return invalid();
 return b as unknown as ResultTable;
}
function compactText(v:unknown):string{
 const raw=string(v);
 if(raw.length>400||!raw.trim()||[...raw].length>200||/[\x00-\x1f\x7f-\x9f]/u.test(raw))return invalid();
 return raw;
}
const decimal=/^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/u;
function exactStockQuantity(value:unknown):string{
 const raw=string(value);
 if(!raw.length||raw.length>100||/[\r\n]/u.test(raw))return invalid();
 const parsed=decimal.exec(raw);if(!parsed)return invalid();
 const [, , whole, fraction='', exponent='0']=parsed;
 const first=(whole+fraction).search(/[1-9]/u);
 // Validate Decimal.adjusted without converting the source quantity into a JS Number.
 const adjusted=BigInt(exponent)+BigInt(first<0 ? -fraction.length : whole.length-first-1);
 if(adjusted>100n||adjusted< -100n)return invalid();
 return raw;
}
function utcTime(value:unknown):string {
 const raw=string(value);
 if(raw.startsWith('0000-')||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/u.test(raw)||/[\r\n]/u.test(raw))return invalid();
 const ms=Date.parse(raw);
 if(!Number.isFinite(ms)||new Date(ms).toISOString().slice(0,19)!==raw.slice(0,19))return invalid();
 return raw;
}
function resultBlock(value:unknown):ResultBlock{
 const b=object(value);
 if(b.type==='partial_endpoint_observation'){
  keys(b,['type','block_id','sequence_no','action','returned_count','recorded_at']);
  compactText(b.block_id);
  if(integer(b.sequence_no)===0||integer(b.returned_count)>100||
     (b.action!=='sales_returns'&&b.action!=='purchase_receipts_query'))return invalid();
  utcTime(b.recorded_at);
  return b as unknown as PartialEndpointObservation;
 }
 if(b.type!=='partial_stock_observation')return table(value);
 keys(b,['type','block_id','sequence_no','title','product','stock','price_status']);
 if(b.title!=='已核实的库存观察'||b.price_status!=='unverified'||integer(b.sequence_no)===0)return invalid();
 compactText(b.block_id);
 const p=object(b.product);keys(p,['code','name']);compactText(p.code);compactText(p.name);
 const stock=object(b.stock);keys(stock,['quantity_raw','unit','observed_at']);
 if(stock.unit!==null)return invalid();
 exactStockQuantity(stock.quantity_raw);utcTime(stock.observed_at);
 return b as unknown as PartialStockObservation;
}
export function parseResultSnapshot(value:unknown,target:ResultTarget):ResultSnapshot{
 const v=object(value);keys(v,['version','work_id','run_id','sequence_no','state','blocks','snapshot_sha256']);
 if(v.version!==1||uuid(v.work_id)!==target.workId||(v.run_id!==null&&uuid(v.run_id)!==target.runId)||v.run_id!==target.runId)return invalid();
 integer(v.sequence_no);hash(v.snapshot_sha256);
 if(!['available','not_recorded','withheld'].includes(string(v.state)))return invalid();
 const rawBlocks=array(v.blocks).map(object);
 const receiptContext={runId:target.runId,state:v.state,sequenceNo:integer(v.sequence_no),executionStatus:target.executionStatus};
 const detailCandidates=rawBlocks.filter(block=>block.type==='purchase_order_detail');
 // Only this explicit GET v2 pairing may accompany a detail. The existing
 // list decoder still independently validates every companion against the Run.
 const orderListCompanions=new Map<Record<string,unknown>,OperationalReceiptPage>();
 if(rawBlocks.length>1&&detailCandidates.length===1&&detailCandidates[0].detail_schema===2&&
    rawBlocks.every(block=>block===detailCandidates[0]||
     (block.type==='operational_receipt_page'&&block.action==='purchase_orders_query'))){
  for(const block of rawBlocks)if(block!==detailCandidates[0])
   orderListCompanions.set(block,parseOperationalReceiptPage(block,receiptContext));
 }
 const blocks=rawBlocks.map(block=>{
  const type=block.type;
  if(type==='operational_transfer_page'||type==='operational_transfer_detail')return parseTransferBlock(block,{
   runId:target.runId,state:v.state,sequenceNo:integer(v.sequence_no),executionStatus:target.executionStatus});
  if(type==='product_barcode_page')return parseProductBarcodePage(block,{runId:target.runId,state:v.state,
   sequenceNo:integer(v.sequence_no),executionStatus:target.executionStatus});
  if(type==='operational_receipt_detail')return parseOperationalReceiptDetail(block,{runId:target.runId,state:v.state,
   sequenceNo:integer(v.sequence_no),executionStatus:target.executionStatus});
  if(type==='operational_receipt_page')return orderListCompanions.get(block)??parseOperationalReceiptPage(block,receiptContext);
  if(type!=='customer_receivables_detail'&&type!=='purchase_order_detail')return resultBlock(block);
  if(target.runId===null)return invalid();
  const context={work_id:v.work_id,run_id:v.run_id,state:v.state,sequence_no:v.sequence_no,block_count:rawBlocks.length};
  if(type==='purchase_order_detail')return parseAuthorizedPurchaseOrderDetail(block,
   {...context,order_lists:[...orderListCompanions.values()]},target);
  return parseAuthorizedCustomerReceivablesPage(block,context,
    {workId:target.workId,runId:target.runId,executionStatus:target.executionStatus??''});
 });
 if(v.state!=='available'&&blocks.length)return invalid();
 if(blocks.some(isPartialObservation)&&
    (blocks.length!==1||v.run_id===null||target.executionStatus!=='failed'))return invalid();
 const ids=new Set<string>();for(const b of blocks){if(ids.has(b.block_id)||b.sequence_no>(v.sequence_no as number))return invalid();ids.add(b.block_id);}
 validateProductBarcodeCoverage(blocks.filter(b=>b.type==='product_barcode_page'));
 // Deep copy prevents later mutation of transport-owned data. Digest is a Host
 // identity, not a client signature or a substitute for current authorization.
 return structuredClone({...v,blocks}) as unknown as ResultSnapshot;
}
/** A sequence watermark cannot authorize retaining an old visible table. */
export class PublicResultSlot {
 private generation=0; private target:ResultTarget|null=null;
 snapshot:ResultSnapshot|null=null;
 begin(target:ResultTarget):number{this.target={...target};this.snapshot=null;return ++this.generation;}
 accept(ticket:number,value:unknown):boolean{
  if(ticket!==this.generation||!this.target)return false;
  try{this.snapshot=parseResultSnapshot(value,this.target);return true;}
  catch(error){this.snapshot=null;throw error;}
 }
 fail(ticket:number):void{if(ticket===this.generation)this.snapshot=null;}
 reset():void{this.generation++;this.target=null;this.snapshot=null;}
}
