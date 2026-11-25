import {useEffect,useId,useLayoutEffect,useRef,useState} from 'react';
import {usePath} from '../../app/router';
import {ApiError} from '../../lib/api/client';
import {ProductBarcodeRows} from './ProductBarcodePageCard';
import {parseProductBarcodeRemainder,type ProductBarcodePage} from './productBarcodePage';
import type {ProductRemainderReading,ProductRemainderProgress} from './productBarcodeRemainderMemory';
import {browserOffline} from './readbackConnectivity';

export type ReadProductBarcodeRemainder=(origin:string,after:string|null,signal:AbortSignal)=>Promise<{data:unknown;archiveProof:string|null}>;
const maxBatchesPerClick=6;

/** Read only when selected by the person. Data never becomes part of the original Run. */
export function ProductBarcodeRemainderReader({anchor,workId,runId,read,initialReading,interrupted=false,remember,onReadingChange}:{
 anchor:ProductBarcodePage;workId:string;runId:string;read:ReadProductBarcodeRemainder;
 initialReading?:ProductRemainderReading|null;interrupted?:boolean;remember?:(reading:ProductRemainderReading|null)=>void;
 onReadingChange?:(reading:boolean)=>void;
}){
 const title=useId(),path=usePath(),section=useRef<HTMLElement>(null);
 const binding=JSON.stringify([path,workId,runId,anchor.block_id,anchor.verified_through]),initialBinding=useRef(binding);
 const inFlight=useRef<AbortController|null>(null),stored=useRef<ProductRemainderProgress|null>(initialReading?.progress??null);
 const selectedPage=useRef(initialReading?.pageIndex??0);
 const [progress,setProgress]=useState<ProductRemainderProgress|null>(initialReading?.progress??null),
  [pageIndex,setPageIndex]=useState(selectedPage.current),[reading,setReading]=useState(false),
  [notice,setNotice]=useState(interrupted
   ? (initialReading?'定期核对查看权限时已暂停。已恢复已读页，可从未完成处继续。':'定期核对查看权限时已暂停，未收到可恢复的补核页。可重新核对剩余记录。')
   : (initialReading?'查看权限仍有效，已恢复上次阅读。':''));
 function selectPage(index:number){
  selectedPage.current=index;setPageIndex(index);
  if(stored.current)remember?.({progress:stored.current,pageIndex:index});
 }
 function clearReading(){
  stored.current=null;setProgress(null);selectPage(0);remember?.(null);
 }
 const currentView=()=>document.visibilityState!=='hidden'&&!section.current?.closest('[hidden]')&&!!section.current&&!browserOffline();
 function abort(retain:boolean,message:string){
  const job=inFlight.current;inFlight.current=null;job?.abort();
  if(!retain)clearReading();
  setReading(false);onReadingChange?.(false);setNotice(message);
 }
 // On an app destination / selection change, data from this optional reader is no longer current.
 useLayoutEffect(()=>{
  if(initialBinding.current!==binding){initialBinding.current=binding;abort(false,'');}
  return ()=>{const job=inFlight.current;inFlight.current=null;if(job){job.abort();onReadingChange?.(false);}};
 },[binding]);
 useEffect(()=>{
  function hidden(){if(document.visibilityState==='hidden'&&inFlight.current)abort(true,'已暂停。回到页面后可手动继续核对。');}
  function offline(){if(inFlight.current)abort(true,'连接已断开，已暂停。恢复连接后可手动继续。');}
  document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',offline);
  return ()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',offline);};
 },[]);
 async function start(){
  if(inFlight.current||stored.current?.cursor===null||!currentView())return;
  const job=new AbortController();inFlight.current=job;setReading(true);onReadingChange?.(true);setNotice('正在从同一已存原件核对剩余记录…');
  let current=stored.current;
  const live=()=>!job.signal.aborted&&inFlight.current===job&&currentView();
  try{
   for(let i=0;i<maxBatchesPerClick;i++){
    if(!live())return;
    const cursor=current?.cursor??null;
    const reply=await read(anchor.origin_block_id,cursor,job.signal);
    if(!live())return;
    const batch=parseProductBarcodeRemainder(reply.data,{workId,runId,anchor,
     expectedOffset:current?.through??anchor.verified_through,recordedAt:current?.recordedAt});
    if(batch.nextCursor!==null&&batch.nextCursor===cursor)throw Error('剩余范围未前进。');
    if(current?.archiveProof&&reply.archiveProof!==null&&current.archiveProof!==reply.archiveProof)throw Error('原件证明已变化。');
    const archiveProof=!current||current.archiveProof===reply.archiveProof?reply.archiveProof:null;
    current={pages:[...(current?.pages??[]),...batch.pages],through:batch.verifiedThrough,cursor:batch.nextCursor,recordedAt:batch.recordedAt,archiveProof};
    stored.current=current;setProgress(current);remember?.({progress:current,pageIndex:selectedPage.current});
    if(current.cursor===null){setNotice('已补核到这份已存原件的末尾。原工作状态没有改变。');return;}
   }
   if(live())setNotice('本次已到读取批次上限，仍有记录未核对。可手动继续。');
  }catch(error){
   if(live()){
    // A partial response is not an authorization cache. Clear these optional
    // rows on denial, a changed anchor, malformed data or unavailable service.
    clearReading();
    setNotice(error instanceof ApiError&&error.status===404?'当前服务暂不能核对剩余记录。原工作已保存的页不变。':
     '目前无法确认这份已存原件的剩余范围，已隐藏本次补核的数据。');
   }
  }finally{
   if(inFlight.current===job){inFlight.current=null;setReading(false);onReadingChange?.(false);}
  }
 }
 const page=progress?.pages[pageIndex];
 return <section ref={section} className="ar-product-barcode ar-panel" aria-labelledby={title}>
  <h4 id={title}>从同一已存原件补核</h4>
  <p>只读已存原件，不查询当前管家婆数据，不会重跑或发送消息。要核对最新数据，请另外发起查询。</p>
  {reading?<button type="button" className="ar-read" onClick={()=>abort(true,'已暂停。已显示的补核页仍可读；剩余记录没有继续读取。')}>取消剩余核对</button>:
   (!progress||progress.cursor!==null)&&<button type="button" className="ar-read" onClick={()=>void start()}>
    {progress?'继续核对已存原件的剩余':'核对已存原件的剩余'}
   </button>}
  {notice&&<p role="status">{notice}</p>}
  {progress&&<>
   <p>原件回执保存时间：<time dateTime={progress.recordedAt}>{progress.recordedAt}</time>。这些不是实时数据。</p>
   <p>到第 {progress.through} 条，共 {anchor.total_records} 条记录。{progress.cursor===null?'已存原件已核到末尾。':'还有记录尚未核对。'}</p>
   {page&&<>
    <p>补核页：第 {page.offset+1} 至 {page.offset+page.returned_count} 条记录，不计入原工作已保存页。</p>
    <ProductBarcodeRows key={page.offset} sourceRows={page.rows}/>
    {progress.pages.length>1&&<div className="ar-product-barcode-nav">
     <button type="button" className="ar-read" disabled={pageIndex===0} onClick={()=>selectPage(Math.max(0,selectedPage.current-1))}>上一页补核记录</button>
     <span>补核页 {pageIndex+1} / {progress.pages.length}</span>
     <button type="button" className="ar-read" disabled={pageIndex+1>=progress.pages.length} onClick={()=>selectPage(Math.min(progress.pages.length-1,selectedPage.current+1))}>下一页补核记录</button>
    </div>}
   </>}
  </>}
 </section>;
}
