import { EvidenceDetails } from './EvidenceDetails';
import {LocalExportControls} from './LocalExportControls';
import {savedAnswerText} from './localResultText';
import {conversationUpdateKey} from './conversationUpdate';
import {captureAssistantReadingAnchor,restoreAssistantReadingAnchor,type AssistantReadingAnchor} from './assistantReadingAnchor';
import {FoldedSourcePanels} from './foldedSourcePanels';
import {CompanionInbox} from './CompanionInbox';
import {currentWorkFeedback,receiptPresence} from './companionState';
import {prepareInboxCorrection,appendCorrection} from './inboxCorrection';
import {RunResults,type StageResultDraft} from './RunResults';
import {SourceRowReferenceChip} from './SourceRowReferenceChip';
import {activeDraftSourceRow,sourceRowVisibility,type ScopedDraftSourceRow,type SourceRowVisibility} from './sourceRowReference';
import {canDraftReceiptDetail} from './businessChoice';
import {RecoveryPanel} from './RecoveryPanel';
import {BusinessReadStarters} from './BusinessReadStarters';
import {confirmedFollowupDraft,saveConfirmedFollowupDraft} from './confirmedFollowupDraft';
import {exceedsNewIntakeTextBudget} from './newIntakeDraftFeedback';
import {mergeCarriedWorkDraft,saveCarriedWorkDraft} from './carriedWorkDraft';
import {DraftImages,MessageImages,type DraftImage,type DraftImageDelivery} from './ImageAttachments';
import {uploadImageAsset,validateImageFile} from './imageAssets';
import {transferFiles,transferHasFiles} from './imageTransfer';
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import type { Session } from '../../lib/auth/session';
import { getScopeVersion, getSession, logoutSession, sessionIsExpired, subscribe } from '../../lib/auth/session';
import { useCapabilities, hasCapability, getCapabilityStatus, getCapabilityVersion } from '../../lib/auth/capabilities';
import { navigate } from '../../app/router';
import { assistantCheckpointTextLimit, clearDraftCheckpoint, readDraftCheckpoint, readDraftCheckpointInfo, replaceDraftCheckpoint, saveDraftCheckpoint } from '../../lib/auth/assistantDraftCheckpoint';
import { ConnectionBanner } from '../../app/ConnectionBanner';
import { browserReportsOffline, offlineBeforeSendNotice, useBrowserOffline } from './assistantNetwork';
import { AnswerText } from '../agents/AnswerText';
import { AssistantController, statusLabels } from './assistantApi';
import { AssistantRouteContext, assistantRouteVisibility } from './assistantRouteVisibility';
import type { Work } from './assistantApi';
import { ExecutionTrace } from './ExecutionTrace';
import { TerminalSettlementStatus, workSettlement } from './TerminalSettlement';
import '../../styles/assistant-workspace.css';
import './assistant-mobile-reading.css';

// Keep synchronized with the framed shallow layout in the stylesheet.
const shallowReadingLayout = '(max-width: 960px) and (max-height: 440px)';
const draftAttachmentCapabilities = ['agent.run','tasks.read_self','tasks.manage','media.read','media.write'];

