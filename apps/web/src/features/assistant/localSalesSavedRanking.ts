const origin='stored_local_sales_ranking';
const quantityRule='posted-signed-basic-quantity-excluding-explicit-gifts/1';
const fail=():never=>{throw Error('已保存的销量排行格式异常。');};
const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/u;
const china=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'});
export type SavedSalesMetric='net_qty'|'sale_qty'|'return_qty'|'gift_qty';
export type SalesRankingChoice={step_id:string;sequence_no:number;start_date:string;end_date:string;original_queried_at:string;
 metric:SavedSalesMetric;order:'asc'|'desc';offset:number};
export type SavedSalesRanking={step_id:string;start_date:string;end_date:string;original_queried_at:string;
 metric:SavedSalesMetric;order:'asc'|'desc';offset:number;latest_capture_at:string|null;archived_source_row_count:number;
 contains_open_day:boolean;returned_count:number;next_offset:number|null;total_product_count:number;
 rows:{position:number;product_code:string;product_name:string;base_unit:string;quantity:string;source_row_count:number}[]};
type Scope={workId:string;runId:string};
type Fields=Record<string,unknown>;
function record(value:unknown,keys:readonly string[]):Fields{
 if(!value||typeof value!=='object'||Array.isArray(value))return fail();
 const row=value as Fields;
 if(Object.keys(row).length!==keys.length||!keys.every(k=>Object.hasOwn(row,k)))return fail();
 return row;
}
function id(value:unknown):string{
 if(typeof value!=='string'||!uuid.test(value))return fail();
 return value;
}
function nonnegative(value:unknown,min=0,max=1_000_000):number{
 if(typeof value!=='number'||!Number.isSafeInteger(value)||value<min||value>max)return fail();
 return value;
}
function exactDay(value:unknown):string{
 if(typeof value!=='string'||!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(value)||value.startsWith('0000-'))return fail();
 const date=new Date(value+'T00:00:00Z');
 if(!Number.isFinite(date.valueOf())||date.toISOString().slice(0,10)!==value)return fail();
 return value;
}
function exactTime(value:unknown):string{
 if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/u.test(value)||!Number.isFinite(Date.parse(value)))return fail();
 exactDay(value.slice(0,10));return value;
}
function sourceDay(time:string){
 const parts=china.formatToParts(new Date(time));
 const part=(k:string)=>parts.find(p=>p.type===k)?.value||'';
 return part('year').padStart(4,'0')+'-'+part('month')+'-'+part('day');
}
function timestampTicks(time:string):bigint{
 const micros=time.match(/\.(\d{1,6})/u)?.[1].padEnd(6,'0').slice(3)||'000';
 return BigInt(Date.parse(time))*1000n+BigInt(micros);
}
const scopes=['version','mode','origin','work_id','run_id'];
const windowKeys=['start_date','end_date','original_queried_at','metric','order','offset'];
function scope(row:Fields,target:Scope,mode:'sources'|'ranking'){
 if(row.version!==1||row.origin!==origin||row.mode!==mode||id(row.work_id)!==target.workId||id(row.run_id)!==target.runId)fail();
}
function windowOf(row:Fields):Omit<SalesRankingChoice,'step_id'|'sequence_no'>{
 const start_date=exactDay(row.start_date),end_date=exactDay(row.end_date),original_queried_at=exactTime(row.original_queried_at);
 const metric=row.metric,order=row.order,offset=nonnegative(row.offset);
 if(start_date>end_date||end_date>sourceDay(original_queried_at)||Date.parse(end_date)-Date.parse(start_date)>=366*86_400_000||
    (metric!=='net_qty'&&metric!=='sale_qty'&&metric!=='return_qty'&&metric!=='gift_qty')||(order!=='asc'&&order!=='desc'))return fail();
 return {start_date,end_date,original_queried_at,metric,order,offset};
}
function safeLabel(value:unknown):string{
 if(typeof value!=='string'||value.length>400||[...value].length<1||[...value].length>200||/[\x00-\x1f\x7f-\x9f\p{Cs}]/u.test(value))return fail();
 return value;
}
/** Keep the signed decimal token byte-for-byte; no Number, rounding, or cross-unit arithmetic. */
function quantity(value:unknown):string{
 if(typeof value!=='string'||value.length>2400||/\s/u.test(value)||!/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/u.test(value))return fail();
 return value;
}
export function parseSavedSalesRankingChoices(raw:unknown,target:Scope):SalesRankingChoice[]{
 const row=record(raw,[...scopes,'choices']);scope(row,target,'sources');
 if(!Array.isArray(row.choices)||row.choices.length>64)return fail();
 const ids=new Set<string>();
 return row.choices.map(value=>{
  const n=record(value,['step_id','sequence_no',...windowKeys]),step_id=id(n.step_id);
  if(ids.has(step_id))return fail();ids.add(step_id);
  return {step_id,sequence_no:nonnegative(n.sequence_no,1,Number.MAX_SAFE_INTEGER),...windowOf(n)};
 });
}
export function parseSavedSalesRanking(raw:unknown,target:Scope,choice:SalesRankingChoice):SavedSalesRanking{
 const n=record(raw,[...scopes,'step_id',...windowKeys,'latest_capture_at','source_calendar','archived_source_row_count',
  'all_requested_days_archived','contains_open_day','monetary_totals_included','atomic_snapshot_verified','business_coverage_verified',
  'quantity_rule','returned_count','next_offset','total_product_count','rows']);
 scope(n,target,'ranking');
 if(id(n.step_id)!==choice.step_id||n.source_calendar!=='source_bill_date'||n.all_requested_days_archived!==true||
    n.monetary_totals_included!==false||n.atomic_snapshot_verified!==false||n.business_coverage_verified!==false||
    n.quantity_rule!==quantityRule)return fail();
 const window=windowOf(n);
 if(window.start_date!==choice.start_date||window.end_date!==choice.end_date||window.original_queried_at!==choice.original_queried_at||
    window.metric!==choice.metric||window.order!==choice.order||window.offset!==choice.offset)return fail();
 const contains_open_day=window.end_date===sourceDay(window.original_queried_at);
 const latest_capture_at=n.latest_capture_at===null?null:exactTime(n.latest_capture_at);
 if(n.contains_open_day!==contains_open_day||!latest_capture_at||timestampTicks(latest_capture_at)>timestampTicks(window.original_queried_at))return fail();
 const archived_source_row_count=nonnegative(n.archived_source_row_count),total_product_count=nonnegative(n.total_product_count,0,archived_source_row_count);
 const returned_count=nonnegative(n.returned_count,0,Math.min(1000,total_product_count-window.offset)),end=window.offset+returned_count;
 const next_offset=end<total_product_count?end:null;
 if((end<total_product_count&&returned_count===0)||n.next_offset!==next_offset||!Array.isArray(n.rows)||n.rows.length!==returned_count)return fail();
 let sourceRows=0;
 const rows=n.rows.map((value,index)=>{
  const item=record(value,['position','product_code','product_name','base_unit','quantity','source_row_count']);
  const position=window.offset+index+1;
  if(item.position!==position)return fail();
  const source_row_count=nonnegative(item.source_row_count,1,archived_source_row_count);
  sourceRows+=source_row_count;
  return {position,product_code:safeLabel(item.product_code),product_name:safeLabel(item.product_name),base_unit:safeLabel(item.base_unit),
   quantity:quantity(item.quantity),source_row_count};
 });
 if(sourceRows>archived_source_row_count||window.offset===0&&end===total_product_count&&sourceRows!==archived_source_row_count)return fail();
 return {...window,step_id:choice.step_id,latest_capture_at,archived_source_row_count,contains_open_day,returned_count,next_offset,total_product_count,rows};
}
