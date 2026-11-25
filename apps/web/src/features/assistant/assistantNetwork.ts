import {useSyncExternalStore} from 'react';

// This is only the browser's explicit signal; true onLine says nothing about
// Host readiness, permissions, or whether an earlier request was accepted.
export const browserReportsOffline=()=>globalThis.navigator?.onLine===false;
export const offlineBeforeSendNotice='浏览器报告离线，本条没有发送。请联网后手动发送。';

function subscribeBrowserConnectivity(listener:()=>void){
  if(typeof window==='undefined')return ()=>{};
  const browser=window;
  browser.addEventListener('online',listener);
  browser.addEventListener('offline',listener);
  return ()=>{
    browser.removeEventListener('online',listener);
    browser.removeEventListener('offline',listener);
  };
}
export function useBrowserOffline(){
  return useSyncExternalStore(subscribeBrowserConnectivity,browserReportsOffline,()=>false);
}