function Icon({ name }: { name: 'sidebar' | 'new' | 'memory' | 'home' | 'send' | 'down' | 'refresh' | 'check' | 'logout' }) {
  const paths = { sidebar: <><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/></>,
    new: <><path d="M12 5H6a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-6M14 5l5 5M10 14l1-4 8-8 4 4-8 8z"/></>,
    memory: <><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 3v18M12 8h4M12 12h4"/></>,
    home: <><path d="m3 10 9-7 9 7v10H3zM9 20v-7h6v7"/></>, send: <path d="M12 20V4m-7 7 7-7 7 7"/>,
    down: <path d="M12 4v16m-7-7 7 7 7-7"/>, refresh: <><path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M18 18a8 8 0 0 1-13-2"/></>,
    check: <path d="m5 12 4 4L19 6"/>, logout: <><path d="M10 4H4v16h6M9 12h12m-4-4 4 4-4 4"/></> };
  return <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
function UserMessage({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const disclosure=useRef<HTMLButtonElement>(null);
  const long = text.length > 600;
  return <div className="assistant-user-message"><div className="assistant-user-bubble">
    {long && <button ref={disclosure} type="button" className="assistant-expand-message assistant-expand-message--top"
      aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起全文' : '展开全文'}</button>}
    <p className={long && !expanded ? 'is-collapsed' : undefined}>{text}</p>
    {long && expanded && <button type="button" className="assistant-expand-message"
      aria-label="收起全文并回到本条消息开头" onClick={()=>{
        const top=disclosure.current;
        // Only this explicit end-of-message action moves reading position.
        top?.focus({preventScroll:true});
        top?.scrollIntoView({block:'nearest',inline:'nearest',behavior:'instant'});
        setExpanded(false);
      }}>收起并回到本条开头</button>}
  </div></div>;
}
function productImageEvidenceKey(work:Work):string|undefined{
 const images=work.citations.filter(c=>c.kind==='product_image_result');
 return work.answer!==undefined&&work.execution_status==='completed'&&images.length?
  JSON.stringify(images.map(c=>[c.objectId,c.version])):undefined;
}
const activeStates = new Set(['queued', 'starting', 'created', 'running']);
const savedReplyChanged='答复或依据已变化，已取消旧版本的操作。请刷新或对当前版本重新操作。';
class SavedReplyChanged extends Error {}
function sameSavedReply(a:Work|undefined,b:Work):boolean{
 return !!a&&a.work_id===b.work_id&&a.run_id===b.run_id&&a.answer===b.answer&&!a.answerUnavailable&&
   JSON.stringify(a.citations)===JSON.stringify(b.citations);
}
function Reply({ work, stop, steer, stopping, busy, hideImageAnswer = false,readExport,keepFocusInResult }: {
 work:Work;stop?:()=>void;steer?:()=>void;stopping:boolean;busy:boolean;hideImageAnswer?:boolean;
 readExport?:(signal:AbortSignal)=>Promise<string>;keepFocusInResult?:()=>void;
}) {
  const actions=useRef<HTMLDivElement|null>(null),heading=useRef<HTMLHeadingElement>(null);
  const lostActionFocus=useRef(false);
  const citationsKey=JSON.stringify(work.citations);
  const copyScope=useMemo(()=>Symbol(),[work.work_id,work.run_id,work.answer,work.answerUnavailable,citationsKey]);
  const activeExport=useRef<{scope:symbol}|null>(null);
  const [exportNotice,setExportNotice]=useState('');
  useLayoutEffect(()=>{
    if(activeExport.current&&activeExport.current.scope!==copyScope){
      activeExport.current=null;
      setExportNotice(savedReplyChanged);
    }
  },[copyScope]);
  async function verifyVisibleReply(signal:AbortSignal){
    if(!readExport)throw Error('答复目前不可读取。');
    const pending={scope:copyScope};
    activeExport.current=pending;
    setExportNotice('');
    try{
      const text=await readExport(signal);
      if(signal.aborted||activeExport.current!==pending)throw new SavedReplyChanged();
      return text;
    }catch(error){
      if(error instanceof SavedReplyChanged&&!signal.aborted&&activeExport.current===pending)
        setExportNotice(savedReplyChanged);
      throw error;
    }finally{
      if(activeExport.current===pending)activeExport.current=null;
    }
  }
  const settlement=hideImageAnswer?null:workSettlement(work);
  const trackActions=useCallback((node:HTMLDivElement|null)=>{
    if(node===null&&actions.current?.contains(document.activeElement))lostActionFocus.current=true;
    actions.current=node;
  },[]);
  useLayoutEffect(()=>{
    if(!lostActionFocus.current)return;
    lostActionFocus.current=false;
    const target=heading.current;
    if(!target||document.activeElement!==document.body||document.visibilityState==='hidden'||target.closest('[hidden]'))return;
    keepFocusInResult?.();
    target.focus({preventScroll:true});
    target.scrollIntoView({block:'nearest',inline:'nearest'});
  });
  if(hideImageAnswer)return <p className="assistant-notice" role="status">图片访问权限已变化，相关答复与执行详情暂不展示，请重新核查。</p>;
  if (work.inbox_intake === 'received' && work.execution_status === 'not_dispatched') return <div className="assistant-queued-message" role="status">
    <span>消息已收到，尚未开始处理。它还不是助理答复。</span>
    {stop && <button type="button" disabled={busy || stopping} onClick={stop}>{stopping ? '正在核对…' : '取消这条排队消息'}</button>}
  </div>;
  const presence = receiptPresence(work);
  const live = activeStates.has(work.execution_status);
  return <div className="assistant-reply">
    <div className={`assistant-reply-mark${presence.tone === 'working' ? ' is-working' : ''}`} aria-hidden="true">三</div>
    <div className="assistant-reply-content">
      {settlement&&<h2 ref={heading} className="assistant-terminal-heading" tabIndex={-1}>{settlement.heading}</h2>}
      {work.answer !== undefined ? <>
        <AnswerText text={work.answer} preserveSavedAddress />
        <div className="assistant-message-meta"><Icon name="check"/><span>已答复</span>
          <EvidenceDetails citations={work.citations} available={!work.answerUnavailable} />
        </div>
        {work.run_id&&readExport&&!work.answerUnavailable&&<LocalExportControls
          scopeKey={copyScope}
          label="全文与依据" filename="V4-助理答复.txt" readFresh={verifyVisibleReply} cancelReplyCopyOnHide/>}
        {exportNotice&&<p className="assistant-notice" role="status">{exportNotice}</p>}
      </> : <div className={`assistant-progress${live ? ' is-working' : ''}`}>
        <span role={live ? 'status' : undefined}>{work.answerUnavailable ? '这条答复当前不可用，需核对保存结果或依据。' : live ? presence.detail : work.progress.activity ?? statusLabels[work.execution_status]}</span>
        {work.progress.explanation && <p>{work.progress.explanation}</p>}
        {work.progress.uncertain && <p>部分执行结果尚未核实，请勿重复发送同一项工作。刷新只核对原记录，不会重新执行。</p>}
      </div>}
      {(steer || stop) && <div ref={trackActions} className="assistant-turn-actions">
        {steer && <button type="button" className="assistant-stop" disabled={busy || stopping} onClick={steer}>补充或纠正</button>}
        {stop && <button type="button" className="assistant-stop" disabled={busy || stopping} onClick={stop}>
          {stopping ? '正在核对停止结果…' : work.inbox_intake === 'received' ? '取消补充' : '停止工作'}</button>}
      </div>}
      {work.progress.steps.length > 0 && <div className="assistant-step-strip" aria-label="步骤概览">
        {work.progress.steps.map(step => <span key={step.sequence} className={`assistant-step-chip is-${step.tone ?? 'neutral'}`}>
          <b className="assistant-step-num">{step.sequence}</b><span>{step.label}</span><small>{step.state}</small>
        </span>)}
      </div>}
      <ExecutionTrace progress={work.progress} workId={work.work_id} runId={work.run_id}/>
    </div>
  </div>;
}

export function AssistantPage({ session, baseUrl = '', embedded = false, framed = false }: { session: Session; baseUrl?: string; embedded?: boolean; framed?: boolean }) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const caps = useCapabilities();
  const offline = useBrowserOffline(), offlineNoticeId = useId();
  const selectionKey = 'v4-assistant-selected:' + JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
  const routeVisibility = framed ? assistantRouteVisibility : undefined;
  const controller = useMemo(() => {
    let saved: string | null = null;
    // A restored tab owns its own selection, just like its unsent text.
    try { saved = sessionStorage.getItem(selectionKey); } catch { /* Optional selection only. */ }
    if (saved === null) {
      try { saved = localStorage.getItem(selectionKey); } catch { /* New tab may have no previous selection. */ }
    }
    const initial = saved === 'new' ? null : saved;
    return new AssistantController(session, {
      getItem: key => localStorage.getItem(key), setItem: (key, value) => localStorage.setItem(key, value), removeItem: key => localStorage.removeItem(key),
    }, baseUrl, initial, routeVisibility);
  }, [session, scope, baseUrl, selectionKey, routeVisibility]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const resultPanelScope=JSON.stringify([scope,caps.version,caps.capabilities,view.privacyEpoch]);
  const foldedSourcePanels=useMemo(()=>new FoldedSourcePanels(),[controller,resultPanelScope]);
  type Draft={text:string;images:DraftImage[];error?:string;recovered?:boolean;correctionIds?:string[];revision?:number;revokedImageDraftIds?:string[];checkpointProblem?:string;unboundReceiptRun?:string};
  const [draftState,setDraftState]=useState<{owner:AssistantController;epoch:number;values:Record<string,Draft>}>(()=>({owner:controller,epoch:view.privacyEpoch,values:{}}));
  const latestDraftState=useRef(draftState);
  useLayoutEffect(()=>{latestDraftState.current=draftState;},[draftState]);
  const draftKey=view.selectedId??'new';
  const correctionSelection=useRef({owner:controller,id:view.selectedId,epoch:view.privacyEpoch,version:0});
  if(correctionSelection.current.owner!==controller||correctionSelection.current.id!==view.selectedId||correctionSelection.current.epoch!==view.privacyEpoch)correctionSelection.current={owner:controller,id:view.selectedId,epoch:view.privacyEpoch,version:correctionSelection.current.version+1};
  const draftCurrent=draftState.owner===controller&&draftState.epoch===view.privacyEpoch;
  const currentDraft:Draft=draftCurrent?draftState.values[draftKey]??{text:'',images:[]}:{text:'',images:[]};
  const draft=currentDraft.text;
  const newDraftTooLong=useMemo(()=>view.selectedId===null&&view.selectionReady&&!!draft.trim()&&
    exceedsNewIntakeTextBudget(draft,view.inputPolicy.maxTextJsonBytes),[view.selectedId,view.selectionReady,draft,view.inputPolicy.maxTextJsonBytes]);
  const [sourceRowBinding,setSourceRowBinding]=useState<ScopedDraftSourceRow|null>(null);
  const [sourceRowNotice,setSourceRowNotice]=useState('');
  const [sourceRowProof,setSourceRowProof]=useState<SourceRowVisibility|null>(null);
  const acceptSourceRowProof=useCallback((next:SourceRowVisibility)=>{
    setSourceRowProof(previous=>previous?.rootWorkId===next.rootWorkId&&previous.runId===next.runId&&
      previous.state===next.state&&previous.snapshotSha256===next.snapshotSha256?previous:next);
  },[]);

  const lastReferenceRun=view.conversation.at(-1)?.run_id;
  const referenceScope=view.selectedId&&lastReferenceRun&&view.conversationLoaded&&caps.status==='ready'&&
    controller.supportsDraftSourceRow()&&canDraftReceiptDetail(caps.hasCapability)&&
    (caps.hasCapability('media.read')||!view.conversation.some(work=>work.images?.length))
    ?JSON.stringify([scope,caps.version,view.privacyEpoch,view.selectedId,lastReferenceRun]):null;
  const scopedSourceRow=activeDraftSourceRow(sourceRowBinding,referenceScope,draft);
  const sourceRowState=sourceRowVisibility(scopedSourceRow,sourceRowProof);
  const boundSourceRow=sourceRowState==='invalid'?null:scopedSourceRow;
  const receiptDraftNeedsIdentity=!!draft.trim()&&!boundSourceRow&&!!lastReferenceRun&&currentDraft.unboundReceiptRun===lastReferenceRun;
  const receiptDraftHintId=useId();
  const sourceRowClosed=sourceRowState==='closed';
  const sourceRowNeedsCheck=!!sourceRowBinding&&sourceRowState!=='current'&&!sourceRowClosed;
  const latestBoundSource=useRef(boundSourceRow);
  useLayoutEffect(()=>{latestBoundSource.current=boundSourceRow;},[boundSourceRow]);
  useLayoutEffect(()=>{
    if(!sourceRowBinding||boundSourceRow)return;
    setSourceRowBinding(null);
    if(sourceRowBinding.rootWorkId===view.selectedId)
      setSourceRowNotice('原单引用已失效，文字草稿未改。需要时请重新核对并选单。');
    else setSourceRowNotice('');
  },[sourceRowBinding,boundSourceRow,view.selectedId]);

  const [carryNotice,setCarryNotice]=useState<{source:string;reason:string}|null>(null);
  const canAttachImages=caps.status==='ready'&&draftAttachmentCapabilities.every(caps.hasCapability);
  const images=currentDraft.images;
  const needsTextOnlyReview=!!currentDraft.revokedImageDraftIds||(!canAttachImages&&images.length>0);
  const checkpointScope=caps.status==='ready'&&caps.hasCapability('agent.run')&&caps.hasCapability('tasks.read_self')&&
    caps.hasCapability('tasks.manage')&&controller.canCheckpointSelection(view.selectedId);
  const canCheckpoint=checkpointScope&&(view.selectedId===null||view.conversationLoaded);
  const canPersistDraft=canCheckpoint||checkpointScope&&view.selectedId!==null&&
    view.draftCheckpointFor?.workId===view.selectedId&&view.draftCheckpointFor.capabilityVersion===caps.version;
  const checkpoint=canPersistDraft?readDraftCheckpointInfo(session,caps.version,draftKey):null;
  const checkpointText=checkpoint?.text??null;
  const checkpointExpiry=view.selectedId===null?(checkpoint?.expiresAt??null):null;
  const [,recheckCheckpointDisplay]=useState(0);
  useEffect(()=>{
    if(checkpointExpiry===null)return;
    let timer:ReturnType<typeof setTimeout>|undefined;
    const check=()=>{
      clearTimeout(timer);
      const remaining=checkpointExpiry-Date.now();
      if(remaining<=0){recheckCheckpointDisplay(n=>n+1);return;}
      timer=setTimeout(check,remaining);
    };
    check();
    window.addEventListener('pageshow',check);
    document.addEventListener('visibilitychange',check);
    return ()=>{
      clearTimeout(timer);
      window.removeEventListener('pageshow',check);
      document.removeEventListener('visibilitychange',check);
    };
  },[checkpointExpiry]);
  const hasCheckpoint=draft.length>0&&checkpointText===draft;
  // The cached text never restores based only on a previous read or a network failure.
  const restorable=canCheckpoint&&!draft&&checkpointText!==null;
  const checkpointConflict=canCheckpoint&&draft.length>0&&checkpointText!==null&&!hasCheckpoint;
  const cachedAlreadyInDraft=checkpointText!==null&&(draft.startsWith(checkpointText+'\n\n')||draft.endsWith('\n\n'+checkpointText));
  const mergedLength=checkpointText!==null&&!cachedAlreadyInDraft?checkpointText.length+2+draft.length:draft.length;
  const delivery = controller.localDeliveryPreview();
  const warnUnsaved=draft.length>0&&!hasCheckpoint&&!checkpointConflict&&!view.mutating&&
    (!view.pending||draft.trim()!==delivery?.content);
  const lastPrivacyEpoch=useRef({controller,epoch:view.privacyEpoch});
  useLayoutEffect(()=>{
    const prior=lastPrivacyEpoch.current;
    if(prior.controller===controller&&prior.epoch!==view.privacyEpoch)clearDraftCheckpoint(session);
    lastPrivacyEpoch.current={controller,epoch:view.privacyEpoch};
  },[controller,session,view.privacyEpoch]);
  const imageResources=useRef(new Map<string,{key:string;epoch:number;bytes:number;abort:AbortController;release:()=>void}>());
  const [fileOver,setFileOver]=useState(false);
  const imagePicker=useRef<HTMLInputElement>(null);
  function releaseImage(id:string){const r=imageResources.current.get(id);if(r){r.abort.abort();r.release();imageResources.current.delete(id);}}
  function updateDraft(key:string,epoch:number,update:(previous:Draft)=>Draft){
    if(!controller.isCurrent())return;
    setDraftState(previous=>{
      if(previous.owner!==controller||previous.epoch!==epoch)return previous;
      const before=previous.values[key]??{text:'',images:[]},after=update(before);
      return after===before?previous:{...previous,values:{...previous.values,[key]:{...after,revision:(before.revision??0)+1}}};
    });
  }
  function checkpointAllowed(whileOffline=false) {
    const live=controller.getSnapshot();
    return controller.canCheckpointSelection(view.selectedId)&&live.privacyEpoch===view.privacyEpoch&&
      getCapabilityStatus()==='ready'&&getCapabilityVersion()===caps.version&&
      hasCapability('agent.run')&&hasCapability('tasks.read_self')&&hasCapability('tasks.manage')&&
      (live.selectedId===null||live.conversationLoaded||whileOffline&&
        live.draftCheckpointFor?.workId===live.selectedId&&live.draftCheckpointFor.capabilityVersion===caps.version);
  }
  function setDraft(value:string){
    if(boundSourceRow&&!value.includes(boundSourceRow.anchor)){
      setSourceRowBinding(null);
      setSourceRowNotice('定位文字已编辑，原单引用已移除。你可以继续编辑草稿。');
    }
    if(canPersistDraft&&checkpointAllowed(true)){
      const stored=readDraftCheckpoint(session,caps.version,draftKey);
      // A shortcut, an import from another conversation, or typing into an empty
      // editor must not silently replace a separate draft on this tab.
      if(stored===null||stored===draft)saveDraftCheckpoint(session,caps.version,draftKey,value);
    }
    updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,text:value,checkpointProblem:undefined,unboundReceiptRun:value.trim()?previous.unboundReceiptRun:undefined}));
  }
  function restoreDraft(){
    if(!restorable||!checkpointAllowed())return;
    const text=readDraftCheckpoint(session,caps.version,draftKey);
    if(text!==null){updateDraft(draftKey,view.privacyEpoch,previous=>previous.text?previous:{...previous,text,checkpointProblem:undefined});textarea.current?.focus();}
  }
  function resolveDraftCheckpoint(merge:boolean){
    if(!checkpointConflict||checkpointText===null||!checkpointAllowed())return;
    const next=merge&&!cachedAlreadyInDraft?checkpointText+'\n\n'+draft:draft;
    if(!replaceDraftCheckpoint(session,caps.version,draftKey,checkpointText,next)){
      updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,checkpointProblem:'暂时无法保存这次选择。已有暂存稿未主动清除，当前输入仍在。'}));
      return;
    }
    updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,text:next,checkpointProblem:undefined}));
    textarea.current?.focus();
  }
  function removeImage(id:string){releaseImage(id);updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,images:previous.images.filter(image=>image.id!==id)}));}
  // Revoking only media/write access does not invalidate an otherwise readable
  // text draft. Dispose its previews and uploads without retrying or deleting an asset.
  useLayoutEffect(()=>{
    if(canAttachImages)return;
    for(const id of imageResources.current.keys())releaseImage(id);
    setDraftState(previous=>{
      if(previous.owner!==controller||previous.epoch!==view.privacyEpoch||!Object.values(previous.values).some(d=>d.images.length))return previous;
      const values={...previous.values};
      for(const [key,draftValue] of Object.entries(values)){
        if(draftValue.images.length)values[key]={...draftValue,images:[],error:undefined,revokedImageDraftIds:draftValue.images.map(image=>image.id)};
      }
      return {...previous,values};
    });
  },[canAttachImages,controller,view.privacyEpoch]);
  const canAttachImagesNow=()=>getCapabilityStatus()==='ready'&&
    draftAttachmentCapabilities.every(hasCapability);
  function reviewTextOnly(){
    if(!controller.isCurrent()||controller.getSnapshot().pending||controller.getSnapshot().mutating)return;
    updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,revokedImageDraftIds:undefined}));
    textarea.current?.focus();
  }
  async function chooseImages(files:File[]){
    const key=draftKey,epoch=view.privacyEpoch,live=controller.getSnapshot(),policy=live.inputPolicy;
    if(!controller.isCurrent()||live.privacyEpoch!==epoch||live.selectedId!==view.selectedId)return;
    if(live.mutating||live.pending||!policy.imageAvailable||!canAttachImagesNow()){
      updateDraft(key,epoch,previous=>({...previous,error:'当前暂时不能添加图片，已有草稿保留。'}));return;
    }
    // Resources are reserved synchronously, including uploads queued earlier in
    // the same browser event turn. React's rendered draft may lag a second paste.
    const reserved=[...imageResources.current.values()].filter(r=>r.key===key&&r.epoch===epoch);
    try{if(!files.length)throw Error('没有识别到图片文件，请直接选择或拖入图片，不支持文件夹。');files.forEach(validateImageFile);if(reserved.length+files.length>policy.maxImages||files.some(file=>file.size>policy.maxImageBytes)||reserved.reduce((n,i)=>n+i.bytes,0)+files.reduce((n,f)=>n+f.size,0)>policy.maxTotalImageBytes)throw Error('这批图片超出当前数量或大小额度，未加入消息。');}
    catch(error){updateDraft(key,epoch,previous=>({...previous,error:error instanceof Error?error.message:String(error)}));return;}
    updateDraft(key,epoch,previous=>({...previous,error:undefined,revokedImageDraftIds:undefined}));
    for(const file of files){
      const id=crypto.randomUUID(),url=URL.createObjectURL(file),abort=new AbortController();let released=false;
      imageResources.current.set(id,{key,epoch,bytes:file.size,abort,release:()=>{if(!released){released=true;URL.revokeObjectURL(url);}}});
      updateDraft(key,epoch,previous=>({...previous,images:[...previous.images,{id,name:file.name,bytes:file.size,url,state:'uploading'}]}));
      void uploadImageAsset(file,{category:'assistant_image',source:'assistant_chat',baseUrl,signal:abort.signal}).then(asset=>{
        if(abort.signal.aborted||!controller.isCurrent()||!canAttachImagesNow())return;
        updateDraft(key,epoch,previous=>({...previous,images:previous.images.map(image=>image.id!==id?image:previous.images.some(other=>other.id!==id&&other.asset?.id===asset.id)?{...image,state:'error',error:'这张图片重复，请移除重复项。'}:{...image,state:'ready',asset})}));
      }).catch(()=>{if(!abort.signal.aborted&&canAttachImagesNow())updateDraft(key,epoch,previous=>({...previous,images:previous.images.map(image=>image.id===id?{...image,state:'error',error:'上传未确认。此图不会附到消息，请移除失败项。'}:image)}));});
    }
  }
  useEffect(()=>()=>{for(const id of imageResources.current.keys())releaseImage(id);},[controller,view.privacyEpoch]);
  useEffect(()=>setFileOver(false),[view.selectedId,view.privacyEpoch]);
  // Only liberate object URLs after the state change actually removed those images:
  // a newer draft, even in a different conversation, can still be using them.
  const committedImages=useRef(new Set<string>());
  useLayoutEffect(()=>{
    const next=new Set(Object.values(draftState.values).flatMap(d=>d.images.map(image=>image.id)));
    for(const id of committedImages.current)if(!next.has(id))releaseImage(id);
    committedImages.current=next;
  },[draftState]);
  const [sidebarOpen, setSidebarOpen] = useState(() => !embedded && (typeof window === 'undefined' || window.innerWidth > 760));
  useEffect(() => {
    if (!framed || typeof window === 'undefined' || !window.matchMedia) return;
    const narrow = window.matchMedia('(max-width: 760px)');
    const closeOnNarrow = () => { if (narrow.matches) setSidebarOpen(false); };
    closeOnNarrow(); narrow.addEventListener('change', closeOnNarrow);
    return () => narrow.removeEventListener('change', closeOnNarrow);
  }, [framed]);
  const [atBottom, setAtBottom] = useState(true);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const compositionActive = useRef(false);
  const sendPressDuringComposition = useRef(false);
  const [composing,setComposing] = useState(false);
  useLayoutEffect(()=>{
    compositionActive.current=false;
    setComposing(false);
  },[controller,view.selectedId,view.privacyEpoch]);
  function setComposition(active:boolean){
    compositionActive.current=active;
    setComposing(active);
  }
  const stream = useRef<HTMLDivElement>(null);
  const mainScroll = useRef<HTMLDivElement>(null);
  const composerDock = useRef<HTMLDivElement>(null);
  const sidebar = useRef<HTMLElement>(null);
  const seenScrollTop = useRef(0);
  const readingAnchors = useRef(new WeakMap<HTMLDivElement,AssistantReadingAnchor>());
  const topbar = useRef<HTMLElement>(null);
  const followLatest = useRef(true);
  function scrollTarget() {
    return framed && typeof window.matchMedia==='function' && window.matchMedia(shallowReadingLayout).matches &&
      !composerDock.current?.contains(document.activeElement)
      ? mainScroll.current : stream.current;
  }
  function observeScroll(el:HTMLDivElement) {
    if(el!==scrollTarget())return;
    seenScrollTop.current=el.scrollTop;
    const near=el.scrollHeight-el.scrollTop-el.clientHeight<100;
    followLatest.current=near;setAtBottom(near);
    const anchor=!near&&stream.current?captureAssistantReadingAnchor(el,stream.current,topbar.current):null;
    if(anchor)readingAnchors.current.set(el,anchor);else readingAnchors.current.delete(el);
  }
  const timeline = controller.displayConversation();
  const deliveryStatus = delivery ? (delivery.sending ? '正在发送，尚未确认送达' : '送达待核对，不会自动重发') : undefined;
  const imageDelivery:DraftImageDelivery|undefined=delivery?.imageCount?(delivery.sending?'sending':'unconfirmed'):undefined;
  const imageHint=!canAttachImages||!images.length?null:imageDelivery==='sending'?'正在发送这条含图片的消息，尚未确认送达。':
    imageDelivery==='unconfirmed'?'这条含图片的消息送达待核对，不会自动重发。':
    currentDraft.images.some(image=>image.state==='error')?'有图片上传未确认，请移除失败项。不会自动重试。':
    currentDraft.images.some(image=>image.state==='uploading')?'图片正在上传，上传完成后仍需手动发送。':
    draft.trim()?'图片已上传，消息尚未发送。':'图片已上传。请写一句说明，再手动发送。';
  const updateKey = useMemo(()=>conversationUpdateKey(view.selectedId,view.privacyEpoch,controller.displayConversation()),
    [controller,view.selectedId,view.privacyEpoch,view.conversation,view.messages]);
  useEffect(() => { controller.start(); return () => controller.dispose(); }, [controller]);
  useEffect(() => { setDraftState({ owner: controller, epoch: view.privacyEpoch, values: {} }); }, [controller, view.privacyEpoch]);
  useEffect(() => {
    if (!view.selectionReady || !controller.isCurrent()) return;
    const selected = view.selectedId ?? 'new';
    try { sessionStorage.setItem(selectionKey, selected); } catch { /* Reads and in-memory drafts still work. */ }
    try { localStorage.setItem(selectionKey, selected); } catch { /* The fallback for other tabs is optional. */ }
  }, [controller, selectionKey, view.selectionReady, view.selectedId]);
  useLayoutEffect(() => { const el = textarea.current; if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 200)}px`; } }, [draft]);
  useLayoutEffect(() => { const el=scrollTarget(); if(followLatest.current&&el)el.scrollTop=el.scrollHeight; }, [updateKey, delivery?.requestId, deliveryStatus]);
  useEffect(()=>{
    if(!framed||typeof window.matchMedia!=='function')return;
    const media=window.matchMedia(shallowReadingLayout);
    const dock=composerDock.current;
    const move=()=>{const el=scrollTarget();if(el)el.scrollTop=followLatest.current?el.scrollHeight:seenScrollTop.current;};
    const rotate=()=>{
      const el=scrollTarget(),content=stream.current;
      const previous=media.matches?content:mainScroll.current;
      if(el&&content&&previous&&previous!==el&&!followLatest.current&&
         restoreAssistantReadingAnchor(readingAnchors.current.get(previous)??null,el,content,topbar.current))return;
      move();
    };
    let scheduled=0;
    const followFocus=()=>{
      if(!media.matches)return;
      cancelAnimationFrame(scheduled);
      scheduled=requestAnimationFrame(move);
    };
    media.addEventListener('change',rotate);
    dock?.addEventListener('focusin',followFocus);dock?.addEventListener('focusout',followFocus);
    return ()=>{
      media.removeEventListener('change',rotate);cancelAnimationFrame(scheduled);
      dock?.removeEventListener('focusin',followFocus);dock?.removeEventListener('focusout',followFocus);
    };
  },[framed]);
  useEffect(() => { const close = (e: KeyboardEvent) => { if (e.key === 'Escape') setSidebarOpen(false); }; window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close); }, []);
  if (getSession() !== session || sessionIsExpired(session) || !caps.hasCapability('agent.run') || !caps.hasCapability('tasks.read_self')) {
    return <p role="alert">请使用有权账号重新进入助理。</p>;
  }
  const followupReason = controller.followupReason();
  const lastTurn=view.conversation.at(-1);
  // A saved inbox message is part of the timeline, but it owns no Run yet.
  const activeResultRun=lastTurn?.run_id&&activeStates.has(lastTurn.execution_status)?lastTurn.run_id:null;
  const hideImageResults=!caps.hasCapability('media.read')&&timeline.some(item=>(item.images?.length??0)>0);
  const canShowResultControls=view.conversationLoaded&&view.selectedId!==null&&
    caps.hasCapability('tasks.read_self')&&caps.hasCapability('agent.run')&&!hideImageResults;
  const feedback=currentWorkFeedback({work:canShowResultControls?lastTurn:undefined,selected:view.selectedId!==null,
    pending:view.pending,delivery,showResultControls:canShowResultControls&&!!lastTurn?.run_id&&timeline.some(work=>work.run_id===lastTurn.run_id)});
  const terminalScope=view.selectedId&&caps.status==='ready'&&!hideImageResults
    ? JSON.stringify([scope,caps.version,view.privacyEpoch,view.selectedId]):null;
  const priorOutcomeUnknown=lastTurn?.progress.uncertain===true;
  const imageBlocked=needsTextOnlyReview||!!currentDraft.error||images.some(image=>image.state!=='ready')||(images.length>0&&!view.inputPolicy.imageAvailable);
  const disabled = checkpointConflict || sourceRowNeedsCheck || imageBlocked || view.mutating || view.pending || !view.selectionReady || view.stoppingId !== null ||
    (view.selectedId === null ? !view.intakeAvailable : followupReason !== null) || !caps.hasCapability('tasks.manage');
  const canSend = !disabled && !offline;
  const routineNotice = (offline && view.notice===offlineBeforeSendNotice) || ['工作与结果已更新。', '工作已保存，进度与结果会显示在这里。', '新工作：发送会创建独立事项，不会追加到上一件工作。'].includes(view.notice) ||
    view.conversationReadBusy && view.notice === '正在读取这件工作…';
  // Navigating from an existing Work to an independent draft does not submit anything
  // or replace its delivery journal. The actual Send gate remains separate.
  const canStartNew = !view.mutating && (view.selectedId!==null || (!view.pending&&view.stoppingId===null));
  const attachmentHelp = `可粘贴或拖入 · 最多${view.inputPolicy.maxImages}张，每张不超过${Math.floor(view.inputPolicy.maxImageBytes/1024/1024)}MiB`;
  const readSavedAnswer = async (work:Work,signal:AbortSignal) => {
    if(!view.selectedId||!work.run_id||work.answer===undefined)throw Error('答复目前不可读取。');
    const fresh=await controller.readSavedReplyForExport(view.selectedId,work.work_id,work.run_id,signal);
    const visible=controller.displayConversation().find(candidate=>candidate.work_id===work.work_id&&candidate.run_id===work.run_id);
    if(signal.aborted||!sameSavedReply(visible,work)||!sameSavedReply(fresh,work))
      throw new SavedReplyChanged();
    return savedAnswerText(work.answer,fresh.citations);
  };
  function bottom() {
    followLatest.current=true;setAtBottom(true);
    const el=scrollTarget();if(el)el.scrollTo({top:el.scrollHeight,behavior:'smooth'});
  }
  function jumpToLatest(activated: HTMLButtonElement) {
    // The jump control disappears at the end. Keep focus in the newest entry,
    // without opening the mobile keyboard or refocusing the composer.
    if (document.activeElement === activated) {
      const latest = stream.current?.querySelector<HTMLElement>('.assistant-conversation > .assistant-turn:last-child');
      (latest??stream.current)?.focus({preventScroll:true});
    }
    followLatest.current=true;setAtBottom(true);
    const el=scrollTarget();
    if(el)el.scrollTo({top:el.scrollHeight,behavior:'instant'});
  }
  function focusComposer() { void controller.selectWork(null); bottom(); textarea.current?.focus(); if (window.innerWidth <= 760) setSidebarOpen(false); }
  function carryTextToNew(){
    const source=view.selectedId, live=controller.getSnapshot(), state=latestDraftState.current;
    if(!source||!controller.isCurrent()||!live.selectionReady||live.selectedId!==source||live.mutating||live.pending||
       state.owner!==controller||state.epoch!==live.privacyEpoch||view.privacyEpoch!==live.privacyEpoch||
       getCapabilityStatus()!=='ready'||getCapabilityVersion()!==caps.version||
       !hasCapability('agent.run')||!hasCapability('tasks.read_self')||!hasCapability('tasks.manage'))return;
    const text=state.values[source]?.text??'';
    if(!text.trim())return;
    const target=state.values.new?.text??'';
    const savedTarget=readDraftCheckpoint(session,caps.version,'new');
    if(target&&savedTarget!==null&&target!==savedTarget){
      setCarryNotice({source,reason:'新工作的编辑稿和暂存稿不同，没有转带。请先打开新工作核对，两份原稿未改。'});
      return;
    }
    const merged=mergeCarriedWorkDraft(target,savedTarget,text,assistantCheckpointTextLimit);
    if(merged===null){
      setCarryNotice({source,reason:'两段草稿合并后超过暂存上限，没有转带。两份原稿均保留。'});
      return;
    }
    try{
      if(saveCarriedWorkDraft(session,caps.version,merged,window.sessionStorage)==='would_evict'){
        setCarryNotice({source,reason:'暂存空间已满；没有转带或移除任何草稿。请打开新工作核对。'});
        return;
      }
    }catch{/* The prior checkpoint stays intact; this tab can still show the merged draft. */}
    setCarryNotice(null);
    updateDraft('new',live.privacyEpoch,previous=>({...previous,text:merged}));
    focusComposer();
  }
  function steer(){if(controller.getSnapshot().selectedId!==view.selectedId)return;bottom();textarea.current?.focus();}
  function carryCancelledText(id:string,text:string){
    if(!controller.isCurrent()||controller.getSnapshot().selectedId!==view.selectedId)return;
    const message=controller.getSnapshot().messages.find(m=>m.work_id===id);
    if(!message||message.inbox_intake!=='cancelled'||message.text!==text||message.images?.length)return;
    const epoch=view.privacyEpoch;void controller.selectWork(null);
    updateDraft('new',epoch,previous=>({...previous,text:previous.text.trim()?previous.text+'\n\n'+text:text,recovered:true}));
    bottom();textarea.current?.focus();
  }
  function prepareChoiceDraft(root:string,run:string):StageResultDraft{
    const originalFocus=document.activeElement;
    const original=latestDraftState.current.values[root];
    const originalText=original?.text??'';
    const originalRevision=original?.revision??0;
    const originalSource=latestBoundSource.current;
    return (text,source,review)=>{
      const live=controller.getSnapshot();
      if(!controller.isCurrent()||!caps.hasCapability('tasks.manage')||!caps.hasCapability('agent.run')||!caps.hasCapability('tasks.read_self')||
         live.privacyEpoch!==view.privacyEpoch||live.selectedId!==root||live.conversation.at(-1)?.run_id!==run||
         live.pending||live.mutating||live.stoppingId!==null||controller.followupReason()!==null)return 'unavailable';
      const editor=textarea.current;
      const current=latestDraftState.current.values[root];
      const activeSource=latestBoundSource.current;
      if((editor&&document.activeElement===editor)||(current?.text??'')!==originalText||
         (current?.revision??0)!==originalRevision||activeSource!==originalSource)
        return 'editor_changed';
      if(!source&&activeSource){
        setSourceRowNotice('请先移除已有原单引用，再从其它结果添加追问。');
        return 'unavailable';
      }
      if(source){
        if(!referenceScope||!controller.supportsDraftSourceRow()||source.rootWorkId!==root||
          source.reference.source_run_id!==run||!text.includes(source.anchor))return 'unavailable';
        if(activeSource&&JSON.stringify(activeSource.reference)!==JSON.stringify(source.reference)){
          setSourceRowNotice('请先移除已有原单引用，再选择另一张单据。');
          return 'unavailable';
        }
        setSourceRowBinding({...source,scopeKey:referenceScope});
        // Only a fresh, validated source click can make a new reference actionable.
        acceptSourceRowProof({rootWorkId:root,runId:run,state:'available',snapshotSha256:source.reference.snapshot_sha256});
        setSourceRowNotice('');
      }
      const appended=originalText.split('\n\n').includes(text)?originalText:
        originalText.trim()?originalText+'\n\n'+text:text;
      // This explicit append is an edit to the current draft, not a second draft.
      // Replace only a checkpoint still equal to the exact text before the click.
      if(appended!==originalText&&canPersistDraft&&checkpointAllowed())
        replaceDraftCheckpoint(session,caps.version,root,originalText,appended);
      updateDraft(root,view.privacyEpoch,previous=>{
        if(previous.text!==originalText)return previous;
        const unboundReceiptRun=review==='unbound_purchase_receipt'?run:source?undefined:previous.unboundReceiptRun;
        return appended===originalText&&unboundReceiptRun===previous.unboundReceiptRun
          ? previous : {...previous,text:appended,unboundReceiptRun};
      });
      if(originalFocus instanceof HTMLButtonElement&&document.activeElement===originalFocus)editor?.focus();
      return 'added';
    };
  }
  async function correctInbox(id:string){
    const root=view.selectedId,epoch=view.privacyEpoch,version=correctionSelection.current.version;
    if(!root||view.pending||view.mutating||view.stoppingId!==null||!caps.hasCapability('tasks.manage'))return;
    const current=()=>hasCapability('tasks.manage')&&controller.isCurrent()&&correctionSelection.current.version===version&&controller.getSnapshot().privacyEpoch===epoch;
    const source=await prepareInboxCorrection({root,id,current,snapshot:controller.getSnapshot,stop:messageId=>controller.stop(messageId)});
    if(!source||!current())return;
    updateDraft(root,epoch,previous=>appendCorrection(previous,source));
    textarea.current?.focus();
  }
  function locate(id: string) {
    const fromSidebar=sidebar.current?.contains(document.activeElement);
    const epoch=view.privacyEpoch;
    followLatest.current = true; setAtBottom(true); void controller.selectWork(id);
    if (window.innerWidth <= 760) {
      setSidebarOpen(false);
      if(fromSidebar)requestAnimationFrame(()=>{
        const current=controller.getSnapshot();
        if(controller.isCurrent()&&current.selectedId===id&&current.privacyEpoch===epoch&&
          (document.activeElement===document.body||sidebar.current?.contains(document.activeElement)))
          stream.current?.focus({preventScroll:true});
      });
    }
  }
  async function submit() {
    if (compositionActive.current || !canSend || browserReportsOffline() || !draft.trim()) return;
    if(sourceRowBinding&&(!boundSourceRow||(sourceRowState!=='current'&&!sourceRowClosed))){
      setSourceRowNotice('原单引用已失效，尚未发送。请重新选单或移除引用。');
      return;
    }
    const sentSourceRow=boundSourceRow;
    const sent = draft, key = draftKey, epoch = view.privacyEpoch, sentImages=currentDraft.images, version=caps.version;
    const references=sentImages.map(image=>({asset_id:image.asset!.id}));
    const sameWritableScope=()=>controller.isCurrent()&&controller.getSnapshot().privacyEpoch===epoch&&
      getCapabilityStatus()==='ready'&&getCapabilityVersion()===version&&hasCapability('agent.run')&&
      hasCapability('tasks.read_self')&&hasCapability('tasks.manage');
    const sameSourceDraft=()=>{
      const current=latestDraftState.current, value=current.values[key];
      return sameWritableScope()&&latestBoundSource.current===sentSourceRow&&
        current.owner===controller&&current.epoch===epoch&&value?.revision===currentDraft.revision&&value?.text===sent;
    };
    let originalCheckpointCleared=false;
    const draftLifecycle={
      ...(sentSourceRow?{sourceRow:sentSourceRow}:{}),
      ...(sentSourceRow&&sourceRowClosed?{closedSourceCheck:{
        stillCurrent:sameSourceDraft,
        onChanged:()=>{
          if(!sameSourceDraft())return;
          setSourceRowBinding(current=>current===sentSourceRow?null:current);
          setSourceRowNotice('原单引用已失效，尚未发送。原草稿保留，请重新选择原单或检查后手动发送。');
        },
      }}:{}),
      // Before journaling, text is still unsent. After this point the only recovery
      // path for uncertain delivery must be the original pending request ID.
      onJournaled:()=>{
        if (sameWritableScope()&&readDraftCheckpoint(session,version,key)===sent) {
          saveDraftCheckpoint(session,version,key,'');
          originalCheckpointCleared=readDraftCheckpoint(session,version,key)===null;
        }
      },
      onConfirmedFollowup:()=>{
        const live=controller.getSnapshot(), state=latestDraftState.current, editable=state.values[key];
        if(!originalCheckpointCleared||key==='new'||live.selectedId!==key||live.pending||!sameWritableScope()||
           state.owner!==controller||state.epoch!==epoch||!editable||editable.revision===currentDraft.revision||
           readDraftCheckpoint(session,version,key)!==null)return;
        const unsent=confirmedFollowupDraft(sent,editable.text);
        if(!unsent||unsent.text.length>assistantCheckpointTextLimit)return;
        // Save in this action's own cleared slot. A storage exception cannot
        // erase other drafts or make the original message resendable.
        const tab=window.sessionStorage;
        const saved=saveConfirmedFollowupDraft(session,version,key,unsent.text,tab);
        if(saved&&unsent.removeDeliveredPrefix){
          updateDraft(key,epoch,previous=>previous.revision===editable.revision&&previous.text===editable.text
            ?{...previous,text:unsent.text}:previous);
        }
      },
      onNotAccepted:()=>{
        const current=latestDraftState.current, editable=current.values[key]?.text;
        if (sameWritableScope()&&!controller.getSnapshot().pending&&current.owner===controller&&current.epoch===epoch&&
            editable&&readDraftCheckpoint(session,version,key)===null)
          saveDraftCheckpoint(session,version,key,editable);
      },
    };
    const clearIfUnedited=()=>{
      if(!controller.isCurrent())return;
      if(sentSourceRow)setSourceRowBinding(current=>current===sentSourceRow?null:current);
      setDraftState(current=>{
        const value=current.values[key];
        // Permission-driven preview disposal isn't a user edit. An original
        // confirmed delivery must still clear only its own unchanged draft.
        const imageIds=value?.revokedImageDraftIds??value?.images.map(i=>i.id);
        return current.owner===controller&&current.epoch===epoch&&value?.revision===currentDraft.revision&&value?.text===sent&&
          JSON.stringify(imageIds)===JSON.stringify(sentImages.map(i=>i.id))
          ? {...current,values:{...current.values,[key]:{text:'',images:[]}}}:current;
      });
    };
    followLatest.current = true;
    setAtBottom(true);
    if (await controller.sendMessage(sent,references,clearIfUnedited,draftLifecycle)) clearIfUnedited();
  }
  return <AssistantRouteContext.Provider value={routeVisibility}><section className={`assistant-layout ${framed ? 'assistant-layout--framed' : ''} ${embedded ? 'assistant-layout--embedded' : ''} ${sidebarOpen ? 'is-sidebar-open' : ''}`} aria-label="零售助理对话工作台"
    onDragOver={event=>{if(transferHasFiles(event.dataTransfer)){event.preventDefault();event.dataTransfer.dropEffect='copy';setFileOver(true);}else if(Array.from(event.dataTransfer.types).includes('text/uri-list'))event.preventDefault();}}
    onDragLeave={event=>{if(!(event.relatedTarget instanceof Node)||!event.currentTarget.contains(event.relatedTarget))setFileOver(false);}}
    onDrop={event=>{setFileOver(false);if(!transferHasFiles(event.dataTransfer)){if(Array.from(event.dataTransfer.types).includes('text/uri-list')){event.preventDefault();updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,error:'请拖入图片文件；网页链接不会自动下载为图片。'}));}return;}event.preventDefault();void chooseImages(transferFiles(event.dataTransfer));}}>
    {sidebarOpen && <button className="assistant-sidebar-scrim" aria-label="关闭侧栏" onClick={() => setSidebarOpen(false)} />}
    <aside ref={sidebar} className="assistant-sidebar" aria-label="助理导航" inert={!sidebarOpen}>
      <div className="assistant-sidebar-brand"><span className="assistant-brand-symbol">三</span><strong>零售助理</strong>
        <button className="assistant-icon-button" aria-label="收起侧栏" onClick={() => setSidebarOpen(false)}><Icon name="sidebar"/></button>
      </div>
      <button className="assistant-nav-button" onClick={focusComposer}><Icon name="new"/>交代新工作</button>
      {caps.hasCapability('memory.read') && <button className="assistant-nav-button" onClick={() => navigate('/memory')}><Icon name="memory"/>助理记忆</button>}
      {!framed && <button className="assistant-nav-button" onClick={() => navigate('/')}><Icon name="home"/>经营工作台</button>}
      <div className="assistant-history-heading">最近工作 <span>{view.items.length || ''}</span></div>
      <nav className="assistant-history" aria-label="最近工作">
        {view.items.map(work => <button key={work.work_id} className={view.selectedId === work.work_id ? 'is-selected' : ''} onClick={() => locate(work.work_id)} title={work.title}>
          <span>{work.title}</span>{['working','queued','waiting','attention'].includes((view.historyPresence[work.work_id]??receiptPresence(work)).tone) && <><i aria-label={(view.historyPresence[work.work_id]??receiptPresence(work)).label} className="assistant-work-dot"/><small className="assistant-history-state" aria-hidden="true">{(view.historyPresence[work.work_id]??receiptPresence(work)).label}</small></>}
        </button>)}
        {!view.items.length && <p className="assistant-sidebar-empty">你的工作会保存在这里</p>}
        {view.hasMore && <button disabled={view.busy} onClick={() => { followLatest.current = false; void controller.loadMore(); }}>查看更早的工作</button>}
      </nav>
      <div className="assistant-account"><span className="assistant-avatar">{session.username.slice(0, 1).toUpperCase()}</span><div><strong>{session.username}</strong><small>本机开发试用</small></div>
        <button className="assistant-icon-button" title="退出登录" aria-label="退出登录" onClick={() => { void logoutSession().then(() => navigate('/login')); }}><Icon name="logout"/></button>
      </div>
    </aside>
    <div className="assistant-main" ref={mainScroll} onScroll={event=>observeScroll(event.currentTarget)}>
      <header className="assistant-topbar" ref={topbar}>
        {!sidebarOpen && <button className="assistant-icon-button" aria-label="展开侧栏" onClick={() => setSidebarOpen(true)}><Icon name="sidebar"/></button>}
        <div className="acp-identity"><h1>零售助理</h1><small title={view.conversation[0]?.title}>{view.conversation[0]?.title??(view.selectedId!==null?'当前事项':'聊一个新问题')}</small></div>
        {feedback&&<span title={feedback.detail} className={'acp-badge is-'+feedback.tone}>{feedback.label}</span>}
        <div className="assistant-topbar-actions">
          <button type="button" className="assistant-icon-button" onClick={focusComposer} disabled={!canStartNew} aria-label="＋ 新建工作" title="另起话题"><Icon name="new"/></button>
          <button className="assistant-icon-button assistant-refresh" aria-label="刷新工作" title="刷新工作" disabled={view.busy} onClick={() => { void controller.refresh(); }}><Icon name="refresh"/></button>
        </div>
      </header>
      <TerminalSettlementStatus scope={terminalScope} work={view.conversationLoaded?lastTurn:undefined}/>
      {!embedded && <ConnectionBanner session={session} audience="assistant"/>}
      {feedback&&<section className={'acp-now is-'+feedback.tone} aria-label="助理当前进展">
        <div className="acp-now-copy"><span>{feedback.detail}</span><small>{feedback.guidance}</small></div>
        {['waiting','attention'].includes(feedback.tone)&&!delivery&&view.selectedId&&!followupReason&&caps.hasCapability('tasks.manage')&&
          <button type="button" onClick={steer}>补充或纠正</button>}
      </section>}
      {view.selectedId!==null&&<RecoveryPanel messages={view.messages} currentUnresolved={view.conversation.at(-1)?.progress.uncertain??false} busy={view.busy} canStart={!view.mutating&&!view.pending&&view.stoppingId===null} canManage={caps.hasCapability('tasks.manage')} onRefresh={()=>{void controller.refresh();}} onNew={focusComposer} onCancel={id=>{void controller.stop(id);}} onCarry={carryCancelledText} onCorrect={id=>{void correctInbox(id);}}/>}

      <div ref={stream} className="assistant-stream" aria-label="对话记录" tabIndex={0} onScroll={event=>observeScroll(event.currentTarget)}>
        <div className="assistant-conversation">
          {view.selectionReady && view.selectedId === null && !delivery ? <div className="assistant-welcome"><span className="assistant-welcome-mark" aria-hidden="true">三</span><h2>想问什么？</h2><p>直接发消息，也可以接着聊之前的事。</p><CompanionInbox items={view.items} open={locate} presences={view.historyPresence}/>
            <div className="assistant-suggestions">
              <BusinessReadStarters
                enabled={canCheckpoint&&!draft.trim()&&!currentDraft.images.length&&!restorable}
                hasCapability={hasCapability}
                onDraft={text=>{
                  const current=controller.getSnapshot();
                  if(!controller.isCurrent()||current.selectedId!==null||current.pending||current.mutating||
                    !hasCapability('tasks.manage')||!hasCapability('tasks.read_self')||!hasCapability('agent.run'))return;
                  setDraft(text);textarea.current?.focus();
                }}
              />
              {canCheckpoint && !draft.trim() && !currentDraft.images.length && !restorable &&
                hasCapability('guanjia.company.read') && hasCapability('guanjia.sales.read') && (
                <button type="button" onClick={() => {
                  const current = controller.getSnapshot();
                  if (!controller.isCurrent() || current.selectedId !== null || current.pending || current.mutating ||
                    !hasCapability('tasks.manage') || !hasCapability('tasks.read_self') || !hasCapability('agent.run')) return;
                  setDraft('请结合本店今年历史销售缓存与今日实时已过账销售明细，统计今年销量最高的商品及规格排行（区分单品 SKU 与同规格合并，注明完整核验日期范围与计量单位）。');
                  textarea.current?.focus();
                }}>查今年热销商品规格排行</button>
              )}
              {['我之前让你记住了什么？','整理一下待处理的事'].map(text=>
                <button key={text} type="button" onClick={()=>{setDraft(text);textarea.current?.focus();}}>{text}</button>,
              )}
            </div>
          </div> : <>
            {!view.conversationLoaded && <p role="status">{view.conversationReadBusy
              ? '对话暂时没读到。刷新只会重新读取，不会重发消息。'
              : view.busy ? '正在读取这件事…' : '暂时无法读取，请刷新重试。'}</p>}
            {timeline.map(work => <article id={'work-' + work.work_id} key={work.work_id} className="assistant-turn" tabIndex={work.work_id===timeline.at(-1)?.work_id?-1:undefined} aria-label={work.title}>
              <UserMessage text={work.text} />
              <MessageImages images={work.images??[]} canRead={caps.hasCapability('media.read')} baseUrl={baseUrl}/>
              <time className="assistant-message-time" dateTime={work.created_at}>{new Date(work.created_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time>
              <Reply work={work} readExport={signal=>readSavedAnswer(work,signal)} hideImageAnswer={hideImageResults} keepFocusInResult={()=>{followLatest.current=false;}} stopping={view.stoppingId === work.work_id} busy={view.mutating}
                steer={!followupReason && caps.hasCapability('tasks.manage') && activeStates.has(work.execution_status) &&
                  (work.work_id === lastTurn?.work_id || !!work.run_id && work.run_id === lastTurn?.run_id) ? steer : undefined}
                stop={caps.hasCapability('tasks.manage') && (['queued', 'starting', 'created', 'running', 'waiting_for_human', 'waiting_for_external'].includes(work.execution_status) )
                  ? () => { void controller.stop(work.work_id); } : undefined}/>
              {canShowResultControls && view.selectedId && work.run_id &&
                <RunResults key={JSON.stringify([scope,caps.version,caps.capabilities,view.privacyEpoch,view.selectedId,work.run_id])}
                 initiallyFolded={foldedSourcePanels.has(view.selectedId,work.run_id)} onDisclosureChange={open=>{
                   if(controller.isCurrent()&&controller.getSnapshot().selectedId===view.selectedId)
                     foldedSourcePanels.set(view.selectedId!,work.run_id!,!open);
                 }}
                 controller={controller} root={view.selectedId} run={work.run_id} executionStatus={work.execution_status} outcomeUnknown={work.progress.uncertain} sourceReadIssues={work.progress.sourceReadIssues}
                 onSourceVisibility={work.work_id===lastTurn?.work_id?acceptSourceRowProof:undefined}
                 imageBaseUrl={baseUrl} imageEvidenceKey={productImageEvidenceKey(work)} latest={work.work_id===timeline.at(-1)?.work_id} running={activeStates.has(work.execution_status)}
                 followActiveRun={work.run_id===activeResultRun}
                 savedAnswer={work.answer===undefined?undefined:work.citations.length?'answer-with-evidence':'answer'} onDraft={!disabled&&work.work_id===timeline.at(-1)?.work_id?()=>prepareChoiceDraft(view.selectedId!,work.run_id!):undefined}/>}
            </article>)}
            {delivery && <article key={delivery.requestId} className="assistant-turn assistant-local-delivery" tabIndex={-1} aria-label="刚发送的消息">
              <UserMessage text={delivery.content}/>
              <p className={`assistant-delivery-status${delivery.sending ? '' : ' is-unconfirmed'}`} role="status">
                {delivery.imageCount > 0 && <span>{delivery.imageCount} 张图片 · </span>}{deliveryStatus}
              </p>
            </article>}
          </>}
        </div>
      </div>
      <div ref={composerDock} className={`assistant-composer-dock${fileOver?' is-file-over':''}`}>
        {!atBottom && <button type="button" className="assistant-jump" onClick={event=>jumpToLatest(event.currentTarget)}><Icon name="down"/>回到最新</button>}
        <div className="assistant-composer-extras">
        {boundSourceRow&&<SourceRowReferenceChip ordinal={boundSourceRow.reference.row_index+1}
          disabled={view.pending||view.mutating} onRemove={()=>{
           const live=controller.getSnapshot();
           if(!controller.isCurrent()||live.pending||live.mutating||live.selectedId!==boundSourceRow.rootWorkId||
              live.conversation.at(-1)?.run_id!==boundSourceRow.reference.source_run_id||live.privacyEpoch!==view.privacyEpoch||
              getCapabilityStatus()!=='ready'||getCapabilityVersion()!==caps.version)return;
           setSourceRowBinding(current=>current===boundSourceRow?null:current);
           setSourceRowNotice('已移除原单引用，草稿文字保持。');
           textarea.current?.focus({preventScroll:true});
          }}/>}
        {boundSourceRow&&sourceRowClosed&&<p role="status" className="assistant-notice">
          {view.mutating?'正在核对原单引用。':'结果已收起。手动发送时会先重新核对原单引用；也可以先移除引用。'}
        </p>}
        {boundSourceRow&&sourceRowNeedsCheck&&<p role="status" className="assistant-notice">
          正在核对原单引用。核对通过前不能带引用发送；你仍可继续编辑，或移除引用。
        </p>}
        {sourceRowNotice&&<p role="status" className="assistant-notice">{sourceRowNotice}</p>}
        {fileOver&&<p className="assistant-image-drop-hint" role="status">松开后添加到当前对话草稿，不会自动发送</p>}
        {view.recoveredDelivery&&<div className="assistant-notice" role="status">
          上一条消息已核实送达，没有重发。{view.selectedId!==view.recoveredDelivery.rootId&&
            <button type="button" onClick={()=>{const receipt=controller.getSnapshot().recoveredDelivery;if(receipt)locate(receipt.rootId);}}>打开原对话</button>}
          <button type="button" aria-label="关闭送达提示" onClick={()=>controller.dismissRecoveredDelivery()}>知道了</button>
        </div>}
        {!routineNotice && <p className="assistant-notice" role="status">{view.notice}</p>}
        {followupReason && !view.conversationReadBusy && <div className="assistant-notice" role="status">
          {priorOutcomeUnknown?'上一轮结果仍待核对，原请求不会自动重发。你可以核对原记录，也可以另问新问题。':followupReason}
          {draft.trim() && view.selectedId && <button className="acp-carry" type="button" onClick={carryTextToNew} disabled={view.mutating||view.pending}>
            {priorOutcomeUnknown?'把草稿带到新问题（不发送）':'带这段文字问新问题'}</button>}
          {carryNotice?.source===view.selectedId&&<p>{carryNotice?.reason}</p>}
          {draft.trim()&&canAttachImages&&images.length>0&&<small>图片仍保留在本段对话的草稿中。</small>}
        </div>}
        {currentDraft.recovered&&<p className="assistant-notice">已带到新工作草稿，原有输入保留。尚未发送，旧记录不会重跑。</p>}
        {!!currentDraft.correctionIds?.length&&<p className="assistant-notice" role="status">旧补充取消已确认，原文已带回本对话草稿。已有输入和附件保留；请修改后再发送。当前运行中的工作不会因此停止。</p>}
        {restorable&&<p className="assistant-notice"><button type="button" onClick={restoreDraft}>恢复此标签页的文字草稿</button> 图片不会跨刷新恢复。</p>}
        {checkpointConflict&&<div className="assistant-notice" role="status">
          此事项另有暂存稿。当前输入尚未暂存，请先选择保留方式，再发送或关闭页面。
          <div className="assistant-recovery-actions">
            <button type="button" disabled={mergedLength>assistantCheckpointTextLimit} onClick={()=>resolveDraftCheckpoint(true)}>合并暂存稿到当前文字</button>
            <button type="button" disabled={draft.length>assistantCheckpointTextLimit} onClick={()=>resolveDraftCheckpoint(false)}>用当前输入替换暂存稿</button>
          </div>
          {mergedLength>assistantCheckpointTextLimit&&<p>合并后超过本机暂存长度。可先缩短当前文字，再选择合并。</p>}
          {currentDraft.checkpointProblem&&<p>{currentDraft.checkpointProblem}</p>}
        </div>}
        {hasCheckpoint&&<p className="assistant-composer-footnote">此标签页暂存文字草稿一小时，不会自动发送。图片请在刷新前保留原件。</p>}
        {warnUnsaved&&<p className="assistant-composer-footnote" role="status">{view.selectedId===null
          ?'这段文字目前只在本页，没有可恢复的暂存。离开或关闭页面可能丢失这次编辑。'
          :'此标签页未能暂存这段文字。离开或关闭页面可能丢失这次编辑。'}</p>}
        {canAttachImages&&<DraftImages images={images} remove={removeImage} disabled={view.mutating||view.pending} delivery={imageDelivery}/>}
        {needsTextOnlyReview&&<div className="assistant-notice" role="status">
          图片权限已变化，草稿附件已移除，文字仍保留。
          <button type="button" disabled={view.mutating||view.pending||!caps.hasCapability('tasks.manage')} onClick={reviewTextOnly}>仅保留文字，继续编辑</button>
        </div>}
        {currentDraft.error&&<div className="assistant-notice" role="alert">{currentDraft.error}<button type="button" onClick={()=>updateDraft(draftKey,view.privacyEpoch,previous=>({...previous,error:undefined}))}>不附这批图片</button></div>}
        {imageHint&&<p className="assistant-notice" role="status">{imageHint}</p>}
        {view.pending && !delivery && <p className="assistant-composer-footnote" role="status">
          {view.selectedId===null
            ? (canCheckpoint?'上一条送达仍待核对。可暂存独立的新草稿；核对完成后再手动发送。':'上一条送达仍待核对。当前未能暂存新输入，请等待核对，不会自动发送。')
            : '正在核对发送结果，请勿重复发送'}
        </p>}
        </div>
        {newDraftTooLong&&<p className="assistant-composer-footnote" id="assistant-new-draft-budget" role="status">
          这条消息超过当前传输上限。请缩短或分段；草稿仍可编辑。
        </p>}
        {receiptDraftNeedsIdentity&&<p className="assistant-composer-footnote" id={receiptDraftHintId} role="status">
          草稿未绑定原入库单。请核对并填入入库单号和单据日期，再决定是否发送。
        </p>}
        <form className="assistant-composer" onSubmit={event => { event.preventDefault(); void submit(); }}>
          <label className="assistant-sr-only" htmlFor="assistant-request">给助理发消息</label>
          <textarea ref={textarea} id="assistant-request" rows={1} value={draft}
            aria-describedby={[newDraftTooLong?'assistant-new-draft-budget':'',receiptDraftNeedsIdentity?receiptDraftHintId:''].filter(Boolean).join(' ')||undefined}
            onChange={event => setDraft(event.target.value)}
            onCompositionStart={()=>setComposition(true)} onCompositionEnd={()=>setComposition(false)}
            onPaste={event=>{if(transferHasFiles(event.clipboardData))void chooseImages(transferFiles(event.clipboardData));}}
            placeholder={view.selectedId ? activeStates.has(lastTurn?.execution_status??'')?'补充信息，或告诉助理换个方向…':'继续问，或补充一句…' : '发消息给零售助理…'} onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !compositionActive.current && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                event.preventDefault(); if (canSend && draft.trim()) void submit();
              }
            }}/>
          <div className="assistant-composer-toolbar">
            {view.inputPolicy.imageAvailable&&canAttachImages&&<div className="assistant-attach-control">
              <input ref={imagePicker} type="file" accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp" multiple hidden onChange={event=>{const files=Array.from(event.target.files??[]);event.target.value='';if(files.length)void chooseImages(files);}}/>
              <button type="button" disabled={view.mutating||view.pending} title={attachmentHelp} aria-describedby="assistant-attachment-help" onClick={()=>imagePicker.current?.click()}>＋ 添加图片</button>
              <span id="assistant-attachment-help" className="assistant-sr-only">{attachmentHelp}</span>
            </div>}
            <span className="assistant-key-hint">Enter 发送 · Shift + Enter 换行</span>
            <span id={offlineNoticeId} role="status" aria-atomic="true" className={offline?undefined:'assistant-sr-only'}
              style={offline?{color:'var(--warning-ink, #704d00)'}:undefined}>{offline?'浏览器报告离线，可编辑；联网后需手动发送。':''}</span>
            <button className="assistant-send" type="submit" aria-label="发送消息" aria-describedby={offline?offlineNoticeId:undefined}
              disabled={!canSend || !draft.trim()} aria-disabled={composing||undefined}
              onPointerDown={()=>{sendPressDuringComposition.current=compositionActive.current;}}
              onPointerCancel={()=>{sendPressDuringComposition.current=false;}}
              onClick={event=>{
                // A tap can finish composition on blur before the form sees its click.
                const blocked=compositionActive.current||(event.detail>0&&sendPressDuringComposition.current);
                sendPressDuringComposition.current=false;
                if(blocked)event.preventDefault();
              }}><Icon name="send"/></button>
          </div>
        </form>
        {view.selectedId===null&&view.stoppingId!==null&&<p className="assistant-composer-footnote" role="status">
          上一次停止仍待核对。你可以先写新问题，核对完成后再手动发送。
        </p>}
      </div>
    </div>
  </section></AssistantRouteContext.Provider>;
}
