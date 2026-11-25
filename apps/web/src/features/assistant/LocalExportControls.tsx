import {useLayoutEffect,useRef,useState,useSyncExternalStore} from 'react';
import {loadAssistantExport,useAssistantRouteSignal} from './assistantRouteVisibility';
import {captureSessionScope} from '../../lib/auth/session';
import {getCapabilityEpoch,hasCapability,subscribeCapabilities} from '../../lib/auth/capabilities';
import './local-export-controls.css';

type Props={
 scopeKey:string|symbol;label:string;filename:string;available?:boolean;cancelReplyCopyOnHide?:boolean;
 readFresh:(signal:AbortSignal)=>Promise<string>;
};
const unavailable=()=>{throw Error('结果未重新核对。');};
const pausedReplyCopy='复制已中断。返回页面后请手动点击“复制全文与依据”。';

/** The click is the only trigger. No background copy, download or network transmission. */
export function LocalExportControls({scopeKey,label,filename,readFresh,available=true,cancelReplyCopyOnHide=false}:Props){
 const permit=useAssistantRouteSignal();
 const epoch=useSyncExternalStore(subscribeCapabilities,getCapabilityEpoch,getCapabilityEpoch);
 const inFlight=useRef<AbortController|null>(null);
 const stopVisibilityWatch=useRef<(()=>void)|null>(null);
 const [busy,setBusy]=useState(false),[notice,setNotice]=useState('');
 useLayoutEffect(()=>{
  setBusy(false);setNotice('');
  return()=>{
   stopVisibilityWatch.current?.();stopVisibilityWatch.current=null;
   inFlight.current?.abort();inFlight.current=null;
  };
 },[scopeKey]);

 async function act(mode:'copy'|'download'){
  if(permit?.aborted||!available||inFlight.current||document.visibilityState==='hidden')return;
  const signalOwner=new AbortController();inFlight.current=signalOwner;
  const scope=captureSessionScope();
  const matchesAction=()=>inFlight.current===signalOwner&&!signalOwner.signal.aborted&&scope()&&epoch===getCapabilityEpoch()&&
   hasCapability('agent.run')&&hasCapability('tasks.read_self');
  const valid=()=>matchesAction()&&!permit?.aborted&&document.visibilityState!=='hidden';
  if(!valid()){inFlight.current=null;return;}
  const watchReplyCopy=mode==='copy'&&cancelReplyCopyOnHide;
  let textHandedOff=false;
  let unsubscribeAuthority:(()=>void)|undefined;
  const stopWatching=()=>{
   document.removeEventListener('visibilitychange',cancelIfHidden);
   permit?.removeEventListener('abort',cancelIfHidden);
   unsubscribeAuthority?.();
  };
  function interrupt(notice?:string){
   if(textHandedOff||inFlight.current!==signalOwner)return;
   stopWatching();stopVisibilityWatch.current=null;
   inFlight.current=null;signalOwner.abort();setBusy(false);
   if(notice)setNotice(notice);
  }
  function cancelIfHidden(){
   if(!permit?.aborted&&document.visibilityState!=='hidden')return;
   interrupt(matchesAction()?(watchReplyCopy?pausedReplyCopy:'核对已中断，未执行本地操作。'):undefined);
  }
  stopVisibilityWatch.current=stopWatching;
  if(watchReplyCopy)document.addEventListener('visibilitychange',cancelIfHidden);
  permit?.addEventListener('abort',cancelIfHidden,{once:true});
  unsubscribeAuthority=subscribeCapabilities(()=>{
   if(epoch!==getCapabilityEpoch())interrupt(scope()?'权限已变化，未交付本地内容。请重新核对后手动操作。':undefined);
  });
  setBusy(true);setNotice('正在核对本次已保存内容…');
  const load=()=>loadAssistantExport(readFresh,signalOwner.signal,valid);
  try{
   if(mode==='copy'){
    if(typeof ClipboardItem==='function'&&navigator.clipboard?.write){
     // Safari and Chromium can require activation before a promise-backed item settles.
     const item=load().then(text=>{
      // The promise continuation has its own microtask. Recheck at the
      // native handoff, not only when the previous authorization resolved.
      if(!valid())return unavailable();
      const blob=new Blob([text],{type:'text/plain'});
      textHandedOff=true;
      return blob;
     });
     void item.catch(()=>{}); // A denied write may never consume the item promise.
     await navigator.clipboard.write([new ClipboardItem({'text/plain':item})]);
    }else if(navigator.clipboard?.writeText){
     const text=await load();if(!valid())return unavailable();
     textHandedOff=true;
     await navigator.clipboard.writeText(text);
    }else throw Error('此浏览器不允许复制。');
    // If the browser already took verified text while visible, honor its
    // actual completion even if the tab was hidden before it replied.
    if(valid()||(watchReplyCopy&&matchesAction()))setNotice('已复制'+label+'。');
   }else{
    const text=await load();if(!valid())return unavailable();
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));
    const link=document.createElement('a');link.href=url;link.download=filename;
    try{
     document.body.append(link);
     if(!valid())return unavailable();
     textHandedOff=true;
     link.click();
    }finally{
     link.remove();window.setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    if(valid())setNotice('已请求下载'+label+'。');
   }
  }catch{
   if(valid())setNotice(mode==='copy'?'未复制。请核对当前结果或改用下载文本。':'未下载。请重新核对当前结果后再试。');
   else if(watchReplyCopy&&matchesAction())setNotice(pausedReplyCopy);
  }finally{
   if(stopVisibilityWatch.current===stopWatching){stopWatching();stopVisibilityWatch.current=null;}
   if(inFlight.current===signalOwner){signalOwner.abort();inFlight.current=null;setBusy(false);}
  }
 }
 return <div role="group" aria-label={label+'的本地操作'} className="assistant-export-actions">
  <button type="button" className="ar-read" aria-disabled={busy||!available||undefined} onClick={()=>void act('copy')}>复制{label}</button>{' '}
  <button type="button" className="ar-read" aria-disabled={busy||!available||undefined} onClick={()=>void act('download')}>下载{label}（TXT）</button>
  <p role="status" aria-atomic="true" className={notice?undefined:'assistant-sr-only'}>{notice}</p>
 </div>;
}
