import {sourceBusinessDay} from './publicResultDays';

export const operationalReceiptNames={
 sales_returns:'销售退货单',
 purchase_returns:'采购退货单',
 purchase_receipts_query:'采购入库单',
 purchase_orders_query:'采购订单',
} as const;
export type OperationalReceiptAction=keyof typeof operationalReceiptNames;
export type OperationalReceiptRow={bill_code:string;bill_date:string|null;bill_state:number};
export type OperationalReceiptPage={
 type:'operational_receipt_page';block_id:string;sequence_no:number;action:OperationalReceiptAction;
 offset:number;returned_count:number;reported_total:number|null;items:OperationalReceiptRow[];
};
const invalid=():never=>{throw Error('单据来源页格式异常。');};
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/u;
function exact(value:unknown,keys:string[]):Record<string,unknown>{
 if(!value||typeof value!=='object'||Array.isArray(value))return invalid();
 const r=value as Record<string,unknown>;
 if(Object.keys(r).length!==keys.length||keys.some(k=>!Object.hasOwn(r,k)))return invalid();
 return r;
}
function number(value:unknown,min=0,max=Number.MAX_SAFE_INTEGER):number {
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<min||value>max)return invalid();
 return value;
}
function text(value:unknown,max:number):string {
 if(typeof value!=='string'||!value.length||value.length>max*2||[...value].length>max||/[\x00-\x1f]/u.test(value))return invalid();
 return value;
}
export function allowsOperationalReceipts(executionStatus?:string):boolean {
 // Active evidence is permitted only after a bound Host snapshot for a real running Run.
 return executionStatus==='running'||executionStatus==='completed'||executionStatus==='failed';
}
/** Accept only typed fields in an authorized same-Work/Run snapshot; never parse a model message. */
export function parseOperationalReceiptPage(value:unknown,{runId,state,sequenceNo,executionStatus}:{
 runId:string|null;state:unknown;sequenceNo:number;executionStatus?:string;
}):OperationalReceiptPage {
 if(!runId||state!=='available'||!allowsOperationalReceipts(executionStatus))return invalid();
 const b=exact(value,['type','block_id','sequence_no','action','offset','returned_count','reported_total','items']);
 if(b.type!=='operational_receipt_page'||typeof b.action!=='string'||!Object.hasOwn(operationalReceiptNames,b.action)||
    typeof b.block_id!=='string'||!uuid.test(b.block_id)||!Array.isArray(b.items))return invalid();
 const action=b.action as OperationalReceiptAction;
 const rowLimit=action==='purchase_receipts_query'?500:100;
 const offset=number(b.offset),returned=number(b.returned_count,0,rowLimit);
 if(b.items.length!==returned||!Number.isSafeInteger(offset+returned))return invalid();
 let total:number|null=null;
 if(b.reported_total!==null){
  if(action!=='purchase_receipts_query')return invalid();
  total=number(b.reported_total);
  if(returned&&offset+returned>total)return invalid();
 }
 const items=b.items.map(item=>{
  const r=exact(item,['bill_code','bill_date','bill_state']);
  const date=r.bill_date===null?null:text(r.bill_date,64);
  if(date!==null&&!sourceBusinessDay(date))return invalid();
  return {bill_code:text(r.bill_code,200),bill_date:date,bill_state:number(r.bill_state,Number.MIN_SAFE_INTEGER)};
 });
 return {type:'operational_receipt_page',block_id:b.block_id,sequence_no:number(b.sequence_no,1,sequenceNo),
  action,offset,returned_count:returned,reported_total:total,items};
}
