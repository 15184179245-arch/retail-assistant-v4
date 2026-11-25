import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {usePath} from '../../app/router';
import {parseSavedStockLossPage,type SavedStockLossPage} from './savedStockLossDocuments';
import type {SavedStockLossStep} from './workProgress';

export function SavedStockLossDocumentsCard({workId,runId,ready,steps,read}:{workId:string;runId:string;ready:boolean;
 steps:SavedStockLossStep[];read:(stepId:string,signal:AbortSignal)=>Promise<unknown>}){
 const path=usePath(),panel=useRef<HTMLElement>(null),job=useRef<AbortController|null>(null);
 const [working,setWorking]=useState<SavedStockLossStep|null>(null),[notice,setNotice]=useState('');
 const [view,setView]=useState<{step:SavedStockLossStep;page:SavedStockLossPage}|null>(null);
 const allowed=(original:SavedStockLossStep)=>steps.some(step=>step.id===original.id&&step.sequence===original.sequence&&
  step.outputDigest===original.outputDigest);
 const current=()=>ready&&document.visibilityState!=='hidden'&&navigator.onLine!==false&&
  !!panel.current&&!panel.current.closest('[hidden]');
 function reset(message=''){
  const active=job.current;job.current=null;active?.abort();setWorking(null);setView(null);setNotice(message);
 }
 useLayoutEffect(()=>{
  reset();
  return ()=>{const active=job.current;job.current=null;active?.abort();};
 },[workId,runId,path]);
 const shown=ready&&view&&allowed(view.step)?view:null;
 const witness=steps.map(step=>[step.id,step.sequence,step.outputDigest].join(':')).join(',');
 useLayoutEffect(()=>{
  if((!ready&&(working||view))||(working&&!allowed(working))||(view&&!allowed(view.step)))
   reset('为核对查看权限，暂不展示上次记录。可手动再查看。');
 },[ready,witness]);
 useEffect(()=>{
  function hidden(){if(document.visibilityState==='hidden')reset();}
  function offline(){reset();}
  document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',offline);
  return ()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',offline);};
 },[]);
 async function show(step:SavedStockLossStep){
  if(!current()||!allowed(step)||working?.id===step.id)return;
  reset('正在核对这一步保存的报损单页…');
  const request=new AbortController();job.current=request;setWorking(step);
  const live=()=>job.current===request&&!request.signal.aborted&&current();
  try{
   const data=await read(step.id,request.signal);
   if(live()){setView({step,page:parseSavedStockLossPage(data,{workId,runId,stepId:step.id})});setNotice('');}
  }catch{
   if(live())reset('目前无法核实这一步的报损单页。没有重新查询；未核实不等于本页没有单据。');
  }finally{if(job.current===request){job.current=null;setWorking(null);}}
 }
 return <article className="ar-card" ref={panel} aria-label="已保存的报损单">
  <h3>已保存的报损单</h3>
  <p>只核对点选步骤保存的一页，不会补查其他页。来源状态码不代表过账或库存变动。</p>
  <div role="group" aria-label="原报损步骤">
   {steps.map(step=><button className="ar-read" type="button" disabled={!ready||working?.id===step.id}
    key={step.id} onClick={()=>void show(step)}>查看第 {step.sequence} 步报损单页</button>)}
   {working&&<button className="ar-read" type="button" onClick={()=>reset('已取消查看。没有重新查询。')}>取消查看</button>}
  </div>
  {notice&&<p role="status">{notice}</p>}
  {shown&&<section aria-label="报损单原页">
   <h4>第 {shown.step.sequence} 步保存的一页</h4>
   <p>原保存时间（UTC）：<time dateTime={shown.page.recorded_at}>{shown.page.recorded_at}</time>。</p>
   <p>原请求跳过 {shown.page.offset} 条，最多取 {shown.page.requested_count} 条；本页保存了 {shown.page.returned_count} 条。</p>
   {shown.page.items.length===0
    ?<p>已核对：这一页返回 0 条。不能据此判断原日期范围没有报损单。</p>
    :<div className="ar-operational-scroll" tabIndex={0} aria-label="报损单原字段列表"><table aria-label="原编号、日期与来源状态码">
      <thead><tr><th scope="col">原单号</th><th scope="col">原单据日期</th><th scope="col">来源状态码</th></tr></thead>
      <tbody>{shown.page.items.map((row,index)=><tr key={index}>
       <th scope="row"><bdi>{row.bill_code}</bdi></th><td><bdi>{row.bill_date}</bdi></td><td>{row.source_bill_state}</td>
      </tr>)}</tbody>
     </table></div>}
  </section>}
 </article>;
}
