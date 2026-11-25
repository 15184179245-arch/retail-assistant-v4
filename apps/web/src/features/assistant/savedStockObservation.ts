import type {SavedStockObservationStep} from './workProgress';

type Scope={workId:string;runId:string;stepId:string};
const fail=():never=>{throw Error('已保存的单仓库存格式异常。');};
const decimal=/^(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/u;
const utc=/^(\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d)(?:\.(\d{6}))?\+00:00$/u;
type StockFieldType='qty'|'subUnitQty';
export type SavedStockField={field:StockFieldType;unit:string|null}&
 ({presence:'value';raw_value:string;wire_type:'number'|'string'}|
  {presence:'missing';raw_value:null;wire_type:null}|{presence:'null';raw_value:null;wire_type:'null'});
export type SavedStockObservation={
 product:{code:string;name:string};warehouse_scope:'matched_warehouse'|'request_only';
 observed_started_at:string;observed_finished_at:string;stock_fields:SavedStockField[];
};
export function canReadSavedStockStep(step:SavedStockObservationStep,has:(capability:string)=>boolean){
 return has('agent.run')&&has('tasks.read_self')&&has('catalog.read')&&has('guanjia.inventory.read')&&
  (step.version==='1'||step.version==='2'&&has('guanjia.company.read')&&has('guanjia.warehouses.read'));
}
function exact(raw:unknown,fields:readonly string[]):Record<string,unknown>{
 if(!raw||typeof raw!=='object'||Array.isArray(raw))return fail();
 const value=raw as Record<string,unknown>;
 if(Object.keys(value).length!==fields.length||!fields.every(field=>Object.hasOwn(value,field)))return fail();
 return value;
}
function label(raw:unknown):string{
 if(typeof raw!=='string'||raw.length>400||[...raw].length<1||[...raw].length>200||/[\x00-\x1f\x7f-\x9f\p{Cs}]/u.test(raw))return fail();
 return raw;
}
function date(raw:unknown):{value:string;micros:string}{
 if(typeof raw!=='string'||raw.startsWith('0000-')||/[\s]/u.test(raw))return fail();
 const parts=utc.exec(raw),ms=Date.parse(raw);
 if(!parts||!Number.isFinite(ms)||new Date(ms).toISOString().slice(0,19)!==parts[1])return fail();
 return {value:raw,micros:parts[1]+'.'+(parts[2]??'000000')};
}
function quantity(raw:unknown):string{
 if(typeof raw!=='string'||raw.length>100||/[\s]/u.test(raw))return fail();
 const parts=decimal.exec(raw);if(!parts)return fail();
 const [,,whole,fraction='',exponent='0']=parts;
 const first=(whole+fraction).search(/[1-9]/u),adjusted=BigInt(exponent)+BigInt(first<0?-fraction.length:whole.length-first-1);
 if(adjusted>100n||adjusted< -100n)return fail();
 return raw;
}
function field(raw:unknown,expected:StockFieldType):SavedStockField{
 const value=exact(raw,['field','presence','raw_value','wire_type','unit']);
 if(value.field!==expected||expected==='qty'&&value.unit!==null)return fail();
 const unit=value.unit===null?null:label(value.unit);
 if(value.presence==='value'&&(value.wire_type==='number'||value.wire_type==='string'))
  return {field:expected,unit,presence:'value',raw_value:quantity(value.raw_value),wire_type:value.wire_type};
 if(value.raw_value!==null)return fail();
 if(value.presence==='null'&&value.wire_type==='null')return {field:expected,unit,presence:'null',raw_value:null,wire_type:'null'};
 if(value.presence==='missing'&&value.wire_type===null)return {field:expected,unit,presence:'missing',raw_value:null,wire_type:null};
 return fail();
}
export function parseSavedStockObservation(raw:unknown,scope:Scope):SavedStockObservation{
 const n=exact(raw,['version','origin','work_id','run_id','step_id','product','warehouse_scope','warehouse_name',
  'source_status','observed_started_at','observed_finished_at','source_updated_at','stock_fields','live','coverage_verified','availability_verified']);
 if(!scope.workId||!scope.runId||!scope.stepId||n.version!==1||n.origin!=='stored_warehouse_stock_observation'||n.work_id!==scope.workId||
    n.run_id!==scope.runId||n.step_id!==scope.stepId||n.warehouse_name!==null||n.source_updated_at!==null||n.live!==false||
    n.coverage_verified!==false||n.availability_verified!==false||!Array.isArray(n.stock_fields))return fail();
 const warehouse_scope=n.warehouse_scope;
 if(warehouse_scope==='request_only'&&(n.source_status!=='empty_unclassified'||n.stock_fields.length!==0)||
    warehouse_scope==='matched_warehouse'&&(n.source_status!=='observed_fields'||n.stock_fields.length<1||n.stock_fields.length>2)||
    warehouse_scope!=='request_only'&&warehouse_scope!=='matched_warehouse')return fail();
 const product=exact(n.product,['code','name']),start=date(n.observed_started_at),end=date(n.observed_finished_at);
 if(start.micros>end.micros)return fail();
 return {product:{code:label(product.code),name:label(product.name)},warehouse_scope,observed_started_at:start.value,observed_finished_at:end.value,
  stock_fields:n.stock_fields.map((raw,i)=>field(raw,i===0?'qty':'subUnitQty'))};
}
