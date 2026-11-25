/**
 * Proposed UI boundary for the authorized public-only movement page.
 * This module does not request, expose, or enable any customer data by itself.
 */
export type CustomerReceivablesRow = {
  bill_date: string;
  bill_code: string | null;
  receivable_increase_reported: string | null;
  advance_receipt_increase_reported: string | null;
  amounts_withheld_by_source: boolean;
};
export type CustomerReceivablesPage = {
  type: 'customer_receivables_detail';
  block_id: string;
  sequence_no: number;
  start_date: string;
  end_date: string;
  returned_count: number;
  rows_omitted_unverified_date: number;
  items: CustomerReceivablesRow[];
};
type Target = {workId:string;runId:string;executionStatus:string};
const dayMs = 24 * 60 * 60 * 1000;
const bad = ():never => { throw Error('客户单据明细格式异常。'); };
function record(value:unknown):Record<string,unknown> {
  if(!value||typeof value!=='object'||Array.isArray(value))return bad();
  return value as Record<string,unknown>;
}
function shape(r:Record<string,unknown>,fields:string[]):void {
  if(Object.keys(r).length!==fields.length||!fields.every(name=>Object.hasOwn(r,name)))bad();
}
function count(value:unknown,lo:number,hi:number):number {
  if(typeof value!=='number'||!Number.isSafeInteger(value)||value<lo||value>hi)return bad();
  return value;
}
function day(value:unknown):string {
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||value.startsWith('0000-'))return bad();
  const instant=Date.parse(value);
  if(!Number.isFinite(instant)||new Date(instant).toISOString().slice(0,10)!==value)return bad();
  return value;
}
function visibleCode(value:unknown,max:number):string {
  if(typeof value!=='string'||!value||[...value].length>max||value!==value.trim()||/[\p{C}]/u.test(value))return bad();
  return value;
}
function amount(value:unknown):string|null {
  if(value===null)return null;
  // Canonical decimals arrive as strings. Never convert, round or assume currency.
  if(typeof value!=='string'||!/^(-?)(0|[1-9][0-9]{0,57})(\.[0-9]{1,18})?$/.test(value))return bad();
  return value;
}
function item(value:unknown,start:string,end:string):CustomerReceivablesRow {
  const r=record(value);
  shape(r,['bill_date','bill_code','receivable_increase_reported','advance_receipt_increase_reported','amounts_withheld_by_source']);
  const billDate=day(r.bill_date);
  if(billDate<start||billDate>end||typeof r.amounts_withheld_by_source!=='boolean')return bad();
  const receivable=amount(r.receivable_increase_reported),advance=amount(r.advance_receipt_increase_reported);
  if(r.amounts_withheld_by_source&&(receivable!==null||advance!==null))return bad();
  return {bill_date:billDate,bill_code:r.bill_code===null?null:visibleCode(r.bill_code,128),
    receivable_increase_reported:receivable,advance_receipt_increase_reported:advance,
    amounts_withheld_by_source:r.amounts_withheld_by_source};
}
export function parseAuthorizedCustomerReceivablesPage(rawBlock:unknown,envelopeContext:{
  work_id:unknown;run_id:unknown;state:unknown;sequence_no:unknown;block_count:unknown;
},target:Target):CustomerReceivablesPage {
  // The caller must already have verified current user/scope and the GET envelope.
  // Never infer disclosure from a successful tool receipt or model answer text.
  if(!target.workId||!target.runId||target.executionStatus!=='completed'||
     envelopeContext.work_id!==target.workId||envelopeContext.run_id!==target.runId||
     envelopeContext.state!=='available'||envelopeContext.block_count!==1)return bad();
  const r=record(rawBlock);
  shape(r,['type','block_id','sequence_no','start_date','end_date','returned_count','rows_omitted_unverified_date','items']);
  if(r.type!=='customer_receivables_detail'||!Array.isArray(r.items))return bad();
  const high=count(envelopeContext.sequence_no,1,Number.MAX_SAFE_INTEGER);
  const seq=count(r.sequence_no,1,high),returned=count(r.returned_count,0,50),omitted=count(r.rows_omitted_unverified_date,0,50);
  const start=day(r.start_date),end=day(r.end_date);
  if(returned!==r.items.length||returned+omitted>50||end<start||Date.parse(end)-Date.parse(start)>=31*dayMs)return bad();
  return {type:'customer_receivables_detail',block_id:visibleCode(r.block_id,100),sequence_no:seq,start_date:start,end_date:end,
    returned_count:returned,rows_omitted_unverified_date:omitted,items:r.items.map(row=>item(row,start,end))};
}
