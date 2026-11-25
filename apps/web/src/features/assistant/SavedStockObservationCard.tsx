import {useEffect,useLayoutEffect,useRef,useState} from 'react';
import {usePath} from '../../app/router';
import {parseSavedStockObservation,type SavedStockObservation} from './savedStockObservation';
import type {SavedStockObservationStep} from './workProgress';

export function SavedStockObservationCard({workId,runId,ready,steps,read}:{workId:string;runId:string;ready:boolean;
 steps:SavedStockObservationStep[];read:(stepId:string,signal:AbortSignal)=>Promise<unknown>}){
 const path=usePath(),panel=useRef<HTMLElement>(null),job=useRef<AbortController|null>(null);
 const [working,setWorking]=useState<SavedStockObservationStep|null>(null),[notice,setNotice]=useState('');
 const [view,setView]=useState<{step:SavedStockObservationStep;observation:SavedStockObservation}|null>(null);
 const allowed=(original:SavedStockObservationStep)=>steps.some(step=>step.id===original.id&&step.version===original.version&&
  step.sequence===original.sequence&&step.outputDigest===original.outputDigest);
 const current=()=>ready&&document.visibilityState!=='hidden'&&navigator.onLine!==false&&
  !!panel.current&&!panel.current.closest('[hidden]');
 function stop(){const active=job.current;job.current=null;active?.abort();setWorking(null);}
 function hide(message=''){stop();setView(null);setNotice(message);}
 useLayoutEffect(()=>{
  hide();
  return ()=>{const active=job.current;job.current=null;active?.abort();};
 },[workId,runId,path]);
 const shown=ready&&view&&allowed(view.step)?view:null;
 const authWitness=steps.map(step=>[step.id,step.version,step.sequence,step.outputDigest].join(':')).join(',');
 useLayoutEffect(()=>{
  if((!ready&&(working||view))||(working&&!allowed(working))||(view&&!allowed(view.step)))
   hide('为核对查看权限，暂不展示上次记录。可手动再查看。');
 },[ready,authWitness]);
 useEffect(()=>{
  function hidden(){if(document.visibilityState==='hidden')hide();}
  function offline(){hide();}
  document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',offline);
  return ()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',offline);};
 },[]);
 async function show(step:SavedStockObservationStep){
  if(!current()||!allowed(step)||working?.id===step.id)return;
  hide('正在核对这一步已保存的库存原字段…');
  const request=new AbortController();job.current=request;setWorking(step);
  const live=()=>job.current===request&&!request.signal.aborted&&current();
  try{
   const data=await read(step.id,request.signal);
   if(live()){setView({step,observation:parseSavedStockObservation(data,{workId,runId,stepId:step.id})});setNotice('');}
  }catch{
   if(live())hide('目前无法核实这一步已保存的记录，不能据此判断库存。没有重新查询。');
  }finally{if(job.current===request){job.current=null;setWorking(null);}}
 }
 return <article className="ar-card" ref={panel} aria-label="已保存的单仓库存观察">
  <h3>已保存的单仓库存观察</h3>
  <p>只看原步骤保存的字段。不是实时库存，不代表可售数量或所有仓库；不换算或合并。</p>
  <div role="group" aria-label="原库存步骤">
   {steps.map(step=><button className="ar-read" type="button" disabled={!ready||working?.id===step.id}
    key={step.id} onClick={()=>void show(step)}>查看第 {step.sequence} 步库存原字段</button>)}
   {working&&<button className="ar-read" type="button" onClick={()=>hide('已取消查看。没有重新查询。')}>取消查看</button>}
  </div>
  {notice&&<p role="status">{notice}</p>}
  {shown&&<section aria-label="单仓库存原字段">
   <h4>第 {shown.step.sequence} 步 · <bdi>{shown.observation.product.name}</bdi>（<bdi>{shown.observation.product.code}</bdi>）</h4>
   <p>{shown.observation.warehouse_scope==='matched_warehouse'
    ?'回包与原请求的仓库吻合；原记录未保存仓名。'
    :'只能核实原请求指向一仓；回包未能确认该仓数量。原记录未保存仓名，未查实不等于零。'}</p>
   <p>原观察开始（UTC）：<time dateTime={shown.observation.observed_started_at}>{shown.observation.observed_started_at}</time>。</p>
   <p>原观察结束（UTC）：<time dateTime={shown.observation.observed_finished_at}>{shown.observation.observed_finished_at}</time>。</p>
   <p>来源更新时间：未知。观察时刻不是来源更新时间。</p>
   {shown.observation.stock_fields.length>0&&<dl className="ar-operational-detail-fields">
    {shown.observation.stock_fields.map(item=><div key={item.field}>
     <dt>{item.field==='qty'?'主数量字段（qty）':'辅助数量字段（subUnitQty）'}</dt>
     <dd>{item.presence==='value'?<bdi>{item.raw_value}</bdi>:
      item.presence==='null'?'来源明确返回空值，不能按零计。':'来源未返回该字段，不能按零计。'}</dd>
     <dd>单位：{item.unit===null?'来源未提供':<bdi>{item.unit}</bdi>}</dd>
    </div>)}
   </dl>}
  </section>}
 </article>;
}
