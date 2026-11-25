import {allowsOperationalReceipts} from './operationalReceiptPage';
import {sourceBusinessDay} from './publicResultDays';

export type TransferDocument = {
 bill_code:string;bill_date:string|null;bill_state:number;bill_type:201|202;
 out_warehouse_name:string|null;in_warehouse_name:string|null;
};
export type TransferLine = {product_code:string;product_name:string;reported_quantity:string;reported_unit:string|null};
export type TransferPage = {type:'operational_transfer_page';action:'stock_transfers';block_id:string;sequence_no:number;
 returned_count:number;offset:number;reported_total:null;items:TransferDocument[]};
export type TransferDetail = TransferDocument & {type:'operational_transfer_detail';action:'stock_transfer_detail';
 block_id:string;sequence_no:number;returned_count:number;items:TransferLine[]};
export type TransferBlock = TransferPage|TransferDetail;
const docKeys=['bill_code','bill_date','bill_state','bill_type','out_warehouse_name','in_warehouse_name'] as const;
const shared=['type','action','block_id','sequence_no','returned_count','items'] as const;
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
const numberText=/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/u;
const invalid=():never=>{throw new Error('已核调拨来源格式异常。');};
function exact(v:unknown,keys:readonly string[]):Record<string,unknown>{
 if(!v||typeof v!=='object'||Array.isArray(v))return invalid();
 const o=v as Record<string,unknown>;
 if(keys.length!==Object.keys(o).length||keys.some(k=>!Object.hasOwn(o,k)))return invalid();
 return o;
}
function integer(v:unknown,min=0,max=Number.MAX_SAFE_INTEGER):number{
 if(typeof v!=='number'||!Number.isSafeInteger(v)||v<min||v>max)return invalid();
 return v;
}
function text(v:unknown,cap:number):string{
 if(typeof v!=='string'||v.length>2*cap||[...v].length>cap||!v.trim()||/[\x00-\x1f\x7f-\x9f]/u.test(v))return invalid();
 return v;
}
function nullable(v:unknown,cap:number):string|null{return v===null?null:text(v,cap);}
function documentFields(v:Record<string,unknown>):TransferDocument{
 const day=nullable(v.bill_date,64);
 if(day!==null&&!sourceBusinessDay(day))return invalid();
 const type=v.bill_type;
 if(type!==201&&type!==202)return invalid();
 return {bill_code:text(v.bill_code,200),bill_date:day,bill_state:integer(v.bill_state,Number.MIN_SAFE_INTEGER),
  bill_type:type,out_warehouse_name:nullable(v.out_warehouse_name,200),in_warehouse_name:nullable(v.in_warehouse_name,200)};
}
export function parseTransferBlock(value:unknown,{runId,state,sequenceNo,executionStatus}:{
  runId:string|null;state:unknown;sequenceNo:number;executionStatus?:string
}):TransferBlock{
 if(!runId||state!=='available'||!allowsOperationalReceipts(executionStatus))return invalid();
 if(!value||typeof value!=='object')return invalid();
 const type=(value as Record<string,unknown>).type;
 if(type!=='operational_transfer_page'&&type!=='operational_transfer_detail')return invalid();
 const b=exact(value,type==='operational_transfer_page'?[...shared,'offset','reported_total']:[...shared,...docKeys]);
 if(typeof b.block_id!=='string'||!uuid.test(b.block_id)||b.block_id.length!==36||!Array.isArray(b.items))return invalid();
 const returned=integer(b.returned_count,0,type==='operational_transfer_page'?100:250);
 if(returned!==b.items.length)return invalid();
 const base={block_id:b.block_id,sequence_no:integer(b.sequence_no,1,sequenceNo),returned_count:returned};
 if(type==='operational_transfer_page'){
  if(b.action!=='stock_transfers'||b.reported_total!==null)return invalid();
  return {type,action:b.action,...base,offset:integer(b.offset,0,100000),reported_total:null,
          items:b.items.map(row=>documentFields(exact(row,docKeys)))};
 }
 if(b.action!=='stock_transfer_detail')return invalid();
 return {type,action:b.action,...base,...documentFields(b),items:b.items.map(row=>{
  const line=exact(row,['product_code','product_name','reported_quantity','reported_unit']);
  const quantity=text(line.reported_quantity,100);
  if(!numberText.test(quantity))return invalid();
  return {product_code:text(line.product_code,300),product_name:text(line.product_name,300),
   reported_quantity:quantity,reported_unit:nullable(line.reported_unit,100)};
 })};
}
export function isTransferBlock<T extends {type:string}>(block:T):block is T & TransferBlock{
 return block.type==='operational_transfer_page'||block.type==='operational_transfer_detail';
}
export function transferHeading(block:TransferBlock){
 return block.type==='operational_transfer_page'?'同店仓库调拨单':'同店仓库调拨商品明细';
}
export const transferDocumentColumns=['单号','单据日期原值','调出仓','调入仓','单据类型码','状态码'];
export const transferLineColumns=['商品编码','商品名称','数量原值','录入单位'];
export function transferDocumentCells(r:TransferDocument){
 return [r.bill_code,r.bill_date,r.out_warehouse_name,r.in_warehouse_name,r.bill_type,r.bill_state];
}
export function transferLineCells(r:TransferLine){
 return [r.product_code,r.product_name,r.reported_quantity,r.reported_unit];
}
export const transferScopeNotice='仅为当前来源保留的同店调拨记录，不代表已查全、已过账或实际库存移动。';
