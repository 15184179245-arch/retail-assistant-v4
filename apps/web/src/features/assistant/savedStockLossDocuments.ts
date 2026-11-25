type Scope={workId:string;runId:string;stepId:string};
type SavedStockLossItem={bill_code:string;bill_date:string;source_bill_state:number};
export type SavedStockLossPage={
 recorded_at:string;offset:number;requested_count:number;returned_count:number;items:SavedStockLossItem[];
};

export function canReadSavedStockLoss(has:(capability:string)=>boolean){
 return has('agent.run')&&has('tasks.read_self')&&has('guanjia.stock_losses.read')&&has('guanjia.company.read');
}
const invalid=():never=>{throw Error('已保存的报损单格式异常。');};
function exact(raw:unknown,fields:readonly string[]):Record<string,unknown>{
 if(!raw||typeof raw!=='object'||Array.isArray(raw))return invalid();
 const value=raw as Record<string,unknown>;
 if(Object.keys(value).length!==fields.length||!fields.every(field=>Object.hasOwn(value,field)))return invalid();
 return value;
}
function bounded(raw:unknown,min:number,max:number):number{
 if(typeof raw!=='number'||!Number.isSafeInteger(raw)||raw<min||raw>max)return invalid();
 return raw;
}
function sourceText(raw:unknown,max:number):string{
 if(typeof raw!=='string'||!raw.trim()||raw.length>max*2||[...raw].length>max||
    /[\x00-\x1f\x7f-\x9f\p{Cs}]/u.test(raw))return invalid();
 return raw;
}
function datePrefix(value:string){
 const date=value.slice(0,10);
 if(!/^(?!0000)\d{4}-\d{2}-\d{2}$/u.test(date))return false;
 const ms=Date.parse(date+'T00:00:00Z');
 return Number.isFinite(ms)&&new Date(ms).toISOString().slice(0,10)===date;
}
function sourceDay(raw:unknown):string{
 const value=sourceText(raw,64);
 // Keep the source bill-date spelling (including original time/offset) unmodified.
 if(!datePrefix(value)||(value.length!==10&&!(value.length>11&&['T',' '].includes(value[10]))))return invalid();
 return value;
}
function storedAt(raw:unknown):string{
 const value=sourceText(raw,32);
 if(!datePrefix(value)||!/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|\+00:00)$/u.test(value))return invalid();
 return value;
}

export function parseSavedStockLossPage(raw:unknown,scope:Scope):SavedStockLossPage{
 const n=exact(raw,['version','origin','work_id','run_id','step_id','recorded_at','offset','requested_count','returned_count',
  'all_pages_verified','inventory_effect_verified','live','items']);
 if(!scope.workId||!scope.runId||!scope.stepId||n.version!==1||n.origin!=='stored_stock_loss_document_page'||
    n.work_id!==scope.workId||n.run_id!==scope.runId||n.step_id!==scope.stepId||n.all_pages_verified!==false||
    n.inventory_effect_verified!==false||n.live!==false||!Array.isArray(n.items))return invalid();
 const offset=bounded(n.offset,0,100000),requested_count=bounded(n.requested_count,1,50),
  returned_count=bounded(n.returned_count,0,requested_count);
 if(n.items.length!==returned_count)return invalid();
 return {offset,requested_count,returned_count,recorded_at:storedAt(n.recorded_at),items:n.items.map(raw=>{
  const row=exact(raw,['bill_code','bill_date','source_bill_state']);
  return {bill_code:sourceText(row.bill_code,200),bill_date:sourceDay(row.bill_date),
   source_bill_state:bounded(row.source_bill_state,0,2147483647)};
 })};
}
