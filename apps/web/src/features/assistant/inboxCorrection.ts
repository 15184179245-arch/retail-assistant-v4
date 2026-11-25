import type {Work} from './assistantApi';
export interface CorrectionSource {selectedId:string|null;messages:Work[]}
/** This prepares a draft only. It never sends, retries cancellation, or navigates. */
export async function prepareInboxCorrection({root,id,current,snapshot,stop}:{root:string;id:string;current:()=>boolean;snapshot:()=>CorrectionSource;stop:(id:string)=>Promise<boolean>}):Promise<{id:string;text:string}|null>{
  if(!current())return null;
  const before=snapshot(),original=before.messages.find(m=>m.work_id===id);
  if(before.selectedId!==root||!original||original.root_work_id!==root||original.images?.length||!['received','cancelled'].includes(original.inbox_intake??''))return null;
  if(original.inbox_intake==='received'&&!(await stop(id)))return null;
  if(!current())return null;
  const after=snapshot(),confirmed=after.messages.find(m=>m.work_id===id);
  if(after.selectedId!==root||!confirmed||confirmed.root_work_id!==root||confirmed.inbox_intake!=='cancelled'||confirmed.execution_status!=='cancelled'||confirmed.images?.length||confirmed.text!==original.text||confirmed.request_id!==original.request_id||confirmed.previous_turn_id!==original.previous_turn_id)return null;
  return {id,text:confirmed.text};
}
/** Preserve in-flight typing and all attachments; repeated clicks do not duplicate text. */
export function appendCorrection<T extends {text:string;correctionIds?:string[]}>(draft:T,source:{id:string;text:string}):T&{correctionIds:string[]}{
  if(draft.correctionIds?.includes(source.id))return draft as T&{correctionIds:string[]};
  return {...draft,text:draft.text.trim()?draft.text+'\n\n'+source.text:source.text,correctionIds:[...(draft.correctionIds??[]),source.id]};
}
