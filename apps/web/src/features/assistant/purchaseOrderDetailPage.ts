import type {OperationalReceiptPage} from './operationalReceiptPage';

/** Public contract for one rechecked, persisted purchase order.
 * Does not discover a tool, perform a read, or treat an answer as evidence. */
export type PurchaseOrderProductLine = {
 product_code:string;
 product_name:string;
 specification:string|null;
};
type DetailIdentity = {
 type:'purchase_order_detail';block_id:string;sequence_no:number;
 bill_code:string;bill_date:string;returned_count:number;
};
type ReportedPurchaseOrderLine=PurchaseOrderProductLine&{
 reported_order_quantity:string|null;reported_order_unit:string|null;
};
export type PurchaseOrderDetailPage =
 | DetailIdentity&{detail_schema?:undefined;items:PurchaseOrderProductLine[]}
 | DetailIdentity&{detail_schema:2;source_recorded_at:string;items:ReportedPurchaseOrderLine[]};
type Envelope={work_id:unknown;run_id:unknown;state:unknown;sequence_no:unknown;block_count:number;
 order_lists?:readonly OperationalReceiptPage[]};
type Target={workId:string;runId:string|null;executionStatus?:string};
const maxLines=250;
const detailFields=['type','block_id','sequence_no','bill_code','bill_date','returned_count','items'];
const lineFields=['product_code','product_name','specification'];
const invalid=():never=>{throw Error('采购订单商品明细格式异常。');};
export function canReadPurchaseOrderDetailPage(has:(capability:string)=>boolean):boolean {
 return ['guanjia.company.read','guanjia.purchase_orders.read','guanjia.purchase_order_detail.read'].every(has);
}
function record(value:unknown,expected:readonly string[]):Record<string,unknown> {
 if(!value||typeof value!=='object'||Array.isArray(value))return invalid();
 const r=value as Record<string,unknown>;
 if(Object.keys(r).length!==expected.length||!expected.every(name=>Object.hasOwn(r,name)))return invalid();
 return r;
}
function count(value:unknown,lower:number,upper:number):number {
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<lower||value>upper)return invalid();
 return value;
}
function sourceText(value:unknown,max:number,min=1):string {
 if(typeof value!=='string'||/[\x00-\x1f\x7f]/u.test(value))return invalid();
 const length=[...value].length;
 if(length<min||length>max)return invalid();
 return value;
}
function quantity(value:unknown):string|null {
 if(value===null)return null;
 if(typeof value!=='string'||!/^-?(?:0|[1-9][0-9]{0,60})(?:\.[0-9]{1,18})?$/u.test(value))return invalid();
 return value;
}
function unit(value:unknown):string|null {
 if(value===null)return null;
 const raw=sourceText(value,120);
 if(!raw.trim()||[...raw].some(char=>char!==' '&&/[\p{C}\p{Z}]/u.test(char)))return invalid();
 return raw;
}
function recorded(value:unknown):string {
 const raw=sourceText(value,40);
 if(!/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?\+00:00$/u.test(raw)||raw.startsWith('0000-'))return invalid();
 const date=new Date(raw);
 if(!Number.isFinite(date.valueOf())||date.toISOString().slice(0,19)!==raw.slice(0,19))return invalid();
 return raw;
}
export function parseAuthorizedPurchaseOrderDetail(value:unknown,envelope:Envelope,target:Target):PurchaseOrderDetailPage {
 const versioned=!!value&&typeof value==='object'&&Object.hasOwn(value,'detail_schema');
 const allowedCompanions=versioned&&envelope.block_count>1&&envelope.order_lists?.length===envelope.block_count-1&&
  envelope.order_lists.every(list=>list.type==='operational_receipt_page'&&list.action==='purchase_orders_query');
 if(!target.workId||!target.runId||target.executionStatus!=='completed'||envelope.state!=='available'||
    envelope.work_id!==target.workId||envelope.run_id!==target.runId||!(envelope.block_count===1||allowedCompanions))return invalid();
 const b=record(value,versioned?[...detailFields,'detail_schema','source_recorded_at']:detailFields);
 if(versioned&&b.detail_schema!==2)return invalid();
 if(b.type!=='purchase_order_detail'||!Array.isArray(b.items))return invalid();
 const expected=count(b.returned_count,0,maxLines);
 if(b.items.length!==expected)return invalid();
 const blockId=sourceText(b.block_id,100);
 if(!/^[A-Za-z0-9_.:-]+$/u.test(blockId))return invalid();
 const seq=count(b.sequence_no,1,count(envelope.sequence_no,1,Number.MAX_SAFE_INTEGER));
 const identity:DetailIdentity={type:'purchase_order_detail',block_id:blockId,sequence_no:seq,bill_code:sourceText(b.bill_code,200),
  bill_date:sourceText(b.bill_date,64),returned_count:expected};
 function line(r:Record<string,unknown>):PurchaseOrderProductLine {
  return {product_code:sourceText(r.product_code,300),product_name:sourceText(r.product_name,300),
   specification:r.specification===null?null:sourceText(r.specification,300,0)};
 }
 if(!versioned)return {...identity,items:b.items.map(raw=>line(record(raw,lineFields)))};
 const items=b.items.map(raw=>{
  const r=record(raw,[...lineFields,'reported_order_quantity','reported_order_unit']);
  return {...line(r),reported_order_quantity:quantity(r.reported_order_quantity),reported_order_unit:unit(r.reported_order_unit)};
 });
 return {...identity,detail_schema:2,source_recorded_at:recorded(b.source_recorded_at),items};
}
