import {allowsOperationalReceipts} from './operationalReceiptPage';
import {sourceBusinessDay} from './publicResultDays';

export const operationalDetailNames={
 purchase_receipt_detail:'采购入库商品明细',
 sales_return_detail:'销售退货商品明细',
 purchase_return_detail:'采购退货商品明细',
} as const;
export const priceFields={
 reported_unit_price:'原单价',
 reported_line_amount:'原行金额',
 reported_discounted_unit_price:'折后单价',
 reported_discounted_line_amount:'折后行金额',
} as const;
export const priceStates={
 not_requested:'这次没有请求价格金额。',
 authority_unverified:'当前无法核实价格权限，未展示价格金额。',
 not_reported:'没有可展示的源价格金额。',
 partially_reported:'只展示有来源且当前允许查看的价格金额，缺失项不按零处理。',
 reported:'显示当前允许查看的源价格金额；币种、税口径和实付均未核实。',
 no_lines:'这次明细没有可展示的商品行。',
} as const;
export type ReceiptPriceAmounts=Record<keyof typeof priceFields,string|null>;
export type OperationalReceiptDetailLine={
 product_code:string|null;product_name:string|null;specification:string|null;
 reported_quantity:string|null;reported_unit:string|null;price_amounts:ReceiptPriceAmounts|null;
};
export type OperationalReceiptDetailPage={
 type:'operational_receipt_detail';block_id:string;sequence_no:number;action:keyof typeof operationalDetailNames;
 bill_code:string;bill_date:string|null;bill_state:number;returned_count:number;
 price_amount_state:keyof typeof priceStates;items:OperationalReceiptDetailLine[];
};
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/u;
const invalid=():never=>{throw Error('单据商品明细格式异常。');};
function exact(value:unknown,keys:readonly string[]):Record<string,unknown>{
 if(!value||typeof value!=='object'||Array.isArray(value))return invalid();
 const r=value as Record<string,unknown>;
 if(Object.keys(r).length!==keys.length||keys.some(key=>!Object.hasOwn(r,key)))return invalid();
 return r;
}
function integer(value:unknown,min:number,max:number):number {
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<min||value>max)return invalid();
 return value;
}
function text(value:unknown,max:number):string {
 if(typeof value!=='string'||value.length>2*max||[...value].length>max||!value.trim()||/[\x00-\x1f\x7f-\x9f]/u.test(value))return invalid();
 return value;
}
function nullableText(value:unknown,max:number):string|null{return value===null?null:text(value,max);}
function exactDecimal(value:unknown):string|null{
 if(value===null)return null;
 // A bounded fixed-format value may have more than 43 characters after
 // expanding a valid upstream exponent; render it verbatim.
 const raw=text(value,64);
 if(!/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$/u.test(raw))return invalid();
 return raw;
}
function originalDate(value:unknown):string|null {
 const s=nullableText(value,64);
 // Use the same ISO wall-date/date-time shape as the Host projector.
 if(s!==null&&!sourceBusinessDay(s))return invalid();
 return s;
}
const amountKeys=Object.keys(priceFields) as (keyof ReceiptPriceAmounts)[];
/** The Host is the authority. Enforce a bounded same-Run snapshot and reject inconsistent money flags. */
export function parseOperationalReceiptDetail(value:unknown,{runId,state,sequenceNo,executionStatus}:{
 runId:string|null;state:unknown;sequenceNo:number;executionStatus?:string;
}):OperationalReceiptDetailPage {
 if(!runId||state!=='available'||!allowsOperationalReceipts(executionStatus))return invalid();
 const b=exact(value,['type','block_id','sequence_no','action','bill_code','bill_date','bill_state','returned_count','price_amount_state','items']);
 if(b.type!=='operational_receipt_detail'||typeof b.action!=='string'||!Object.hasOwn(operationalDetailNames,b.action)||
    typeof b.block_id!=='string'||!uuid.test(b.block_id)||typeof b.price_amount_state!=='string'||
    !Object.hasOwn(priceStates,b.price_amount_state)||!Array.isArray(b.items))return invalid();
 const returned=integer(b.returned_count,0,b.action==='sales_return_detail'?250:500);
 if(b.items.length!==returned)return invalid();
 const priceState=b.price_amount_state as OperationalReceiptDetailPage['price_amount_state'];
 let amountCount=0;
 const items=b.items.map(item=>{
  const row=exact(item,['product_code','product_name','specification','reported_quantity','reported_unit','price_amounts']);
  let amounts:ReceiptPriceAmounts|null=null;
  if(row.price_amounts!==null){
   const a=exact(row.price_amounts,amountKeys);
   amounts={reported_unit_price:exactDecimal(a.reported_unit_price),reported_line_amount:exactDecimal(a.reported_line_amount),
    reported_discounted_unit_price:exactDecimal(a.reported_discounted_unit_price),reported_discounted_line_amount:exactDecimal(a.reported_discounted_line_amount)};
   const n=Object.values(amounts).filter(value=>value!==null).length;
   if(!n)return invalid();
   amountCount+=n;
  }
  // The protected purchase projection may prove the row and its quantity even
  // when the vendor supplies no catalog code/name. Other sources stay strict.
  const productText=(value:unknown)=>b.action==='purchase_return_detail'?nullableText(value,300):b.action==='purchase_receipt_detail'&&value===''?'':text(value,300);
  return {product_code:productText(row.product_code),product_name:productText(row.product_name),specification:nullableText(row.specification,300),
   reported_quantity:exactDecimal(row.reported_quantity),reported_unit:nullableText(row.reported_unit,120),price_amounts:amounts};
 });
 if(priceState==='reported'?(!returned||amountCount!==returned*amountKeys.length):
   priceState==='partially_reported'?(!amountCount||amountCount>=returned*amountKeys.length):
   (amountCount!==0||(priceState==='no_lines'&&returned!==0)))return invalid();
 return {type:'operational_receipt_detail',block_id:b.block_id,sequence_no:integer(b.sequence_no,1,sequenceNo),
  action:b.action as OperationalReceiptDetailPage['action'],bill_code:text(b.bill_code,200),bill_date:originalDate(b.bill_date),
  bill_state:integer(b.bill_state,Number.MIN_SAFE_INTEGER,Number.MAX_SAFE_INTEGER),
  returned_count:returned,price_amount_state:priceState,items};
}
