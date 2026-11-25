export type PurchaseQuantitySource={
 step_id:string;sequence_no:number;bill_code:string;bill_date:string;source_bill_state:number;
 returned_count:number;source_recorded_at:string;
};
type PurchaseQuantityLine={
 ordinal:number;product_code:string;product_name:string;specification:string|null;quantity:string|null;unit:string|null;
};
export type PurchaseQuantityDocument=PurchaseQuantitySource&{items:PurchaseQuantityLine[]};
const candidateStates=[
 'same_reported_unit_name_only','reported_unit_names_differ','unit_not_reported','quantity_not_reported',
 'multiple_source_rows','only_in_selected_order','only_in_selected_receipt',
] as const;
type CandidateState=typeof candidateStates[number];
export type PurchaseQuantityCandidate={product_code:string;order_ordinals:number[];receipt_ordinals:number[];state:CandidateState};
export type PurchaseQuantitySources={orders:PurchaseQuantitySource[];receipts:PurchaseQuantitySource[]};
// Transient continuity only; do not retain source metadata or quantities across authorization rechecks.
export type PurchaseQuantityChoices={order:string|null;receipt:string|null};
type SelectedSource=PurchaseQuantitySource|string;
const selectedId=(source:SelectedSource)=>typeof source==='string'?source:source.step_id;
export type PurchaseQuantityPair={order:PurchaseQuantityDocument;receipt:PurchaseQuantityDocument;same_code_candidates:PurchaseQuantityCandidate[]};
type Scope={workId:string;runId:string};
const sourceFields=['step_id','sequence_no','bill_code','bill_date','source_bill_state','returned_count','source_recorded_at'];
const envelopeFields=['version','work_id','run_id','mode'];
const bad=():never=>{throw Error('采购原值对照格式异常。');};

