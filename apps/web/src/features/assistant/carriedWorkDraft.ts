import {readDraftCheckpoint,saveDraftCheckpoint} from '../../lib/auth/assistantDraftCheckpoint';

type Scope=Parameters<typeof saveDraftCheckpoint>[0];
type StorageLike=NonNullable<Parameters<typeof saveDraftCheckpoint>[4]>;

/** Bring visible unsent text to a new draft without replacing its original paragraphs. */
export function mergeCarriedWorkDraft(memory:string,checkpoint:string|null,incoming:string,limit:number):string|null{
 const current=memory||checkpoint||'';
 const wrapped=(s:string)=>'\n\n'+s+'\n\n';
 const combined=current===incoming||wrapped(current).includes(wrapped(incoming))?current:
  !current?incoming:current+(current.endsWith('\n\n')?'':current.endsWith('\n')?'\n':'\n\n')+incoming;
 return combined.length<=limit?combined:null;
}

/** Preserve every still-recoverable original draft, including when all eight slots are in use. */
export function saveCarriedWorkDraft(scope:Scope,version:string,merged:string,storage:StorageLike):
 'saved'|'storage_unavailable'|'would_evict'{
 let wouldEvict=false;
 const saved=saveDraftCheckpoint(scope,version,'new',merged,{
  getItem:key=>storage.getItem(key),
  setItem:(key,value)=>{
   const prior=JSON.parse(storage.getItem(key)??'null'), next=JSON.parse(value);
   if(Array.isArray(prior?.drafts)&&Array.isArray(next?.drafts)){
    const updated=Date.now();
    // Reuse the normal scope, expiry and text checks; never resurrect expired data.
    for(const entry of prior.drafts.slice(0,8)){
     const work=entry?.work;
     if(typeof work!=='string'||work==='new')continue;
     const original=readDraftCheckpoint(scope,version,work,storage,updated);
     if(original!==null&&!next.drafts.some((candidate:{work:string;text:string})=>candidate.work===work&&candidate.text===original)){
      wouldEvict=true;
      throw Error('A prior draft would be removed.');
     }
    }
   }
   storage.setItem(key,value);
  },
  // During a nonempty merge this happens only on failed storage or invalid input.
  removeItem:()=>undefined,
 });
 return saved?'saved':wouldEvict?'would_evict':'storage_unavailable';
}
