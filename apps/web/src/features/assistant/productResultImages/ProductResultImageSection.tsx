import {useEffect,useState} from 'react';
import {captureSessionScope,getSession,sessionIsExpired} from '../../../lib/auth/session';
import {ApiError} from '../../../lib/api/client';
import {getCapabilityStatus,getCapabilityVersion,hasCapability} from '../../../lib/auth/capabilities';
import {readProductImageContent,readProductImageManifest,sameOriginProductImages} from './api';
import type {ProductImageBinding,ProductImageTarget} from './contract';
import {ProductResultImageCard} from './ProductResultImageCard';

type Display={bindingKey:string;items:ProductImageBinding[];current:()=>boolean;
 phase:'ready'|'checking'|'retry';retry?:()=>void};
function canDiscover():boolean {
 return getCapabilityStatus()==='ready'&&['agent.run','tasks.read_self','catalog.read','guanjia.catalog.read','guanjia.company.read'].every(hasCapability);
}

/** Optional on verified product-image citations. Existing result reports do not depend on this endpoint. */
export function ProductResultImageSection({workId,runId,executionStatus,baseUrl,evidenceKey}:ProductImageTarget&{
 baseUrl:string;evidenceKey?:string;
}) {
 const [display,setDisplay]=useState<Display|null>(null);
 const bindingKey=JSON.stringify([workId,runId,executionStatus,baseUrl,evidenceKey]);
 useEffect(()=>{
  if(!evidenceKey||executionStatus!=='completed'||!sameOriginProductImages(baseUrl)||!canDiscover())return;
  const sameSession=captureSessionScope(),session=getSession(),version=getCapabilityVersion();
  let active=true,network:AbortController|null=null;
  const current=()=>active&&!!session&&!sessionIsExpired(session)&&sameSession()&&
   version===getCapabilityVersion()&&canDiscover()&&document.visibilityState!=='hidden';
  function retry(){if(!network&&current())verify(true);}
  function verify(recovering=false){
   network?.abort();network=null;
   if(!current()){setDisplay(null);return;}
   setDisplay(recovering?{bindingKey,phase:'checking',items:[],current,retry}:null);
   const request=new AbortController();network=request;
   const owned=()=>current()&&network===request&&!request.signal.aborted;
   void readProductImageManifest({workId,runId,executionStatus},request.signal).then(items=>{
    if(owned())setDisplay(items.length?{bindingKey,phase:'ready',items,current,retry:recovering?retry:undefined}:null);
   }).catch(error=>{
    if(owned())setDisplay(error instanceof ApiError&&error.status===503?{bindingKey,phase:'retry',items:[],current,retry}:null);
   }).finally(()=>{if(network===request)network=null;});
  }
  const visibility=()=>verify();
  verify();document.addEventListener('visibilitychange',visibility);
  return()=>{active=false;network?.abort();document.removeEventListener('visibilitychange',visibility);};
 },[bindingKey,baseUrl,evidenceKey,executionStatus,runId,workId]);
 if(!evidenceKey||display?.bindingKey!==bindingKey||!display.current())return null;
 return <section className="ar-results" aria-label={display.phase==='ready'?'已授权的商品图片':'商品图片核对'}>
  {display.retry&&<div className="assistant-notice">
   <p role="status">{display.phase==='retry'?'暂时未能核对商品图片。重新核对此来源不会重跑原事项。':
    display.phase==='checking'?'正在重新核对商品图片…':'已重新核对，可查看商品图片。'}</p>
   <button className="ar-read" type="button" aria-disabled={display.phase==='checking'||undefined}
    onClick={display.retry}>重新核对商品图片</button>
  </div>}
  {display.items.map(binding=>
   <ProductResultImageCard key={binding.image.image_ref} binding={binding} request={readProductImageContent} stillAuthorized={display.current}/>
  )}
 </section>;
}
