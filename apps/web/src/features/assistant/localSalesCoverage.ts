const origin='stored_local_sales_coverage';
const invalid=():never=>{throw Error('已保存的销售覆盖格式异常。');};
const idPattern=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/u;
const chinaCalendar=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'});
const dayMs=86_400_000;
type Scope={workId:string;runId:string};
export type StoredSalesChoice={step_id:string;sequence_no:number;start_date:string;end_date:string;original_queried_at:string};
export type StoredSalesDay={day:string;status:'missing'|'reported_zero'|'recorded';
 source_row_count:number|null;source_captured_at:string|null;was_open_at_query:boolean};
export type StoredSalesCoverage={start_date:string;end_date:string;original_queried_at:string;latest_capture_at:string|null;
 archived_source_row_count:number;all_requested_days_archived:boolean;days:StoredSalesDay[]};

export function mayReadStoredSalesCoverage(has:(capability:string)=>boolean):boolean {
 return has('guanjia.company.read')&&has('guanjia.sales.read');
}
function fields(raw:unknown,keys:readonly string[]):Record<string,unknown>{
 if(!raw||typeof raw!=='object'||Array.isArray(raw))return invalid();
 const row=raw as Record<string,unknown>;
 if(Object.keys(row).length!==keys.length||!keys.every(k=>Object.hasOwn(row,k)))return invalid();
 return row;
}
function id(raw:unknown):string{
 if(typeof raw!=='string'||!idPattern.test(raw))return invalid();
 return raw;
}
function day(raw:unknown):string {
 if(typeof raw!=='string'||!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/u.test(raw)||raw.startsWith('0000-'))return invalid();
 const value=new Date(raw+'T00:00:00Z');
 if(!Number.isFinite(value.valueOf())||value.toISOString().slice(0,10)!==raw)return invalid();
 return raw;
}
function instant(raw:unknown):string {
 if(typeof raw!=='string'||!/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/u.test(raw))return invalid();
 day(raw.slice(0,10));
 if(!Number.isFinite(Date.parse(raw)))return invalid();
 return raw;
}
function tick(raw:string) {
 const fraction=raw.match(/\.(\d{1,6})/u)?.[1].padEnd(6,'0').slice(3)||'000';
 return BigInt(Date.parse(raw))*1000n+BigInt(fraction);
}
function chinaDay(raw:string) {
 const parts=chinaCalendar.formatToParts(new Date(raw));
 const pick=(name:string)=>parts.find(p=>p.type===name)?.value||'';
 return pick('year').padStart(4,'0')+'-'+pick('month')+'-'+pick('day');
}
function integer(raw:unknown,min=0,max=1_000_000):number{
 if(typeof raw!=='number'||!Number.isSafeInteger(raw)||raw<min||raw>max)return invalid();
 return raw;
}
const common=['version','mode','origin','work_id','run_id'];
const windowKeys=['start_date','end_date','original_queried_at'];
function scope(row:Record<string,unknown>,target:Scope,mode:'sources'|'coverage') {
 if(row.version!==1||row.mode!==mode||row.origin!==origin||id(row.work_id)!==target.workId||id(row.run_id)!==target.runId)invalid();
}
function observationWindow(row:Record<string,unknown>) {
 const start_date=day(row.start_date),end_date=day(row.end_date),original_queried_at=instant(row.original_queried_at);
 const start=Date.parse(start_date),span=(Date.parse(end_date)-start)/dayMs+1;
 if(span<1||span>366||end_date>chinaDay(original_queried_at))return invalid();
 return {start_date,end_date,original_queried_at,span,start};
}
export function parseStoredSalesChoices(raw:unknown,target:Scope):StoredSalesChoice[]{
 const row=fields(raw,[...common,'choices']);scope(row,target,'sources');
 if(!Array.isArray(row.choices)||row.choices.length>64)return invalid();
 const seen=new Set<string>(),steps=new Set<number>();
 return row.choices.map(item=>{
  const choice=fields(item,['step_id','sequence_no',...windowKeys]);
  const step_id=id(choice.step_id),sequence_no=integer(choice.sequence_no,1);
  if(seen.has(step_id)||steps.has(sequence_no))return invalid();
  seen.add(step_id);steps.add(sequence_no);
  const {start_date,end_date,original_queried_at}=observationWindow(choice);
  return {step_id,sequence_no,start_date,end_date,original_queried_at};
 });
}
export function parseStoredSalesCoverage(raw:unknown,target:Scope,choice:StoredSalesChoice):StoredSalesCoverage{
 const row=fields(raw,[...common,'step_id','source_calendar',...windowKeys,'latest_capture_at','archived_source_row_count',
  'all_requested_days_archived','atomic_snapshot_verified','business_coverage_verified','days']);
 scope(row,target,'coverage');
 if(id(row.step_id)!==choice.step_id||row.source_calendar!=='source_bill_date'||
    row.atomic_snapshot_verified!==false||row.business_coverage_verified!==false)return invalid();
 const {start_date,end_date,original_queried_at,span,start}=observationWindow(row);
 if(start_date!==choice.start_date||end_date!==choice.end_date||original_queried_at!==choice.original_queried_at||
    !Array.isArray(row.days)||row.days.length!==span)return invalid();
 const latest_capture_at=row.latest_capture_at===null?null:instant(row.latest_capture_at);
 const archiveTotal=integer(row.archived_source_row_count);
 const upper=tick(original_queried_at),openDay=chinaDay(original_queried_at);
 let last:bigint|null=null,total=0,missing=0;
 const days=row.days.map((value,index):StoredSalesDay=>{
  const item=fields(value,['day','status','source_row_count','source_captured_at','was_open_at_query']);
  const date=day(item.day),isOpen=date===openDay;
  if(date!==new Date(start+index*dayMs).toISOString().slice(0,10)||item.was_open_at_query!==isOpen)return invalid();
  if(item.status==='missing'){
   if(item.source_row_count!==null||item.source_captured_at!==null)return invalid();
   missing++;
   return {day:date,status:'missing',source_row_count:null,source_captured_at:null,was_open_at_query:isOpen};
  }
  if(item.status!=='recorded'&&item.status!=='reported_zero')return invalid();
  const source_row_count=integer(item.source_row_count,item.status==='recorded'?1:0,item.status==='recorded'?1_000_000:0);
  const source_captured_at=instant(item.source_captured_at),captured=tick(source_captured_at);
  if(captured>upper)return invalid();
  if(last===null||captured>last)last=captured;
  total+=source_row_count;
  return {day:date,status:item.status,source_row_count,source_captured_at,was_open_at_query:isOpen};
 });
 if((latest_capture_at===null?null:tick(latest_capture_at))!==last||
    total!==archiveTotal||row.all_requested_days_archived!==(missing===0))return invalid();
 return {start_date,end_date,original_queried_at,latest_capture_at,archived_source_row_count:archiveTotal,
  all_requested_days_archived:missing===0,days};
}
