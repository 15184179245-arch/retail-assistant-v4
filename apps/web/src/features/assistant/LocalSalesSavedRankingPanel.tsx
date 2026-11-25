import {useEffect,useId,useLayoutEffect,useRef,useState} from 'react';
import {parseSavedSalesRanking,parseSavedSalesRankingChoices,type SavedSalesMetric,type SavedSalesRanking,type SalesRankingChoice} from './localSalesSavedRanking';
import './local-sales-saved-ranking.css';

export type ReadSavedSalesRanking=(step:string|null,signal:AbortSignal)=>Promise<unknown>;
const groupsOf=25;
const metrics:Record<SavedSalesMetric,string>={net_qty:'净销量',sale_qty:'销售出库量',return_qty:'退货量',gift_qty:'赠品量'};
const orders={asc:'从小到大',desc:'从大到小'};
const metricNotes:Record<SavedSalesMetric,string>={
 net_qty:'净销量已计入过账退货与冲红，不含明确标记的赠品；不要再扣一次退货。',
 sale_qty:'只计非赠品销售出库及其冲红。',
 return_qty:'按净退货量口径，已计入退货冲红。',
 gift_qty:'只计来源明确标记为赠品的有符号数量。',
};

/** Only reads one originally signed page, never creates or extends an ERP ranking. */
export function LocalSalesSavedRankingPanel({workId,runId,ready,read}:{workId:string;runId:string;ready:boolean;read:ReadSavedSalesRanking}){
 const panel=useId(),field=useId(),job=useRef<AbortController|null>(null),previous=useRef<SalesRankingChoice|null>(null);
 const [open,setOpen]=useState(false),[pending,setPending]=useState(false),[notice,setNotice]=useState('');
 const [choices,setChoices]=useState<SalesRankingChoice[]|null>(null),[picked,setPicked]=useState(-1);
 const [ranking,setRanking]=useState<SavedSalesRanking|null>(null),[group,setGroup]=useState(0);
 const currentView=()=>ready&&document.visibilityState!=='hidden'&&navigator.onLine!==false;
 function stop(){const request=job.current;job.current=null;request?.abort();setPending(false);}
 function close(){
  stop();previous.current=null;setOpen(false);setChoices(null);setPicked(-1);setRanking(null);setGroup(0);setNotice('');
 }
 useLayoutEffect(()=>()=>{const request=job.current;job.current=null;request?.abort();},[workId,runId]);
 useLayoutEffect(()=>{
  if(ready||!open)return;
  stop();setChoices(null);setPicked(-1);setRanking(null);setGroup(0);
  setNotice('为核对当前查看权限，已隐藏上次阅读。核对后可手动继续。');
 },[ready,open]);
 useEffect(()=>{
  function hidden(){if(document.visibilityState==='hidden')close();}
  document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',close);
  return ()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',close);};
 },[]);
 async function load(choice:SalesRankingChoice|null){
  if(!currentView()||job.current)return;
  previous.current=choice;
  if(choice===null){setChoices(null);setPicked(-1);}
  const request=new AbortController();job.current=request;
  setPending(true);setRanking(null);setGroup(0);
  setNotice(choice?'正在核对所选的已存排行页…':'正在查看本次工作保存的排行页…');
  const current=()=>job.current===request&&!request.signal.aborted&&currentView();
  try{
   const raw=await read(choice?.step_id??null,request.signal);
   if(!current())return;
   if(choice){setRanking(parseSavedSalesRanking(raw,{workId,runId},choice));setNotice('');}
   else{
    const result=parseSavedSalesRankingChoices(raw,{workId,runId});
    setChoices(result);setPicked(-1);
    setNotice(result.length?'请选择原排行页，再查看。':'当前没有可查看的已存排行页；这不表示没有销售。');
   }
  }catch{
   if(current()){
    previous.current=null;setChoices(null);setPicked(-1);setRanking(null);
    setNotice('目前无法核实这次保存的销售排行。没有重新查询。');
   }
  }finally{if(job.current===request){job.current=null;setPending(false);}}
 }
 const selected=choices?.[picked],start=group*groupsOf,groups=ranking?Math.ceil(ranking.rows.length/groupsOf):0;
 return <article className="ar-card ar-saved-sales-ranking" aria-label="已保存的销量排行">
  <button type="button" className="ar-read" disabled={!ready&&!open} aria-expanded={open} aria-controls={panel}
   onClick={()=>{if(open)close();else if(currentView()){setOpen(true);void load(null);}}}>
   {open?'收起销量排行':'查找本次已保存的销量排行'}
  </button>
  <div id={panel} hidden={!open}>{open&&<>
   <p>只看本次保存的排行原页。不是实时数据，不会重新发起查询。</p>
   {notice&&<p role="status">{notice}</p>}
   {ready&&choices&&choices.length>0&&<div className="ar-saved-sales-select">
    <label htmlFor={field}>原排行范围及页</label>
    <select id={field} value={picked} disabled={pending} onChange={e=>{
     setPicked(Number(e.target.value));previous.current=null;setRanking(null);setGroup(0);
    }}>
     <option value={-1}>选择一页</option>
     {choices.map((choice,index)=><option key={choice.step_id} value={index}>
      {choice.start_date} 至 {choice.end_date} · {metrics[choice.metric]} · 从第 {choice.offset+1} 位起 · 第 {choice.sequence_no} 步
     </option>)}
    </select>
    <button type="button" className="ar-read" disabled={!selected||pending} onClick={()=>{if(selected)void load(selected);}}>核对并查看原页</button>
   </div>}
   {ready&&!pending&&choices===null&&<>
    {!ranking&&previous.current&&<button type="button" className="ar-read" onClick={()=>void load(previous.current)}>重新核对并查看上次所选原页</button>}{' '}
    <button type="button" className="ar-read" onClick={()=>void load(null)}>重新查看已存页清单</button>
   </>}
   {ready&&ranking&&<section aria-label="已存销量排行原页">
    <h3>原排行：{ranking.start_date} 至 {ranking.end_date}</h3>
    <p>{metrics[ranking.metric]} · 原页按数量{orders[ranking.order]}排列 · 范围按来源单据日期。</p>
    <p>原查询时间：<time dateTime={ranking.original_queried_at}>{ranking.original_queried_at}</time>。
     {ranking.latest_capture_at&&<>最近一次采集：<time dateTime={ranking.latest_capture_at}>{ranking.latest_capture_at}</time>。</>}
    </p>
    {ranking.contains_open_day&&<p>原查询时最后一天还没结束，之后的变化不在这份原页里。</p>}
    <p>{metricNotes[ranking.metric]} 每项保留原排行数量和该商品的基本单位。不同单位不换算、不相加，也不作统一件数对比。这里不显示金额。</p>
    <p>这只是已保存的一页，不代表全量销售。{ranking.returned_count?
     <>本页第 {ranking.offset+1} 至 {ranking.offset+ranking.returned_count} 位；原查询记录共 {ranking.total_product_count} 个商品。</>:
     <>原偏移 {ranking.offset}，本页未返回商品。原查询记录共 {ranking.total_product_count} 个商品，不代表没有销售。</>}
     {ranking.offset>0||ranking.next_offset!==null?'原排行还有其他位置的商品，这里不读取其他页。':''}</p>
    {ranking.rows.length>0&&<ol className="ar-saved-sales-ranking-rows" aria-label="原页商品、数量与单位">
     {ranking.rows.slice(start,start+groupsOf).map(row=><li key={row.position} value={row.position}>
      <bdi className="ar-saved-sales-product">{row.product_name}</bdi> <span>编码 <bdi>{row.product_code}</bdi></span>
      <dl><div><dt>原排行数量</dt><dd><bdi>{row.quantity}</bdi></dd></div>
       <div><dt>基本单位</dt><dd><bdi>{row.base_unit}</bdi></dd></div></dl>
     </li>)}
    </ol>}
    {groups>1&&<nav aria-label="原页内浏览">
     <button type="button" className="ar-read" disabled={group===0} onClick={()=>setGroup(n=>n-1)}>上一组</button>{' '}
     <span role="status">已存页内第 {group+1} / {groups} 组</span>{' '}
     <button type="button" className="ar-read" disabled={group+1>=groups} onClick={()=>setGroup(n=>n+1)}>下一组</button>
    </nav>}
   </section>}
  </>}</div>
 </article>;
}
