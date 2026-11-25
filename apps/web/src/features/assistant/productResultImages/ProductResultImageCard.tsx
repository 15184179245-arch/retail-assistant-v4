import {useLayoutEffect,useRef,useState} from 'react';
import type {ProductImageBinding} from './contract';
import {loadProductImagePreview,type ProductResultImageRequest} from './content';
import '../saved-message-images.css';
import './product-result-image.css';

type Current={controller:AbortController;loading:boolean;dispose?:()=>void};
type View={key:string;phase:'loading'|'unavailable'|'ready';url?:string;retrying?:boolean};

/** Only an authorized Work-result renderer can supply a dedicated transport. No route is assumed here. */
export function ProductResultImageCard({binding,request,stillAuthorized}:{
 binding:ProductImageBinding;request?:ProductResultImageRequest;stillAuthorized:()=>boolean;
}){
 if(!request)return null;
 const {image,workId,runId}=binding;
 const key=JSON.stringify([workId,runId,image.block_id,image.sequence_no,image.image_ref,image.sha256,
  image.content_type,image.byte_size,image.width,image.height]);
 return <ProductImageViewer key={key} identity={key} binding={binding} request={request} stillAuthorized={stillAuthorized}/>;
}

function ProductImageViewer({identity,binding,request,stillAuthorized}:{
 identity:string;binding:ProductImageBinding;request:ProductResultImageRequest;stillAuthorized:()=>boolean;
}){
 const dialog=useRef<HTMLDialogElement>(null),trigger=useRef<HTMLButtonElement>(null);
 const closeButton=useRef<HTMLButtonElement>(null),retryButton=useRef<HTMLButtonElement>(null);
 const current=useRef<Current|null>(null),[view,setView]=useState<View|null>(null);
 function release(){
  const previous=current.current;current.current=null;
  previous?.controller.abort();previous?.dispose?.();
 }
 useLayoutEffect(()=>()=>{
  release();
  if(dialog.current?.open)dialog.current.close();
 },[]);
 async function read(retrying=false){
  const panel=dialog.current;
  if(!panel||!stillAuthorized()||current.current?.loading)return;
  release();
  const requestRef:Current={controller:new AbortController(),loading:true};
  current.current=requestRef;
  setView({key:identity,phase:'loading',retrying});
  if(!panel.open)panel.showModal();
  try{
   const result=await loadProductImagePreview(binding,request,requestRef.controller.signal,stillAuthorized);
   if(current.current!==requestRef||!panel.open||!stillAuthorized()){result.dispose();return;}
   requestRef.dispose=result.dispose;
   if(retryButton.current&&document.activeElement===retryButton.current)closeButton.current?.focus({preventScroll:true});
   setView({key:identity,phase:'ready',url:result.url});
  }catch{
   if(current.current===requestRef&&!requestRef.controller.signal.aborted)setView({key:identity,phase:'unavailable'});
  }finally{requestRef.loading=false;}
 }
 function close(){
  release();setView(null);trigger.current?.focus();
 }
 const visible=view?.key===identity?view:null;
 const retrying=visible?.phase==='loading'&&visible.retrying;
 const showRetry=retrying||visible?.phase==='unavailable';
 return <article className="ar-card ar-product-image-result" aria-label="商品图片">
  <header><span className="ar-kicker">图片结果 · 管家婆</span><h3>商品图片</h3>
   <button ref={trigger} className="ar-read" type="button" onClick={()=>void read()}>查看商品图片</button>
  </header>
  <dialog ref={dialog} className="assistant-image-dialog" aria-label="商品图片预览" onClose={close}>
   <div className="ar-product-image-controls">
    <button ref={closeButton} className="assistant-image-close" type="button" onClick={()=>dialog.current?.close()}>关闭图片预览</button>
    {showRetry&&<button ref={retryButton} className="assistant-image-retry" type="button" aria-disabled={retrying||undefined} onClick={()=>void read(true)}>重新读取图片</button>}
   </div>
   <div className="ar-product-image-viewport">
    {visible?.phase==='ready'&&visible.url?<img src={visible.url} alt="商品图片" onError={()=>{
     release();setView({key:identity,phase:'unavailable'});
    }}/>:visible?.phase==='unavailable'?<p role="status">当前未能读取商品图片。重新读取不会重跑原事项。</p>
    :visible?.phase==='loading'?<p role="status">正在核对权限并读取商品图片…</p>:null}
   </div>
  </dialog>
 </article>;
}
