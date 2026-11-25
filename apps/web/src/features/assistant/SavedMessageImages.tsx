import {useEffect,useId,useRef,useState} from 'react';
import {authorizedRequest} from '../../lib/auth/session';
import {loadImagePreview,parseImageAsset} from './imageAssets';
import type {ImageReference} from './assistantInput';
import './saved-message-images.css';

type ReadableImage=Extract<ImageReference,{readable:true}>;
type Load={state:'loading'|'failed'}|{state:'ready';url:string};

function AuthorizedImage({image,baseUrl}:{image:ReadableImage;baseUrl:string}){
 const [attempt,retry]=useState(0),[load,setLoad]=useState<Load>({state:'loading'}),[zoom,setZoom]=useState(false);
 const opener=useRef<HTMLButtonElement>(null),dialog=useRef<HTMLDialogElement>(null),retryPending=useRef(false);
 const dialogId=useId();
 const {asset_id:id,sha256,byte_size,content_type,original_name:name}=image;
 useEffect(()=>{
  const abort=new AbortController();let disposed=false,release:(()=>void)|undefined;
  void (async()=>{
   const asset=parseImageAsset(await authorizedRequest<unknown>('/api/v1/media/assets/'+id,{baseUrl,signal:abort.signal}));
   if(asset.id!==id||asset.sha256!==sha256||asset.byte_size!==byte_size||asset.content_type!==content_type)throw Error('图片记录已改变。');
   const result=await loadImagePreview(asset,{baseUrl,signal:abort.signal});release=result.dispose;
   if(disposed){result.dispose();return;}
   retryPending.current=false;setLoad({state:'ready',url:result.url});
  })().catch(()=>{if(!disposed){retryPending.current=false;setLoad({state:'failed'});}});
  return()=>{disposed=true;abort.abort();release?.();};
 },[id,sha256,byte_size,content_type,baseUrl,attempt]);

 function open(){
  const panel=dialog.current,target=opener.current;
  if(!panel||!target||panel.open)return;
  // Register the button that the user just activated as the native modal opener.
  target.focus({preventScroll:true});
  setZoom(true);panel.showModal();
 }
 function close(){setZoom(false);}
 function fail(){
  dialog.current?.close();setLoad({state:'failed'});
 }
 function act(){
  if(load.state==='ready'){open();return;}
  if(load.state!=='failed'||retryPending.current)return;
  retryPending.current=true;setLoad({state:'loading'});retry(n=>n+1);
 }
 const ready=load.state==='ready',waiting=load.state==='loading',showAction=!waiting||attempt>0;
 return <figure className="assistant-saved-image">
  {load.state==='failed'?<p className="assistant-image-read-error" role="status">图片未能读取，未重新执行工作</p>:
   waiting?<span role="status">正在读取图片…</span>:null}
  {showAction&&<button ref={opener} className={ready?'assistant-image-open':'assistant-image-retry'} type="button"
   aria-label={(ready?'查看大图：':'重新读取图片：')+name}
   aria-haspopup={ready?'dialog':undefined} aria-controls={ready?dialogId:undefined}
   aria-disabled={waiting||undefined} onClick={act}>
   {load.state==='ready'?<><img src={load.url} alt={name} onError={fail}/><span>查看大图</span></>:'重新读取图片'}
  </button>}
  {load.state==='ready'&&<dialog id={dialogId} ref={dialog} className="assistant-image-dialog" aria-label={'图片预览：'+name} onClose={close}>
   <button className="assistant-image-close" type="button" onClick={()=>dialog.current?.close()}>关闭图片预览</button>
   {zoom&&<img src={load.url} alt={name} onError={fail}/>}
  </dialog>}
  <figcaption>{name}</figcaption>
 </figure>;
}
export function MessageImages({images,canRead,baseUrl}:{images:ImageReference[];canRead:boolean;baseUrl:string}){
 if(!images.length)return null;
 return <div className="assistant-message-images" aria-label="这条消息的图片">{images.map(image=>
  canRead&&image.readable
   ? <AuthorizedImage key={JSON.stringify([baseUrl,image.asset_id,image.sha256,image.byte_size,image.content_type,image.original_name])}
      image={image} baseUrl={baseUrl}/>
   : <span key={image.asset_id} className="assistant-image-unavailable">图片当前不可访问</span>
 )}</div>;
}
