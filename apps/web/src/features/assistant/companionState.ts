import type {Work} from './assistantApi';
export interface Presence{tone:'idle'|'queued'|'working'|'waiting'|'attention'|'done';label:string;detail:string}
export function receiptPresence(work:Work):Presence{
 const s=work.execution_status,p=work.progress;
 if(p.uncertain||s==='needs_attention'||s==='dispatch_unverified')return{tone:'attention',label:'结果待核对',detail:'工作已保存，但执行结果暂未确认。请勿重复发送。'};
 if(s==='waiting_for_human')return{tone:'waiting',label:'等你确认',detail:p.explanation??'这一步需要你查看已有记录。'};
 if(s==='waiting_for_external')return{tone:'waiting',label:'等外部结果',detail:p.explanation??'正在等外部回执，目前还没有确认结果。'};
 if(s==='blocked'||s==='failed'||s==='authority_unrecorded')return{tone:'attention',label:s==='failed'?'执行未完成':'暂时无法继续',detail:p.explanation??'先看已记录的原因，原问题还在。'};
 if(s==='cancelled')return{tone:'idle',label:'已停止',detail:'之前已完成的部分仍保留。'};
 if(s==='completed')return work.answer!==undefined&&!work.answerUnavailable?{tone:'done',label:'已有答复',detail:'答复已保存在这段对话里。'}:{tone:'attention',label:'答复待核对',detail:'执行已结束，但当前没有可查看的答复。'};
 if(s==='not_dispatched')return{tone:'queued',label:'已保存待执行',detail:'补充已保存，尚未开始处理。'};
 if(s==='queued')return{tone:'queued',label:'已受理',detail:'工作已保存，正在排队，尚未开始处理。'};
 if(s==='starting'||s==='created')return{tone:'queued',label:'准备执行',detail:'工作已受理，正在准备，尚未收到执行结果。'};
 const saved=p.savedToolRecords??0;
 const savedNote=saved>0?`已保存 ${saved} 条工具结果记录。`:'';
 // A running model step is not evidence of the model's thoughts or of a
 // final answer; it may return another tool call.
 if(s==='running'&&p.awaitingModelRecord)return{tone:'working',label:'正在处理',detail:savedNote+'正在等待这一轮模型结果，尚无最终答复。'};
 for(let i=p.steps.length-1;i>=0;i--){
  const step=p.steps[i];
  if(step.kind==='tool'&&step.tone==='active')return{tone:'working',label:'正在处理',detail:savedNote?`${step.label}。${savedNote}`:step.label};
 }
 const knownPhase=p.activity!=='正在思考'?p.activity:undefined;
 const detail=knownPhase
  ? (savedNote?`${knownPhase}。${savedNote}`:knownPhase)
  : (savedNote?'仍在处理。'+savedNote:'已开始处理，新的可见进展会出现在这里。');
 return{tone:'working',label:'正在处理',detail};
}
const REFRESH_ONLY='点「刷新工作」只核对原记录，不会重发。';
const RESULT_READ_ONLY='在原消息下点「核对本次业务结果」；若已收起，先点「查看本次业务结果」。核对只读原记录，不会重跑。';
export type WorkFeedback=Presence&{guidance:string};
/** The only positive acknowledgement comes from an authorized, parsed Work. */
export function currentWorkFeedback({work,selected,pending,delivery,showResultControls}:{
 work?:Work;selected:boolean;pending:boolean;delivery?:{sending:boolean};showResultControls:boolean;
}):WorkFeedback|null{
 if(delivery?.sending)return {tone:'queued',label:'正在提交',detail:'正在提交，尚未确认受理。',guidance:REFRESH_ONLY};
 // A journal without current-tab delivery context can refer to another
 // conversation. Do not replace a known saved Work's state with that journal.
 if(delivery||(pending&&!work))return {tone:'attention',label:'送达待核对',detail:'还未确认消息是否受理。请勿再次发送。',guidance:REFRESH_ONLY};
 if(!work)return selected?{tone:'idle',label:'当前事项',detail:'尚未核对到这项工作的当前状态。',guidance:REFRESH_ONLY}:null;
 const presence=receiptPresence(work);
 const detail=presence.tone==='working'?'工作已保存。'+presence.detail:presence.detail;
 return {...presence,detail,guidance:work.run_id&&showResultControls?RESULT_READ_ONLY:REFRESH_ONLY};
}
export function restartChoices(items:Work[],presences:Record<string,Presence>={}){
 const presence=(w:Work)=>presences[w.work_id]??receiptPresence(w);
 const pinned:Work[]=[];const pick=(predicate:(w:Work)=>boolean)=>{const w=items.find(x=>!pinned.some(y=>y.work_id===x.work_id)&&predicate(x));if(w)pinned.push(w);};
 if(items[0])pinned.push(items[0]);
 pick(w=>['attention','waiting'].includes(presence(w).tone));
 pick(w=>['working','queued'].includes(presence(w).tone));
 return pinned.slice(0,3);
}
