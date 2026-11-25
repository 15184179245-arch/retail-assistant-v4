import {useAssistantRouteSignal} from './assistantRouteVisibility';
import {ProductBarcodeRemainderMemory} from './productBarcodeRemainderMemory';
import {matchesProductBarcodeArchiveCheck} from './productBarcodePage';
import {PurchaseQuantityComparisonPane} from './PurchaseQuantityComparisonPane';
import {canOpenPurchaseQuantityComparison,type PurchaseQuantityChoices} from './purchaseQuantityComparison';
import {canDraftReceiptDetail,verifiedChoiceDraft,type ChoiceRef} from './businessChoice';
import {ApiError} from '../../lib/api/client';
import {hasCapability} from '../../lib/auth/capabilities';
import {selectedPurchaseSource,type DraftSourceRow,type SourceRowVisibility} from './sourceRowReference';
import {LocalExportControls} from './LocalExportControls';
import {hasExportableResult,savedResultText} from './localResultText';
import {useEffect,useId,useLayoutEffect,useRef,useState} from 'react';
import {needsVerifiedSourceDirectory,VerifiedSourceDirectory} from './VerifiedSourceDirectory';
import {PublicResultTables,type SavedAnswerPresentation} from './PublicResultTables';
import {PublicResultSlot,parseResultSnapshot,isPartialObservation,type ResultSnapshot} from './publicResultSnapshot';
import type {AssistantController} from './assistantApi';
import type {SourceReadIssue} from './workProgress';
import {allowsOperationalReceipts} from './operationalReceiptPage';
import {isTransferBlock} from './operationalTransfer';
import {ProductResultImageSection} from './productResultImages/ProductResultImageSection';
import {LocalSalesCoveragePanel} from './LocalSalesCoveragePanel';
import {LocalSalesSavedRankingPanel} from './LocalSalesSavedRankingPanel';
import {SavedStockObservationCard} from './SavedStockObservationCard';
import {canReadSavedStockStep} from './savedStockObservation';
import {SavedStockLossDocumentsCard} from './SavedStockLossDocumentsCard';
import {canReadSavedStockLoss} from './savedStockLossDocuments';
import {SavedWarehouseDirectoryCard} from './SavedWarehouseDirectoryCard';
import {canReadSavedWarehouseDirectory} from './savedWarehouseDirectory';
import {SavedLegacyProductCard} from './SavedLegacyProductCard';
import {canReadSavedLegacyProduct} from './savedLegacyProduct';
import {mayReadStoredSalesCoverage} from './localSalesCoverage';
import {browserOffline,readbackConnectionHint,readbackNoLongerAccessible,retryableReadbackDisconnect,type ReadbackConnection} from './readbackConnectivity';

/** Reports can remain authorized when the overall Work failed before its answer. */
export function FailedReportNotice({status,savedAnswer,snapshot}:{
 status?:string;savedAnswer?:SavedAnswerPresentation;snapshot:ResultSnapshot|null;
}){
 if(status!=='failed'||savedAnswer!==undefined||snapshot?.state!=='available'||!snapshot.blocks.some(block=>block.type==='report_table'||block.type==='operational_receipt_page'||block.type==='operational_receipt_detail'||block.type==='product_barcode_page'||isTransferBlock(block)))return null;
 return <p className="assistant-notice">这次未完成，当前没有可查看的完整答复。下方是已核对的来源页，不等于完整分析。</p>;
}

export function ActiveReportNotice({snapshot}:{snapshot:ResultSnapshot}) {
 if(snapshot.state==='withheld')return null;
 return snapshot.blocks.length
  ? <p className="assistant-notice">还在处理。以下是目前已核对的来源，不是最终答复。</p>
  : <p role="status">还在处理，暂时没有可展示的业务来源。不需要重发。</p>;
}

export function ReadbackIntentNotice({outcomeUnknown}:{outcomeUnknown:boolean}) {
 return outcomeUnknown?<p className="assistant-notice">“核对本次业务结果”只读取原记录，不会重跑。新的问题由你手动发送。</p>:null;
}

