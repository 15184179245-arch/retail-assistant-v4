import {useEffect,useRef,useState} from 'react';
import type {Work} from './assistantApi';
import './terminal-settlement.css';

type ProgressReceipt=Pick<Work,'work_id'|'request_id'|'run_id'|'execution_status'|'answerUnavailable'|'progress'|'answer'>;
type Observation={workId:string;requestId:string;runId:string|null;observedInFlight:boolean;announcement:string};
const inFlight=new Set(['queued','starting','created','running','waiting_for_human','waiting_for_external']);
const terminal=new Set(['completed','failed','blocked','cancelled','needs_attention','dispatch_unverified','authority_unrecorded']);

export function workSettlement(work:ProgressReceipt):{heading:string;announcement:string}|null {
  const status=work.execution_status;
  if(!terminal.has(status))return null;
  if(work.progress.uncertain||status==='needs_attention'||status==='dispatch_unverified')
    return {heading:'执行结果',announcement:'本次工作结果仍待核对。请查看原事项，不要重复发送。'};
  if(status==='completed')
    return work.answer!==undefined&&!work.answerUnavailable
      ?{heading:'助理答复',announcement:'本次工作已答复。可在原事项中查看。'}
      :{heading:'执行结果',announcement:'本次执行已结束，当前没有可查看的答复。'};
  if(status==='cancelled')return {heading:'执行结果',announcement:'本次工作已停止。可在原事项中查看已保存的记录。'};
  if(status==='failed')return {heading:'执行结果',announcement:'本次工作未完成。可在原事项中查看执行记录。'};
  return {heading:'执行结果',announcement:'本次工作暂时无法继续。可在原事项中查看原因。'};
}

function isSameTurn(previous:Observation,work:ProgressReceipt) {
  return previous.workId===work.work_id&&previous.requestId===work.request_id&&
    (previous.runId===null||previous.runId===work.run_id);
}

export function observeWorkSettlement(previous:Observation|undefined, work:ProgressReceipt):Observation {
  const continued=!!previous&&isSameTurn(previous,work);
  const observedInFlight=inFlight.has(work.execution_status)||continued&&previous.observedInFlight;
  return {workId:work.work_id,requestId:work.request_id,runId:work.run_id,observedInFlight,
    announcement:observedInFlight?workSettlement(work)?.announcement??'':''};
}

/** The live region is mounted before the status changes and never receives focus. */
export function TerminalSettlementStatus({scope,work}:{scope:string|null;work?:ProgressReceipt}) {
  const live=useRef<HTMLSpanElement>(null);
  const [prior,setPrior]=useState<{scope:string;observation:Observation}|null>(null);
  useEffect(()=>{
    if(!scope||!work||document.visibilityState==='hidden'||live.current?.closest('[hidden]')){
      setPrior(old=>old?.scope===scope?old:null);
      return;
    }
    setPrior(old=>{
      const previous=old?.scope===scope?old.observation:undefined;
      const next=observeWorkSettlement(previous,work);
      if(previous&&isSameTurn(previous,work)&&previous.runId===next.runId&&
        previous.observedInFlight===next.observedInFlight&&previous.announcement===next.announcement)return old;
      return {scope,observation:next};
    });
  },[scope,work]);
  const text=scope!==null&&prior?.scope===scope&&work&&isSameTurn(prior.observation,work)?prior.observation.announcement:'';
  return <span ref={live} className="assistant-sr-only" data-assistant-terminal-status=""
    role="status" aria-live="polite" aria-atomic="true">{text}</span>;
}
