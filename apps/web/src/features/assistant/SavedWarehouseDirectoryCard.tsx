import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {usePath} from '../../app/router';
import {parseSavedWarehouseDirectoryPage,type SavedWarehouseDirectoryPage} from './savedWarehouseDirectory';
import type {SavedWarehouseDirectoryStep} from './workProgress';

export function SavedWarehouseDirectoryCard({workId,runId,ready,steps,read}:{workId:string;runId:string;ready:boolean;
 steps:SavedWarehouseDirectoryStep[];read:(stepId:string,signal:AbortSignal)=>Promise<unknown>}){
 const path=usePath(),panel=useRef<HTMLElement>(null),job=useRef<AbortController|null>(null);
 const [working,setWorking]=useState<SavedWarehouseDirectoryStep|null>(null),[notice,setNotice]=useState('');
 const [view,setView]=useState<{step:SavedWarehouseDirectoryStep;page:SavedWarehouseDirectoryPage}|null>(null);
 const allowed=(original:SavedWarehouseDirectoryStep)=>steps.some(step=>step.id===original.id&&step.sequence===original.sequence&&
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
 async function show(step:SavedWarehouseDirectoryStep){
  if(!current()||!allowed(step)||working?.id===step.id)return;
  reset('正在核对这一步保存的仓库目录页…');
  const request=new AbortController();job.current=request;setWorking(step);
  const live=()=>job.current===request&&!request.signal.aborted&&current();
  try{
   const data=await read(step.id,request.signal);
   if(live()){setView({step,page:parseSavedWarehouseDirectoryPage(data,{workId,runId,stepId:step.id})});setNotice('');}
  }catch{
   if(live())reset('目前无法核实这一步的仓库目录页。没有重新查询；未核实不等于本页没有仓库。');
  }finally{if(job.current===request){job.current=null;setWorking(null);}}
 }
 return <article className="ar-card" ref={panel} aria-label="已保存的仓库目录">
  <h3>已保存的仓库目录</h3>
  <p>只核对点选步骤保存的一页，不会补查其他页。不是实时目录，不包含库存。</p>
  <div role="group" aria-label="原仓库目录步骤">
   {steps.map(step=><button className="ar-read" type="button" disabled={!ready||working?.id===step.id}
    key={step.id} onClick={()=>void show(step)}>查看第 {step.sequence} 步仓库目录页</button>)}
   {working&&<button className="ar-read" type="button" onClick={()=>reset('已取消查看。没有重新查询。')}>取消查看</button>}
  </div>
  {notice&&<p role="status">{notice}</p>}
  {shown&&<section aria-label="仓库目录原页">
   <h4>第 {shown.step.sequence} 步保存的一页</h4>
   <p>原保存时间（UTC）：<time dateTime={shown.page.recorded_at}>{shown.page.recorded_at}</time>。</p>
   <p>原请求跳过 {shown.page.offset} 条，最多取 {shown.page.requested_count} 条；本页保存了 {shown.page.returned_count} 条。</p>
   {shown.page.items.length===0
    ?<p>已核对：这一页返回 0 条。不能据此判断本店没有仓库。</p>
    :<div className="ar-operational-scroll" tabIndex={0} aria-label="仓库目录原字段列表"><table aria-label="原仓库代码、名称与启用状态">
      <thead><tr><th scope="col">原仓库代码</th><th scope="col">原仓库名称</th><th scope="col">来源启用状态</th></tr></thead>
      <tbody>{shown.page.items.map((row,index)=><tr key={index}>
       <th scope="row">{row.code===''?'来源代码为空':<bdi style={{whiteSpace:'pre-wrap'}}>{row.code}</bdi>}</th>
       <td><bdi style={{whiteSpace:'pre-wrap'}}>{row.name}</bdi></td>
       <td>{row.source_is_active===true?'启用':row.source_is_active===false?'未启用':'来源未提供'}</td>
      </tr>)}</tbody>
     </table></div>}
  </section>}
 </article>;
}
