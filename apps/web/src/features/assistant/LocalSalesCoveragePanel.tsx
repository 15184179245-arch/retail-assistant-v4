import {useEffect,useId,useLayoutEffect,useRef,useState} from 'react';
import {parseStoredSalesChoices,parseStoredSalesCoverage,type StoredSalesChoice,type StoredSalesCoverage} from './localSalesCoverage';

export type ReadStoredSalesCoverage=(stepId:string|null,signal:AbortSignal)=>Promise<unknown>;
const daysPerPage=30;
const dateStatus={recorded:'已封存有记录',reported_zero:'已封存，来源返回 0 行',missing:'尚无已封存来源'};

/** Keep every request/row in this view, and make no ERP queries on behalf of it. */
export function LocalSalesCoveragePanel({workId,runId,ready,read}:{workId:string;runId:string;ready:boolean;read:ReadStoredSalesCoverage}) {
 const panel=useId(),field=useId(),job=useRef<AbortController|null>(null);
 const lastRequested=useRef<StoredSalesChoice|null>(null);
 const [open,setOpen]=useState(false),[pending,setPending]=useState(false),[notice,setNotice]=useState('');
 const [choices,setChoices]=useState<StoredSalesChoice[]|null>(null),[picked,setPicked]=useState('');
 const [coverage,setCoverage]=useState<StoredSalesCoverage|null>(null),[page,setPage]=useState(0);
 const scope={workId,runId};
 const present=()=>ready&&document.visibilityState!=='hidden'&&navigator.onLine!==false;
 function close(){
  job.current?.abort();job.current=null;setOpen(false);setPending(false);setChoices(null);setPicked('');
  lastRequested.current=null;setCoverage(null);setPage(0);setNotice('');
 }
 useLayoutEffect(()=>()=>{job.current?.abort();job.current=null;},[workId,runId]);
 useLayoutEffect(()=>{
  if(ready||!open)return;
  job.current?.abort();job.current=null;
  setPending(false);setChoices(null);setPicked('');setCoverage(null);setPage(0);
  setNotice('为核对当前查看权限，已隐藏上次阅读。核对后可手动继续。');
 },[ready,open]);
 useEffect(()=>{
  function visibility(){if(document.visibilityState==='hidden')close();}
  document.addEventListener('visibilitychange',visibility);window.addEventListener('offline',close);
  return ()=>{document.removeEventListener('visibilitychange',visibility);window.removeEventListener('offline',close);};
 },[]);
 async function load(choice:StoredSalesChoice|null){
  if(job.current||!present())return;
  lastRequested.current=choice;
  const active=new AbortController();job.current=active;setPending(true);setCoverage(null);setPage(0);
  setNotice(choice?'正在核对所选的已保存资料…':'正在找本次工作已保存的覆盖记录…');
  const current=()=>job.current===active&&!active.signal.aborted&&present();
  try{
   const data=await read(choice?.step_id??null,active.signal);
   if(!current())return;
   if(choice!==null){setCoverage(parseStoredSalesCoverage(data,scope,choice));setNotice('');}
   else{
    const sources=parseStoredSalesChoices(data,scope);setChoices(sources);
    setPicked(sources.length===1?sources[0].step_id:'');
    setNotice(sources.length?'请选择要查看的原查询。':'本次工作没有可展示的已保存销售覆盖。没有重新查询。');
   }
  }catch{
   if(current()){
    lastRequested.current=null;setChoices(null);setPicked('');setCoverage(null);
    setNotice('目前无法展示本次已保存的销售覆盖。没有重新查询。');
   }
  }finally{
   if(job.current===active){job.current=null;setPending(false);}
  }
 }
 function toggle(){if(open){close();return;}setOpen(true);void load(null);}
 const chosen=choices?.find(choice=>choice.step_id===picked);
 const pageCount=coverage?Math.ceil(coverage.days.length/daysPerPage):0;
 return <article className="ar-card" aria-label="已保存的销售覆盖">
  <button type="button" className="ar-read" disabled={!ready&&!open} aria-expanded={open} aria-controls={panel} onClick={toggle}>
   {open?'收起销售覆盖':'查看本次已保存的销售覆盖'}</button>
  <div id={panel} hidden={!open}>{open&&<>
   <p>只查看本次保存的过账销售资料，不是实时查询，不会重新发起 ERP 查询。</p>
   {notice&&<p role="status">{notice}</p>}
   {ready&&choices&&choices.length>0&&<>
    <label htmlFor={field}>原查询范围</label>
    <select id={field} value={picked} disabled={pending} onChange={e=>{setPicked(e.target.value);setCoverage(null);setPage(0);}}>
     <option value="">选择一份已保存的覆盖记录</option>
     {choices.map(choice=><option key={choice.step_id} value={choice.step_id}>
      {choice.start_date} 至 {choice.end_date} · 第 {choice.sequence_no} 步
     </option>)}
    </select>
    <button type="button" className="ar-read" disabled={!chosen||pending} onClick={()=>{if(chosen)void load(chosen);}}>核对并查看</button>
   </>}
   {ready&&!pending&&choices===null&&<>
    {!coverage&&lastRequested.current&&<button type="button" className="ar-read" onClick={()=>void load(lastRequested.current)}>重新核对并查看上次选择</button>}
    <button type="button" className="ar-read" onClick={()=>void load(null)}>重新核对本次已存记录</button>
   </>}
   {ready&&coverage&&<section aria-label="按原单据日期的存档情况">
    <h3>原查询：{coverage.start_date} 至 {coverage.end_date}</h3>
    <p>营业日期按北京时间划分；查询和采集时刻显示原记录时区，例如 +00:00 为 UTC。</p>
    <p>原查询时间：<time dateTime={coverage.original_queried_at}>{coverage.original_queried_at}</time>。</p>
    <p>这份档案最近一次采集时间：{coverage.latest_capture_at?<time dateTime={coverage.latest_capture_at}>{coverage.latest_capture_at}</time>:'未记录'}。</p>
    <p>这份档案共有 {coverage.archived_source_row_count} 行。{coverage.all_requested_days_archived?
     '原查询范围内逐日均有封存记录，不代表完整业务覆盖或同时采集。':'原查询范围内还有未封存日期，不能按零计。'}</p>
    <ul>{coverage.days.slice(page*daysPerPage,(page+1)*daysPerPage).map(d=>
     <li key={d.day}><time dateTime={d.day}>{d.day}</time> · {dateStatus[d.status]}
      {d.status==='recorded'?' '+d.source_row_count+' 行':''}
      {d.was_open_at_query?' · 原查询时当日尚未结束':''}
      {d.source_captured_at&&<> · 采集时间 <time dateTime={d.source_captured_at}>{d.source_captured_at}</time></>}
     </li>)}</ul>
    {pageCount>1&&<div role="group" aria-label="日期翻页">
     <button type="button" disabled={page===0} onClick={()=>setPage(n=>n-1)}>上一页</button>
     <span aria-live="polite">第 {page+1} / {pageCount} 页</span>
     <button type="button" disabled={page+1>=pageCount} onClick={()=>setPage(n=>n+1)}>下一页</button>
    </div>}
   </section>}
  </>}</div>
 </article>;
}
