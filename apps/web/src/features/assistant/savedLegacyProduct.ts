type Scope={workId:string;runId:string;stepId:string;version:'1'|'2'};
type ProductFields={
 code:string;name:string;model:string|null;specification:string|null;brand_name:string|null;category_name:string|null;
 reported_purchase_unit_name:string|null;reported_sales_unit_name:string|null;subunit_name:string|null;
};
type Unit={name:string;reported_relation:string}&(
 {barcode_state:'not_in_version'|'not_reported';barcodes:null}|
 {barcode_state:'partial'|'reported';barcodes:string[]}
);
export type SavedLegacyProduct={recorded_at:string;product:ProductFields;units:Unit[]};

export function canReadSavedLegacyProduct(has:(capability:string)=>boolean){
 return has('agent.run')&&has('tasks.read_self')&&has('catalog.read')&&has('guanjia.catalog.read')&&has('guanjia.company.read');
}
const invalid=():never=>{throw Error('已保存的旧版商品记录格式异常。');};
const maxNested=500;
function exact(raw:unknown,fields:readonly string[]):Record<string,unknown>{
 if(!raw||typeof raw!=='object'||Array.isArray(raw))return invalid();
 const value=raw as Record<string,unknown>;
 if(Object.keys(value).length!==fields.length||!fields.every(field=>Object.hasOwn(value,field)))return invalid();
 return value;
}
function text(raw:unknown,max=200,reported=false):string{
 if(typeof raw!=='string'||!raw||raw.length>max*2||[...raw].length>max||/[\x00-\x1f\x7f-\x9f\p{Cs}]/u.test(raw)||
    (reported&&(!raw.trim()||/[\p{Z}\p{C}]/u.test(raw.replaceAll(' ','')))))return invalid();
 return raw;
}
function optional(raw:unknown,max=200,reported=false):string|null{
 return raw===null?null:text(raw,max,reported);
}
function storedAt(raw:unknown):string{
 const value=text(raw,32);
 if(!/^(?!0000)\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|\+00:00)$/u.test(value))return invalid();
 const ms=Date.parse(value);
 if(!Number.isFinite(ms)||new Date(ms).toISOString().slice(0,19)!==value.slice(0,19))return invalid();
 return value;
}
function relation(raw:unknown):string{
 const value=text(raw,100);
 const parts=/^(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/u.exec(value);
 if(!parts||/\s/u.test(value))return invalid();
 const [,whole,fraction='',exponent='0']=parts,first=(whole+fraction).search(/[1-9]/u);
 const adjusted=BigInt(exponent)+BigInt(whole.length-first-1);
 if(first<0||adjusted>100n||adjusted< -100n)return invalid();
 return value;
}
function unit(raw:unknown,version:Scope['version']):Unit{
 const n=exact(raw,['name','reported_relation','barcode_state','barcodes']);
 const base={name:text(n.name),reported_relation:relation(n.reported_relation)};
 if(version==='1')return n.barcode_state==='not_in_version'&&n.barcodes===null?{...base,barcode_state:'not_in_version',barcodes:null}:invalid();
 if(n.barcode_state==='not_reported')return n.barcodes===null?{...base,barcode_state:n.barcode_state,barcodes:null}:invalid();
 if((n.barcode_state!=='partial'&&n.barcode_state!=='reported')||!Array.isArray(n.barcodes)||n.barcodes.length>maxNested)return invalid();
 return {...base,barcode_state:n.barcode_state,barcodes:n.barcodes.map(code=>text(code,200,true))};
}
const v2Fields=['model','specification','brand_name','category_name','reported_purchase_unit_name','reported_sales_unit_name'] as const;
export function parseSavedLegacyProduct(raw:unknown,scope:Scope):SavedLegacyProduct{
 const n=exact(raw,['version','origin','work_id','run_id','step_id','recorded_at','legacy_tool_version',
  'live','barcode_completeness_verified','continuation_available','product','units']);
 if(!scope.workId||!scope.runId||!scope.stepId||!['1','2'].includes(scope.version)||n.legacy_tool_version!==scope.version||
    n.version!==1||n.origin!=='stored_legacy_product_result'||n.work_id!==scope.workId||n.run_id!==scope.runId||
    n.step_id!==scope.stepId||n.barcode_completeness_verified!==false||n.continuation_available!==false||n.live!==false||
    !Array.isArray(n.units)||n.units.length<1||n.units.length>maxNested)return invalid();
 const p=exact(n.product,['code','name',...v2Fields,'subunit_name']);
 if(scope.version==='1'&&v2Fields.some(key=>p[key]!==null))return invalid();
 const units=n.units.map(raw=>unit(raw,scope.version));
 const defaults=[optional(p.reported_purchase_unit_name),optional(p.reported_sales_unit_name)];
 if(defaults.some(name=>name!==null&&!units.some(unit=>unit.name===name)))return invalid();
 return {recorded_at:storedAt(n.recorded_at),product:{
  code:text(p.code),name:text(p.name),model:optional(p.model,200,true),specification:optional(p.specification,500,true),
  brand_name:optional(p.brand_name,200,true),category_name:optional(p.category_name,200,true),
  reported_purchase_unit_name:defaults[0],reported_sales_unit_name:defaults[1],subunit_name:optional(p.subunit_name),
 },units};
}