export function canOpenPurchaseQuantityComparison(has:(capability:string)=>boolean):boolean{
 return ['guanjia.company.read','guanjia.purchase_orders.read','guanjia.purchase_order_detail.read',
  'guanjia.purchase_receipts.read','guanjia.purchase_receipt_detail.read'].every(has);
}
function record(value:unknown,keys:string[]):Record<string,unknown>{
 if(!value||typeof value!=='object'||Array.isArray(value))return bad();
 const r=value as Record<string,unknown>;
 if(Object.keys(r).length!==keys.length||!keys.every(key=>Object.hasOwn(r,key)))return bad();
 return r;
}
function text(value:unknown,max:number,min=1):string{
 if(typeof value!=='string'||value.length>max*2||[...value].length<min||[...value].length>max||/[\x00-\x1f\x7f\p{Cs}]/u.test(value))return bad();
 return value;
}
function integer(value:unknown,min=0,max=Number.MAX_SAFE_INTEGER):number{
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<min||value>max)return bad();
 return value;
}
function array(value:unknown,max:number):unknown[]{
 if(!Array.isArray(value)||value.length>max)return bad();
 return value;
}
function envelope(r:Record<string,unknown>,scope:Scope,mode:'sources'|'comparison'){
 if(!scope.workId||!scope.runId||r.version!==1||r.mode!==mode||r.work_id!==scope.workId||r.run_id!==scope.runId)bad();
}
function source(r:Record<string,unknown>,maxCount:number):PurchaseQuantitySource {
 const time=text(r.source_recorded_at,40);
 if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/u.test(time)||!Number.isFinite(Date.parse(time)))return bad();
 return {step_id:text(r.step_id,120),sequence_no:integer(r.sequence_no,1),bill_code:text(r.bill_code,200),
  bill_date:text(r.bill_date,64),source_bill_state:integer(r.source_bill_state,Number.MIN_SAFE_INTEGER),
  returned_count:integer(r.returned_count,0,maxCount),source_recorded_at:time};
}
export function parsePurchaseQuantitySources(value:unknown,scope:Scope):PurchaseQuantitySources{
 const r=record(value,[...envelopeFields,'orders','receipts']);
 envelope(r,scope,'sources');
 const orders=array(r.orders,512).map(row=>source(record(row,sourceFields),250));
 const receipts=array(r.receipts,512).map(row=>source(record(row,sourceFields),500));
 const all=[...orders,...receipts];
 if(new Set(all.map(row=>row.step_id)).size!==all.length)return bad();
 return {orders,receipts};
}
function quantity(raw:unknown):string|null{
 if(raw===null)return null;
 // Decimal source tokens, not JS numbers. Preserve trailing zeros, negatives and precision.
 if(typeof raw!=='string'||! /^-?(?:0|[1-9][0-9]{0,60})(?:\.[0-9]{1,18})?$/u.test(raw))return bad();
 return raw;
}
function row(raw:unknown,index:number):PurchaseQuantityLine{
 const r=record(raw,['ordinal','product_code','product_name','specification','quantity','unit']);
 if(r.ordinal!==index+1)return bad();
 return {ordinal:index+1,product_code:text(r.product_code,300,0),product_name:text(r.product_name,300,0),
  specification:r.specification===null?null:text(r.specification,300,0),quantity:quantity(r.quantity),
  unit:r.unit===null?null:text(r.unit,120)};
}
function document(raw:unknown,selected:SelectedSource,max:number):PurchaseQuantityDocument{
 const r=record(raw,[...sourceFields,'items']);
 const observed=source(r,max),items=array(r.items,max).map(row);
 if(items.length!==observed.returned_count||observed.step_id!==selectedId(selected)||typeof selected!=='string'&&sourceFields.some(field=>{
  const key=field as keyof PurchaseQuantitySource;return observed[key]!==selected[key];
 }))return bad();
 return {...observed,items};
}
function checkedOrdinals(value:unknown,code:string,doc:PurchaseQuantityDocument):number[]{
 let last=0;
 return array(value,doc.items.length).map(item=>{
  const n=integer(item,last+1,doc.items.length);
  if(doc.items[n-1]?.product_code!==code)return bad();
  last=n;return n;
 });
}
function expectedState(o:PurchaseQuantityLine[],r:PurchaseQuantityLine[]):CandidateState{
 if(!o.length)return 'only_in_selected_receipt';
 if(!r.length)return 'only_in_selected_order';
 if(o.length!==1||r.length!==1)return 'multiple_source_rows';
 if(o[0].unit===null||r[0].unit===null)return 'unit_not_reported';
 if(o[0].unit!==r[0].unit)return 'reported_unit_names_differ';
 if(o[0].quantity===null||r[0].quantity===null)return 'quantity_not_reported';
 return 'same_reported_unit_name_only';
}
/** Check two exact choices, never interpret a cross-source relationship or compute amounts. */
export function parsePurchaseQuantityPair(value:unknown,scope:Scope,selection:{order:SelectedSource;receipt:SelectedSource}):PurchaseQuantityPair{
 const r=record(value,[...envelopeFields,'order','receipt','same_code_candidates','bill_relationship','coverage_verified',
  'fulfillment_verified','inventory_effect_verified','notice']);
 envelope(r,scope,'comparison');
 if(r.bill_relationship!=='not_verified'||r.coverage_verified!==false||r.fulfillment_verified!==false||r.inventory_effect_verified!==false||
    selectedId(selection.order)===selectedId(selection.receipt))return bad();
 text(r.notice,600);
 const order=document(r.order,selection.order,250),receipt=document(r.receipt,selection.receipt,500);
 const seen=new Set<string>();
 const same_code_candidates=array(r.same_code_candidates,order.items.length+receipt.items.length).map((v):PurchaseQuantityCandidate=>{
  const x=record(v,['product_code','order_ordinals','receipt_ordinals','state']),code=text(x.product_code,300);
  const orderOrdinals=checkedOrdinals(x.order_ordinals,code,order),receiptOrdinals=checkedOrdinals(x.receipt_ordinals,code,receipt);
  if(seen.has(code)||!orderOrdinals.length&&!receiptOrdinals.length||
     orderOrdinals.length!==order.items.filter(i=>i.product_code===code).length||
     receiptOrdinals.length!==receipt.items.filter(i=>i.product_code===code).length)return bad();
  seen.add(code);
  const state=expectedState(orderOrdinals.map(i=>order.items[i-1]),receiptOrdinals.map(i=>receipt.items[i-1]));
  if(x.state!==state)return bad();
  return {product_code:code,order_ordinals:orderOrdinals,receipt_ordinals:receiptOrdinals,state};
 });
 // The Host ignores only codes containing no non-whitespace source
 // characters. Do not normalize distinct code, unit, or quantity strings.
 const hasSourceCode=/[^\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u;
 const expectedCodes=new Set([...order.items,...receipt.items].map(row=>row.product_code).filter(code=>hasSourceCode.test(code)));
 if(expectedCodes.size!==seen.size||[...seen].some(code=>!expectedCodes.has(code)))return bad();
 return {order,receipt,same_code_candidates};
}
