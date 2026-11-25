export type AssistantReadingAnchor={item:WeakRef<HTMLElement>;offset:number};

function readingViewport(scroller:HTMLElement,stream:HTMLElement,topbar:HTMLElement|null){
 const outer=scroller.getBoundingClientRect(), body=stream.getBoundingClientRect();
 const left=Math.max(0,outer.left,body.left),right=Math.min(innerWidth,outer.right,body.right);
 const top=Math.max(0,outer.top,body.top,topbar?.getBoundingClientRect().bottom??0),bottom=Math.min(innerHeight,outer.bottom,body.bottom);
 return right>left&&bottom>top?{top,bottom,left,right}:null;
}
const readingItem='h2,h3,h4,p,li,dt,dd,td,th';

/** Track just a weak reference to an actual visible reading item, not a row value or result copy. */
export function captureAssistantReadingAnchor(scroller:HTMLElement,stream:HTMLElement,topbar:HTMLElement|null):AssistantReadingAnchor|null{
 if(typeof WeakRef!=='function')return null;
 const view=readingViewport(scroller,stream,topbar);
 if(!view)return null;
 const x=(view.left+view.right)/2,height=view.bottom-view.top;
 for(const dy of [Math.min(32,height/4),height/2,height*3/4]){
  const point=document.elementFromPoint(x,view.top+dy);
  const item=point?.closest<HTMLElement>(readingItem);
  if(!item||!stream.contains(item)||!item.closest('.ar-results'))continue;
  return {item:new WeakRef(item),offset:item.getBoundingClientRect().top-view.top};
 }
 return null;
}

export function clampAssistantReadingOffset(offset:number,itemHeight:number,viewportHeight:number):number{
 return Math.min(Math.max(0,viewportHeight-Math.min(32,itemHeight)),Math.max(-Math.max(0,itemHeight-16),offset));
}

/** Offset the new scroll container only; never move focus or run an input action. */
export function restoreAssistantReadingAnchor(anchor:AssistantReadingAnchor|null,scroller:HTMLElement,stream:HTMLElement,topbar:HTMLElement|null):boolean{
 const item=anchor?.item.deref();
 if(!anchor||!item?.isConnected||!stream.contains(item)||!item.closest('.ar-results'))return false;
 const view=readingViewport(scroller,stream,topbar);
 if(!view)return false;
 const box=item.getBoundingClientRect();
 scroller.scrollTop+=box.top-view.top-clampAssistantReadingOffset(anchor.offset,box.height,view.bottom-view.top);
 return true;
}
