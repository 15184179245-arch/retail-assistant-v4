import type {ImageAsset} from "./imageAssets";
export interface DraftImage {id:string;name:string;bytes:number;url:string;state:'uploading'|'ready'|'error';asset?:ImageAsset;error?:string}
export type DraftImageDelivery='sending'|'unconfirmed';
export function DraftImages({images,remove,disabled,delivery}:{images:DraftImage[];remove:(id:string)=>void;disabled:boolean;delivery?:DraftImageDelivery}){
 if(!images.length)return null;
 return <div className="assistant-draft-images">{images.map(image=>{
  const status=delivery==='sending'?'消息发送中，尚未确认送达':delivery==='unconfirmed'?'消息送达待核对':
    image.state==='uploading'?'正在上传，尚未发送':image.state==='ready'?'已上传，等待发送':'上传未确认，未附到消息';
  return <figure key={image.id}>
   <button type="button" disabled={disabled} onClick={()=>remove(image.id)} aria-label={'从消息移除图片 '+image.name}>移除</button>
   <img src={image.url} alt={'附件预览：'+image.name}/>
   <figcaption><span>{image.name}</span><small>{status}</small>{image.error&&<span role="alert">{image.error}</span>}</figcaption>
  </figure>;
 })}</div>;
}
export {MessageImages} from "./SavedMessageImages";