/** Distinguish bound, known source step outcomes from website errors or an empty page. */
export function SourceReadIssueNotice({status,snapshot,issues}:{status?:string;snapshot:ResultSnapshot|null;issues?:SourceReadIssue[]}){
 if((status!=='completed'&&status!=='failed')||!snapshot||snapshot.state==='withheld'||!issues?.length)return null;
 const labels=(state:SourceReadIssue['state'])=>issues.filter(issue=>issue.state===state).map(issue=>issue.label).join('、');
 const failed=labels('failed'),unknown=labels('unknown');
 return <div className="assistant-notice" role="note" aria-label="来源读取状态">
  {failed&&<p>来源读取未完成：{failed}。</p>}
  {unknown&&<p>来源读取结果待核对：{unknown}。</p>}
  <p>未核对的来源不能按零计。下方只显示已核对且可查看的结果；核对不会重新执行。</p>
 </div>;
}

/** Each refresh re-authorizes at Host; inbox receipts cannot interrupt the current Run read. */
export type StageResultDraft=(text:string,source?:DraftSourceRow,review?:'unbound_purchase_receipt')=>
 'added'|'editor_changed'|'unavailable';

export function RunResults({controller,root,run,latest=false,running=false,followActiveRun=false,executionStatus,outcomeUnknown=false,sourceReadIssues,savedAnswer,onDraft,onSourceVisibility,imageBaseUrl='',imageEvidenceKey,initiallyFolded=false,onDisclosureChange}:{initiallyFolded?:boolean;onDisclosureChange?:(open:boolean)=>void;savedAnswer?:SavedAnswerPresentation;onDraft?:()=>StageResultDraft;onSourceVisibility?:(proof:SourceRowVisibility)=>void;controller:AssistantController;root:string;run:string;latest?:boolean;running?:boolean;followActiveRun?:boolean;executionStatus?:string;outcomeUnknown?:boolean;sourceReadIssues?:SourceReadIssue[];imageBaseUrl?:string;imageEvidenceKey?:string}) {
 const sourceAnchorPrefix=useId()+'-verified-source';
 const permit=useAssistantRouteSignal(),panel=useRef<HTMLDivElement>(null);
 const remainderMemory=useRef(new ProductBarcodeRemainderMemory()).current;
 const [remainderEpoch,setRemainderEpoch]=useState(0),[remainderNotice,setRemainderNotice]=useState('');
 const [remainderVerifiedSnapshot,setRemainderVerifiedSnapshot]=useState<ResultSnapshot|null>(null);
 const purchaseChoices=useRef<{digest:string;value:PurchaseQuantityChoices}|null>(null);
 const [purchaseEpoch,setPurchaseEpoch]=useState(0);
 useLayoutEffect(()=>{remainderMemory.clear();purchaseChoices.current=null;setRemainderNotice('');},[permit,remainderMemory]);
 // The running source is the authoritative Run, not the newest receipt.
 // Keep `latest` separately for whether a new source choice may enter the composer.
 const followLive=running&&(latest||followActiveRun);
 const [expanded,setExpanded]=useState<boolean|null>(()=>initiallyFolded?false:null), open=expanded??(latest||followLive);
 const panelId=useId();
 function toggle(){
  if(open){remainderMemory.clear();purchaseChoices.current=null;setRemainderNotice('');}
  setExpanded(!open);
  onDisclosureChange?.(!open);
 }
 const [publication,setPublication]=useState<{snapshot:ResultSnapshot|null;permit?:AbortSignal}|null>(null), [status,setStatus]=useState('');
 const snapshot=publication?.permit===permit && !permit?.aborted ? publication?.snapshot??null : null;
 const setSnapshot=(value:ResultSnapshot|null)=>setPublication({snapshot:value,permit});
 const [watchLimited,setWatchLimited]=useState(false);
 const manual=useRef<(()=>void)|null>(null);
 const [connection,setConnection]=useState<ReadbackConnection>('current');
 const confirm=useRef<AbortController|null>(null),[choiceStatus,setChoiceStatus]=useState('');
 useEffect(()=>{
  setWatchLimited(false);
  const slot=new PublicResultSlot();let streamAvailable=true;let job:AbortController|null=null,timer:ReturnType<typeof setTimeout>|undefined,alive=true;
  const hidden=()=>permit?.aborted===true||document.visibilityState==='hidden';let wasOffline=false,needsFreshRead=false;
  function stopRead(){clearTimeout(timer);job?.abort();job=null;slot.reset();}
  function reset(){stopRead();purchaseChoices.current=null;remainderMemory.clear();setRemainderNotice('');wasOffline=false;needsFreshRead=false;setSnapshot(null);setStatus('');setConnection('current');}
  function offline(){
   if(!controller.isCurrent()){reset();return;}
   if(wasOffline)return;
   wasOffline=true;needsFreshRead=true;stopRead();confirm.current?.abort();
   purchaseChoices.current=null;
   remainderMemory.clear();setRemainderNotice('');
   // Keep this already rendered, same-session view readable. It is not fresh proof.
   setStatus('');setChoiceStatus('');setConnection('offline');
  }
  async function read(periodic=false) {
   if(!alive||!open||hidden())return;
   if(!controller.isCurrent()){reset();return;}
   if(browserOffline()){offline();return;}
   const recovery=needsFreshRead;
   wasOffline=false;
   clearTimeout(timer);job?.abort();const a=new AbortController();job=a;
   const t=slot.begin({workId:root,runId:run,executionStatus});
   if(recovery){setConnection('checking');setStatus('');}
   else{if(periodic)remainderMemory.pauseForRecheck();setSnapshot(null);setStatus('正在核对…');}
   let cadence=running?3000:30000,watchdog:ReturnType<typeof setTimeout>|undefined;
   function expire(){if(job===a&&alive){slot.fail(t);setSnapshot(null);setStatus('连接已中断，正在重新核对。');a.abort();}}
   function renew(){clearTimeout(watchdog);watchdog=setTimeout(expire,7500);}
   try{
    if(!recovery && followLive && streamAvailable){
     try {
      renew();
      await controller.watchRunResults(root,run,a.signal,frame=>{
       if(!alive||job!==a||a.signal.aborted)return;
       renew();
       if(frame.kind==='snapshot'){
        if(!slot.accept(t,frame.value))throw Error('过期结果。');
        setSnapshot(slot.snapshot);setStatus('');
       }else if(frame.kind==='checked'){
        if(!slot.snapshot||slot.snapshot.snapshot_sha256!==frame.digest)throw Error('核对不匹配。');
       }else if(frame.kind==='reset'){throw Error('结果需重新核对。');}
       else if(frame.kind==='use-snapshot'){slot.fail(t);setSnapshot(null);setStatus('正在核对…');streamAvailable=false;}
       else {slot.fail(t);setSnapshot(null);setStatus('正在核对…');}
      });
      if(streamAvailable){cadence=250;return;}
     }catch(e){
      // An older Host or a saturated stream lease may use the same freshly
      // authorized snapshot. Denial, bad binding and malformed events never fall back.
      if(!(e instanceof ApiError && [404,405,429].includes(e.status)))throw e;
      streamAvailable=false;
      if(e.status===429)setWatchLimited(true);
     }finally{clearTimeout(watchdog);}
    }
    const data=await controller.readRunResults(root,run,a.signal);
    if(alive&&!a.signal.aborted&&slot.accept(t,data)&&slot.snapshot){
     const next=slot.snapshot;
     if(purchaseChoices.current){
      setPurchaseEpoch(n=>n+1);
      if(next.state!=='available'||purchaseChoices.current.digest!==next.snapshot_sha256||
        !canOpenPurchaseQuantityComparison(hasCapability))purchaseChoices.current=null;
     }
     const hadSupplement=remainderMemory.hasReading;
     if(hadSupplement||remainderMemory.hasInterrupted){
      const current=()=>alive&&job===a&&!a.signal.aborted&&!hidden()&&
       !!panel.current&&!panel.current.closest('[hidden]')&&controller.isCurrent()&&
       controller.getSnapshot().selectedId===root&&controller.getSnapshot().conversationLoaded;
      const kept=new Set<string>();
      for(const {origin,anchor,reading} of remainderMemory.candidates(next)){
       const proof=reading.progress.archiveProof;
       if(!current()||!proof)break;
       try{
        const checked=await controller.checkProductBarcodeArchive(root,run,origin,anchor.block_id,a.signal);
        if(current()&&matchesProductBarcodeArchiveCheck(checked,{workId:root,runId:run,anchor,
          recordedAt:reading.progress.recordedAt,archiveProof:proof}))kept.add(origin);
       }catch{/* No recovery or content fetch if this source cannot be reauthorized. */}
      }
      if(!current()){remainderMemory.clear();return;}
      remainderMemory.keepOnly(kept);
      setRemainderEpoch(i=>i+1);
      setRemainderNotice(hadSupplement&&!kept.size?'先前补核的内容暂不能重新验证，已隐藏。':'');
     }
     needsFreshRead=false;setRemainderVerifiedSnapshot(next);setSnapshot(next);setStatus('');setConnection('current');
    }
   }catch(error){
    if(alive&&!a.signal.aborted){
     purchaseChoices.current=null;
     remainderMemory.clear();setRemainderNotice('');
     slot.fail(t);
     if(recovery&&controller.isCurrent()&&retryableReadbackDisconnect(error)){setConnection('not_reached');setStatus('');}
     else{
      needsFreshRead=false;setSnapshot(null);setConnection('current');
      setStatus(recovery&&readbackNoLongerAccessible(error)
       ?'当前无法访问原结果，已隐藏上次核对的内容。'
       :'暂时无法核对业务结果。当前未能核验来源，不能据此判断业务数量。');
     }
    }
   }finally{
    clearTimeout(watchdog);
    if(alive && job===a){job=null;if(!hidden())timer=setTimeout(()=>void read(true),cadence);}
   }
  }
  function visibility(){if(hidden())reset();else if(!job)void read();}
  function network(){
   if(hidden())return;
   if(browserOffline())offline();
   else if(wasOffline&&!job)void read();
  }
  reset();
  // Repeated taps join the current permission-checked read.
  manual.current=()=>{if(!browserOffline()&&(!job||job.signal.aborted||slot.snapshot))void read();};
  if(open&&!permit?.aborted){
   void read();document.addEventListener('visibilitychange',visibility);
   window.addEventListener('offline',network);window.addEventListener('online',network);
  }
  return ()=>{
   alive=false;manual.current=null;stopRead();
   document.removeEventListener('visibilitychange',visibility);
   window.removeEventListener('offline',network);window.removeEventListener('online',network);
  };
 },[controller,root,run,open,running,followLive,executionStatus,permit]);
 useEffect(()=>{setChoiceStatus('');return()=>{confirm.current?.abort();confirm.current=null;};},[controller,root,run,open,latest,executionStatus,permit]);
 async function choose(ref:ChoiceRef,stillSelected?:()=>boolean){
  if(permit?.aborted||browserOffline()||connection!=='current'||!latest||!open||!onDraft||(ref.kind==='purchase_receipt_detail'&&!canDraftReceiptDetail(hasCapability)))return;
  // Capture this draft and its original control before waiting for the one authorized read.
  const stageDraft=onDraft();
  confirm.current?.abort();
  const a=new AbortController();confirm.current=a;setChoiceStatus('正在重新核对来源…');
  try {
   const raw=await controller.readRunResults(root,run,a.signal);
   if(permit?.aborted||a.signal.aborted||document.visibilityState==='hidden'||browserOffline())return;
   const fresh=parseResultSnapshot(raw,{workId:root,runId:run,executionStatus});
   if(ref.kind==='purchase_receipt_detail'&&!canDraftReceiptDetail(hasCapability)){
    setSnapshot(null);setChoiceStatus('当前权限已变化，未加入草稿。');return;
   }
   const text=verifiedChoiceDraft(fresh,ref);
   if(!text){
    setSnapshot(ref.kind==='purchase_receipt_detail'?fresh:null);
    setChoiceStatus('这份结果已变化，未加入草稿。');
    if(ref.kind!=='purchase_receipt_detail')manual.current?.();
    return;
   }
   // The authorized read can complete after the person changes the picker.
   if(stillSelected&&!stillSelected()){
    setChoiceStatus('已更换选择，先前的单据未加入草稿。确认当前行后可再次添加。');return;
   }
   const canBind=ref.kind==='purchase_receipt_detail'&&controller.supportsDraftSourceRow();
   const source=canBind&&ref.kind==='purchase_receipt_detail'?selectedPurchaseSource(fresh,ref):null;
   if(canBind&&!source){setChoiceStatus('原单引用无法核实，未加入草稿。');return;}
   const unboundReceipt=ref.kind==='purchase_receipt_detail'&&!source;
   const staged=stageDraft(text,source??undefined,unboundReceipt?'unbound_purchase_receipt':undefined);
   setChoiceStatus(staged==='editor_changed'?'编辑器正在使用或草稿已变化，这次未加入草稿。完成编辑后可在来源页再次选择。':
    staged==='unavailable'?'当前工作不能追加，或者已有另一条原单引用，未加入草稿。':
    unboundReceipt?'已加入可编辑草稿；当前入口尚不能绑定原单，请查看编辑框旁提示。':
    '已加入当前追问草稿，请检查后手动发送。');

  }catch{if(!permit?.aborted&&!a.signal.aborted){setSnapshot(null);setChoiceStatus('未能重新确认来源，未加入草稿。');}}
 }
 // A status change must not flash a card from a different execution phase.
 const outOfPhase = snapshot?.blocks.some(block=>isPartialObservation(block)?executionStatus!=='failed':
   (block.type==='operational_receipt_page'||block.type==='operational_receipt_detail'||isTransferBlock(block))?!allowsOperationalReceipts(executionStatus):
   block.type==='product_barcode_page'?executionStatus!=='completed'&&executionStatus!=='failed':
   (block.type==='customer_receivables_detail'||block.type==='purchase_order_detail')&&executionStatus!=='completed');
 const visible = outOfPhase ? null : snapshot;
 const connectionHint=readbackConnectionHint(connection,visible?.state==='available'&&visible.blocks.length>0);
 const sourceState:SourceRowVisibility['state']=!open?'closed':connection!=='current'||!visible?'checking':visible.state==='available'?'available':'unavailable';
 const verifiedSourceDigest=sourceState==='available'?visible?.snapshot_sha256??null:null;
 useLayoutEffect(()=>{
  onSourceVisibility?.({rootWorkId:root,runId:run,state:sourceState,snapshotSha256:verifiedSourceDigest});
 },[onSourceVisibility,root,run,sourceState,verifiedSourceDigest]);
 const pendingSnapshot=running&&visible&&visible.state!=='withheld'?visible:null;
 const rootReceipt=controller.getSnapshot().conversation?.[0];
 const savedStockSteps=executionStatus==='completed'&&rootReceipt?.work_id===root&&rootReceipt.run_id===run
  ?rootReceipt.progress.savedStockObservationSteps?.filter(step=>canReadSavedStockStep(step,hasCapability))??[]:[];
 const savedStockLossSteps=executionStatus==='completed'&&rootReceipt?.work_id===root&&rootReceipt.run_id===run&&
  canReadSavedStockLoss(hasCapability)?rootReceipt.progress.savedStockLossSteps??[]:[];
 const savedWarehouseDirectorySteps=executionStatus==='completed'&&rootReceipt?.work_id===root&&rootReceipt.run_id===run&&
  canReadSavedWarehouseDirectory(hasCapability)?rootReceipt.progress.savedWarehouseDirectorySteps??[]:[];
 const savedLegacyProductSteps=executionStatus==='completed'&&rootReceipt?.work_id===root&&rootReceipt.run_id===run&&
  canReadSavedLegacyProduct(hasCapability)?rootReceipt.progress.savedLegacyProductSteps??[]:[];
 const canFindStoredSales=mayReadStoredSalesCoverage(hasCapability)&&rootReceipt?.work_id===root&&rootReceipt.run_id===run&&
  (executionStatus==='completed'||executionStatus==='failed')&&rootReceipt.progress.hasStoredSalesCoverageCandidate===true;
 const canComparePurchases=connection==='current'&&(executionStatus==='completed'||executionStatus==='failed')&&
  canOpenPurchaseQuantityComparison(hasCapability)&&visible?.state==='available'&&
  visible.blocks.some(block=>block.type==='purchase_order_detail'||
   (block.type==='operational_receipt_detail'&&block.action==='purchase_receipt_detail'));
 const exportBoundary=JSON.stringify([root,run,executionStatus]);
 const readyExportKey=open&&visible&&hasExportableResult(visible)
   ?JSON.stringify([exportBoundary,visible.snapshot_sha256]):null;
 const [exportAnchor,setExportAnchor]=useState<{boundary:string;key:string}|null>(null);
 // Retain the control's identity only during a same-phase check. It stores no
 // row, answer, or result snapshot; the old data remains hidden below.
 const preserveExport=open&&!visible&&status==='正在核对…'&&exportAnchor?.boundary===exportBoundary;
 const exportKey=readyExportKey??(preserveExport?exportAnchor?.key:null);
 useLayoutEffect(()=>{
  if(readyExportKey)setExportAnchor(old=>old?.key===readyExportKey?old:{boundary:exportBoundary,key:readyExportKey});
  else if(!preserveExport)setExportAnchor(null);
 },[readyExportKey,exportBoundary,preserveExport]);
 async function readTableExport(signal:AbortSignal){
  if(browserOffline()||connection!=='current'||!visible||!hasExportableResult(visible))throw Error('结果目前不可重新核对。');
  let fresh:ResultSnapshot;
  try{fresh=parseResultSnapshot(await controller.readRunResults(root,run,signal),{workId:root,runId:run,executionStatus});}
  catch(error){
   if(!signal.aborted&&(!browserOffline()||readbackNoLongerAccessible(error))){
    setSnapshot(null);setStatus('未能重新核对。没有复制或下载，旧结果已隐藏。');
   }
   throw error;
  }
  if(signal.aborted||browserOffline())throw Error('选择已改变，或无法继续联网核对。');
  if(fresh.state!=='available'||fresh.snapshot_sha256!==visible.snapshot_sha256){
   setSnapshot(fresh);setStatus('已保存结果有变化，本次没有复制或下载。');
   throw Error('结果已经变化，请重新核对。');
  }
  return savedResultText(fresh);
 }
 return <div ref={panel}>{open&&<><button type="button" className="ar-read" aria-disabled={status==='正在核对…'||connection==='offline'||connection==='checking'||undefined} onClick={()=>manual.current?.()}>核对本次业务结果</button>{' '}</>}
   <button type="button" className="ar-read" aria-expanded={open} aria-controls={panelId} onClick={toggle}>{open?'收起结果':'查看本次业务结果'}</button>
   <div id={panelId} hidden={!open}>{open&&<>
   {connectionHint&&<p role="status" className="assistant-notice">{connectionHint}</p>}
   {status&&<p role="status">{status==='正在核对…'&&remainderMemory.hasInterrupted?'正在定期核对查看权限，尚未完成的补核已暂停。':
    status==='正在核对…'&&remainderMemory.hasReading?'正在重新核对查看权限。确认无变化后会恢复上次阅读。':status}</p>}
   {!status&&remainderNotice&&<p role="status">{remainderNotice}</p>}{choiceStatus&&<p role="status">{choiceStatus}</p>}
   {running&&watchLimited&&<p className="assistant-notice">进度连接暂忙，改为读取已保存结果，不会重新执行。</p>}
   <ReadbackIntentNotice outcomeUnknown={outcomeUnknown}/>
   <SourceReadIssueNotice status={executionStatus} snapshot={visible} issues={sourceReadIssues}/>
   <FailedReportNotice status={executionStatus} snapshot={visible} savedAnswer={savedAnswer}/>
   {pendingSnapshot&&<ActiveReportNotice snapshot={pendingSnapshot}/>}
   <VerifiedSourceDirectory snapshot={visible} prefix={sourceAnchorPrefix}/>
   {canComparePurchases&&visible&&<PurchaseQuantityComparisonPane key={JSON.stringify([visible.snapshot_sha256,purchaseEpoch,visible===remainderVerifiedSnapshot])}
    workId={root} runId={run}
    initialChoices={visible===remainderVerifiedSnapshot&&purchaseChoices.current?.digest===visible.snapshot_sha256?purchaseChoices.current.value:undefined}
    remember={value=>{purchaseChoices.current=value?{digest:visible.snapshot_sha256,value}:null;}}
    read={(selection,signal)=>controller.readPurchaseQuantityComparison(root,run,selection,signal)}/>}
   {canFindStoredSales&&<LocalSalesCoveragePanel key={JSON.stringify([root,run])} workId={root} runId={run}
    ready={connection==='current'&&visible!==null&&visible.state!=='withheld'}
    read={(step,signal)=>controller.readStoredSalesCoverage(root,run,step,signal)}/>}
   {canFindStoredSales&&<LocalSalesSavedRankingPanel key={JSON.stringify(['ranking',root,run])} workId={root} runId={run}
    ready={connection==='current'&&visible!==null&&visible.state!=='withheld'}
    read={(step,signal)=>controller.readStoredSalesRanking(root,run,step,signal)}/>}
   {savedStockSteps.length>0&&<SavedStockObservationCard key={JSON.stringify(['stock',root,run])} workId={root} runId={run}
    ready={connection==='current'&&visible!==null&&visible.state!=='withheld'} steps={savedStockSteps}
    read={(stepId,signal)=>controller.readSavedStockObservation(root,run,stepId,signal)}/>}
   {savedStockLossSteps.length>0&&<SavedStockLossDocumentsCard key={JSON.stringify(['stock-loss',root,run])} workId={root} runId={run}
    ready={connection==='current'&&visible!==null&&visible.state!=='withheld'} steps={savedStockLossSteps}
    read={(stepId,signal)=>controller.readSavedStockLossPage(root,run,stepId,signal)}/>}
   {savedWarehouseDirectorySteps.length>0&&<SavedWarehouseDirectoryCard key={JSON.stringify(['warehouse-directory',root,run])} workId={root} runId={run}
    ready={connection==='current'&&visible!==null&&visible.state!=='withheld'} steps={savedWarehouseDirectorySteps}
    read={(stepId,signal)=>controller.readSavedWarehouseDirectoryPage(root,run,stepId,signal)}/>}
   {savedLegacyProductSteps.length>0&&<SavedLegacyProductCard key={JSON.stringify(['legacy-product',root,run])} workId={root} runId={run}
    ready={connection==='current'&&visible!==null&&visible.state!=='withheld'} steps={savedLegacyProductSteps}
    read={(stepId,signal)=>controller.readSavedLegacyProduct(root,run,stepId,signal)}/>}
   {exportKey&&<LocalExportControls
    scopeKey={exportKey} available={readyExportKey!==null}
    label="已保存来源表" filename="V4-已保存来源表.txt" readFresh={readTableExport}/>}
   {imageEvidenceKey&&<ProductResultImageSection workId={root} runId={run} executionStatus={executionStatus} baseUrl={imageBaseUrl} evidenceKey={imageEvidenceKey}/>}
   {(!pendingSnapshot||pendingSnapshot.blocks.length>0)&&<PublicResultTables snapshot={visible} canChooseReceiptDetail={canDraftReceiptDetail(hasCapability)} liveReading={followLive}
    readProductRemainder={connection==='current'?(origin,after,signal)=>controller.readProductBarcodeRemainder(root,run,origin,after,signal):undefined}
    productRemainderMemory={visible===remainderVerifiedSnapshot?remainderMemory:undefined} productRemainderEpoch={remainderEpoch}
    sourceAnchorPrefix={needsVerifiedSourceDirectory(visible)?sourceAnchorPrefix:undefined} rechecking={status==='正在核对…'} savedAnswer={savedAnswer} onChoose={latest&&onDraft?(ref,stillSelected)=>void choose(ref,stillSelected):undefined}/>}</>}</div></div>;
}
