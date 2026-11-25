import {readDraftCheckpoint,saveDraftCheckpoint} from '../../lib/auth/assistantDraftCheckpoint';
type Scope=Parameters<typeof saveDraftCheckpoint>[0];
type StorageLike=NonNullable<Parameters<typeof saveDraftCheckpoint>[4]>;

/**
 * Never checkpoint the delivered message as a newly composed draft. An exact,
 * unchanged prefix followed by a new line can be cleanly separated. Ambiguous
 * mixes stay in the editor instead of reconstructing or deleting user text.
 */
export function confirmedFollowupDraft(sent:string,editable:string):
 {text:string;removeDeliveredPrefix:boolean}|null {
 const delivered=sent.trim(),current=editable.trim();
 if(!delivered||!current||delivered===current)return null;
 for(const original of new Set([sent,delivered])){
  if(editable.startsWith(original+'\n')){
   const remainder=editable.slice(original.length).replace(/^\n\n?/u,'');
   return remainder.trim()&&!remainder.includes(delivered)?{text:remainder,removeDeliveredPrefix:true}:null;
  }
 }
 return current.includes(delivered)?null:{text:editable,removeDeliveredPrefix:false};
}

/** The vacated slot can be reused only if no separately saved draft is discarded. */
export function saveConfirmedFollowupDraft(scope:Scope,version:string,work:string,text:string,tab:StorageLike):boolean {
 if(readDraftCheckpoint(scope,version,work,tab)!==null)return false;
 return saveDraftCheckpoint(scope,version,work,text,{
  getItem:k=>tab.getItem(k),
  setItem:(key,value)=>{
   const prior=JSON.parse(tab.getItem(key)??'null'), next=JSON.parse(value);
   if(Array.isArray(prior?.drafts)&&Array.isArray(next?.drafts)){
    const now=Date.now();
    for(const entry of prior.drafts.slice(0,8)){
     if(typeof entry?.work!=='string')continue;
     const current=readDraftCheckpoint(scope,version,entry.work,tab,now);
     if(current!==null&&!next.drafts.some((draft:{work:string;text:string})=>draft.work===entry.work&&draft.text===current))
      throw Error('A previously saved draft would be discarded.');
    }
   }
   tab.setItem(key,value);
  },
  // A quota or collision must not delete the existing checkpoint record.
  removeItem:()=>undefined,
 });
}
