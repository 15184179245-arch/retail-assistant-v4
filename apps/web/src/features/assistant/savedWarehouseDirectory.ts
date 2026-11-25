type Scope={workId:string;runId:string;stepId:string};
type SavedWarehouseRow={code:string;name:string;source_is_active:boolean|null};
export type SavedWarehouseDirectoryPage={
 recorded_at:string;offset:number;requested_count:number;returned_count:number;items:SavedWarehouseRow[];
};

export function canReadSavedWarehouseDirectory(has:(capability:string)=>boolean){
 return has('agent.run')&&has('tasks.read_self')&&has('guanjia.warehouses.read')&&has('guanjia.company.read');
}
const invalid=():never=>{throw Error('已保存的仓库目录格式异常。');};
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
function sourceText(raw:unknown,max:number,allowEmpty=false):string{
 if(typeof raw!=='string'||raw.length>max*2||[...raw].length>max||
    /[\x00-\x1f\x7f\p{Cs}]/u.test(raw)||(raw===''?!allowEmpty:!raw.trim()))return invalid();
 return raw;
}
function storedAt(raw:unknown):string{
 const value=sourceText(raw,32);
 if(!/^(?!0000)(\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)(?:\.\d{1,6})?(?:Z|\+00:00)$/u.test(value))return invalid();
 const ms=Date.parse(value);
 if(!Number.isFinite(ms)||new Date(ms).toISOString().slice(0,19)!==value.slice(0,19))return invalid();
 return value;
}
export function parseSavedWarehouseDirectoryPage(raw:unknown,scope:Scope):SavedWarehouseDirectoryPage{
 const n=exact(raw,['version','origin','work_id','run_id','step_id','recorded_at','offset','requested_count','returned_count',
  'page_status','all_pages_verified','stock_quantity_verified','live','items']);
 if(!scope.workId||!scope.runId||!scope.stepId||n.version!==1||n.origin!=='stored_warehouse_directory_page'||
    n.work_id!==scope.workId||n.run_id!==scope.runId||n.step_id!==scope.stepId||n.all_pages_verified!==false||
    n.stock_quantity_verified!==false||n.live!==false||!Array.isArray(n.items))return invalid();
 const offset=bounded(n.offset,0,100000),requested_count=bounded(n.requested_count,1,100),
  returned_count=bounded(n.returned_count,0,requested_count);
 if(n.items.length!==returned_count||n.page_status!==(returned_count===0?'reported_empty_page':'reported_rows'))return invalid();
 return {offset,requested_count,returned_count,recorded_at:storedAt(n.recorded_at),items:n.items.map(raw=>{
  const row=exact(raw,['code','name','source_is_active']);
  const active=row.source_is_active;
  if(active!==true&&active!==false&&active!==null)return invalid();
  return {code:sourceText(row.code,120,true),name:sourceText(row.name,200),source_is_active:active};
 })};
}
