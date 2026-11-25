import {ProductBarcodeRemainderReader,type ReadProductBarcodeRemainder} from './ProductBarcodeRemainderReader';
import {canReadProductRemainder} from './productBarcodePage';
import type {ProductBarcodeRemainderMemory} from './productBarcodeRemainderMemory';
import {ProductBarcodePageCard} from './ProductBarcodePageCard';
import {BusinessChoiceCards} from './BusinessChoiceCards';
import {PartialStockCard} from './PartialStockCard';
import {PartialEndpointCard} from './PartialEndpointCard';
import {OperationalTransferCard} from './OperationalTransferCard';
import {isTransferBlock} from './operationalTransfer';
import {CustomerReceivablesCard} from './CustomerReceivablesCard';
import {PurchaseOrderDetailCard} from './PurchaseOrderDetailCard';
import {OperationalReceiptCard} from './OperationalReceiptCard';
import type {OperationalReceiptPage} from './operationalReceiptPage';
import {verifiedSourceAnchor,verifiedSourceHeading} from './VerifiedSourceDirectory';
import {OperationalReceiptDetailCard} from './OperationalReceiptDetailCard';
import {initialOperationalDetailView,type OperationalDetailView} from './operationalReceiptDetailView';
import {sourceRowChoice,type ChoiceRef} from './businessChoice';
import {useEffect,useId,useLayoutEffect,useRef,useState,type KeyboardEvent,type RefObject} from 'react';
import type {ResultSnapshot,ResultTable,Scalar} from './publicResultSnapshot';
import {publicResultDays,sourceBusinessDay} from './publicResultDays';
import {filterResultRows} from './resultRowFilter';
import {readResultFilter,reconcileResultFilters,rememberResultFilter,type ResultFilterMemory} from './resultFilterMemory';
import {LiveResultFilterReading} from './LiveResultFilterReading';
import {liveFilterWitness,type LiveFilterWitness} from './liveResultFilterFocus';
import {AssistantLiveSourceReading} from './AssistantLiveSourceReading';
import './public-results.css';
import './sales-result-navigation.css';

function cell(value:Scalar) { return value === null ? '未提供' : typeof value === 'boolean' ? (value ? '是' : '否') : String(value); }
function sourcePageExtent(coverage:ResultTable['coverage']):string {
 const end=coverage.offset+coverage.row_count;
 if(end>coverage.reported_total)return '本页位置超出来源所报范围，不能据此判断查全。';
 if(coverage.reported_total===0)return '来源所报 0 行仅对应这次查询范围。';
 if(coverage.page_exhausted)return '本页已到来源所报末尾，末页本身不证明查全。';
 return '本页尚未到来源所报末尾。';
}
type Mode = 'rows'|'chart'|'source'|'next';
const modeNames = {rows:'明细表',chart:'日期分布',source:'数据依据',next:'下一步'};

type Choice = {mode:Mode;dayIndex:number};
type BlockChoices={scope:string;byBlock:Map<string,Choice>};
type DetailViews={scope:string;byBlock:Map<string,OperationalDetailView>};
type FilterSource=Pick<ResultSnapshot,'work_id'|'run_id'|'snapshot_sha256'|'state'>;
type FilterIdentity={key:string;source:FilterSource;blockId:string;witness:LiveFilterWitness};
type FocusedFilter=FilterIdentity&{element:HTMLInputElement};
type SalesPageJump={number:number;id:string;back:()=>void};
type SalesSearch={scope:string;query:string};
type TableOffsets={visibleScope:string|null;savedScope:string|null;positions:Map<string,number>};
const isSalesPage=(block:ResultSnapshot['blocks'][number]):block is ResultTable=>
 block.type==='report_table'&&(block.source.action==='sales_lines'||block.source.action==='sales_query');

function jumpToSalesPage(id:string) {
 const target=document.getElementById(id);
 if(!target)return;
 target.focus({preventScroll:true});
 target.scrollIntoView({block:'start'});
}
const rowsChoice:Choice={mode:'rows',dayIndex:-1};
const cardKey=(digest:string,id:string)=>digest+':'+id;
function ResultCard({block,choice,choose,digest,onChoose,query,setQuery,filterRef,focusFilter,blurFilter,salesPage,matchedSalesRows,tableOffsetScope,tableOffsets}:{
 block:ResultTable|null;digest:string;onChoose?:(ref:ChoiceRef)=>void;choice:Choice;choose:(c:Choice)=>void;
 query:string;setQuery:(text:string)=>void;filterRef:(node:HTMLInputElement|null)=>void;
 focusFilter:(node:HTMLInputElement)=>void;blurFilter:(node:HTMLInputElement)=>void;salesPage?:SalesPageJump;
 matchedSalesRows?:ResultTable['rows'];
 tableOffsetScope?:string;tableOffsets?:RefObject<TableOffsets>;
}) {
 const id = useId(), mode=choice.mode;
 const titleId=salesPage?.id??id+'-table-title',noteId=id+'-table-source';
 const days = block?publicResultDays(block):[], dateCol = block?.columns.findIndex(c => c.key === 'bill_date')??-1;
 const selectedDay = days[choice.dayIndex]?.day ?? '';
 const setMode=(m:Mode)=>choose({...choice,mode:m});
 const scopeRows=matchedSalesRows??block?.rows??[];
 const dayRows=selectedDay?scopeRows.filter(r=>sourceBusinessDay(r.cells[dateCol])===selectedDay):scopeRows;
 const rows=matchedSalesRows?dayRows:filterResultRows(dayRows,query);
 const modes:Mode[] = days.length ? ['rows','chart','source'] : ['rows','source'];
 if(block?.rows.some((_row,i)=>sourceRowChoice(block,i)!==null))modes.splice(modes.length-1,0,'next');
 const visibleMode = block&&modes.includes(mode) ? mode : 'rows';
 const max = days.reduce((acc,x) => Math.max(acc,x.count),1);
 const tableViewport=useRef<HTMLDivElement>(null);
 const tableBlockId=block?.block_id,showTable=!!block&&visibleMode==='rows';
 useLayoutEffect(()=>{
  const viewport=tableViewport.current,memory=tableOffsets?.current;
  if(!showTable||!viewport||!memory||memory.savedScope!==tableOffsetScope||tableBlockId===undefined)return;
  const offset=memory.positions.get(tableBlockId);
  if(offset!==undefined)viewport.scrollLeft=offset;
 },[showTable,tableBlockId,tableOffsetScope,tableOffsets]);
 function rememberTableOffset(offset:number){
  const memory=tableOffsets?.current;
  if(!memory||!tableOffsetScope||memory.visibleScope!==tableOffsetScope||tableBlockId===undefined)return;
  if(offset===0)memory.positions.delete(tableBlockId);
  else memory.positions.set(tableBlockId,offset);
 }
 function tabKeys(event:KeyboardEvent<HTMLButtonElement>) {
   const i=modes.indexOf(visibleMode);
   const next = event.key === 'ArrowRight' ? (i+1)%modes.length : event.key === 'ArrowLeft' ? (i+modes.length-1)%modes.length : event.key === 'Home' ? 0 : event.key === 'End' ? modes.length-1 : -1;
   if(next<0)return;event.preventDefault();setMode(modes[next]);document.getElementById(id+'-'+modes[next])?.focus();
 }
 return <article className="ar-card" aria-busy={!block||undefined}>
  <header>{block?<><span className="ar-kicker">业务结果 · 管家婆</span><h3 id={titleId}
   className={salesPage?'ar-sales-jump-target':undefined} tabIndex={salesPage?-1:undefined}
   data-sales-page={salesPage?.number}>{block.title}</h3>
  <p>本页 {block.coverage.row_count} 行{block.coverage.row_count>0&&
   `（查询位置第 ${block.coverage.offset+1}–${block.coverage.offset+block.coverage.row_count} 行）`}，来源报告共 {block.coverage.reported_total} 行；不是完整报表。{block.coverage.bill_state_filter===12?' 仅已过账明细。':''} {sourcePageExtent(block.coverage)}</p>
  <p id={noteId}>{block.notice}</p></>
  : <p>正在重新核对。你可以继续输入，明细会在核对通过后显示。</p>}</header>
  {block&&<div role="tablist" aria-label={block.title+'查看方式'} className="ar-tabs">{modes.map(m => <button type="button" role="tab" id={id+'-'+m} key={m} aria-selected={visibleMode===m} aria-controls={id+'-panel'} tabIndex={visibleMode===m?0:-1} onKeyDown={tabKeys} onClick={()=>setMode(m)}>{modeNames[m]}</button>)}</div>}
  <div role={block?'tabpanel':undefined} id={id+'-panel'} aria-labelledby={block?id+'-'+visibleMode:undefined} tabIndex={block?0:undefined} className="ar-panel">
  {visibleMode==='rows' ? <>
   {block&&matchedSalesRows?<p role="status">跨页查找在此页匹配 {matchedSalesRows.length} / {block.rows.length} 行{selectedDay?`，其中所选日期 ${rows.length} 行`:''}。</p>
   :<div className="ar-search"><label htmlFor={id+'-search'}>查找本页明细</label><input ref={filterRef} id={id+'-search'} type="search" value={query} maxLength={200} placeholder="商品、规格、单号；空格分隔多个条件" onChange={e=>setQuery(e.target.value)} onFocus={e=>focusFilter(e.currentTarget)} onBlur={e=>blurFilter(e.currentTarget)}/>{block&&query&&<button type="button" onClick={()=>setQuery('')}>清除查找</button>}{block&&<p role="status">显示 {rows.length} / {dayRows.length} 行，仅筛选已加载本页{selectedDay?'所选日期':''}，不会查询其他页。</p>}</div>}
   {block&&<>{selectedDay && <div className="ar-filter">{selectedDay} · {dayRows.length} 行 <button type="button" onClick={()=>choose({mode:'rows',dayIndex:-1})}>显示本页全部</button></div>}
   <p className="ar-sales-table-scroll-hint">长表可在表内上下滑动，左右滑动查看其他列。</p>
   <div ref={tableViewport} onScroll={event=>rememberTableOffset(event.currentTarget.scrollLeft)}
    className="ar-table-scroll ar-sales-table-scroll" tabIndex={0} role="region" aria-label={block.title+'数据表'}>
   <table aria-labelledby={titleId} aria-describedby={noteId}><thead><tr>{block.columns.map(c=><th key={c.key} scope="col">{c.label}</th>)}</tr></thead>
   <tbody>{rows.map(r=><tr key={r.row_id}>{r.cells.map((v,i)=><td key={block.columns[i].key}>{cell(v)}</td>)}</tr>)}</tbody></table></div>
   {!rows.length&&block.rows.length>0&&<p>{matchedSalesRows
    ? '本页没有符合当前查找条件的行。可返回销售结果目录调整关键词，不代表其他范围没有业务。'
    : '本页筛选未匹配到明细，不代表其他页或其他日期没有业务。可清除查找或取消日期筛选。'}</p>}
   {!block.rows.length&&<p>已核验的来源在这一页返回 0 行；仅代表本次查询范围，不表示其他范围没有业务。</p>}</>}
  </> : block&&(visibleMode==='chart' ? <>
   <h4>本页记录按日期分布</h4><p>每条表示该日返回的行数，不是销售额或单据数。选择日期可查看对应明细。</p>
   <ul className="ar-bands">{days.map((b,i)=><li key={b.day}><button type="button" aria-label={b.day+'：'+b.count+' 行，查看明细'} onClick={()=>{choose({mode:'rows',dayIndex:i});document.getElementById(id+'-rows')?.focus();}}>
    <span>{b.day}</span><span className="ar-track" aria-hidden="true"><i style={{width:(100*b.count/max)+'%'}}/></span><b>{b.count} 行</b>
   </button></li>)}</ul>
  </> : visibleMode==='next' ? <BusinessChoiceCards block={block} digest={digest} onChoose={onChoose}/> : <dl className="ar-facts"><dt>来源</dt><dd>管家婆 · {block.source.action==='sales_lines'?'销售明细':'销售单据'}</dd><dt>记录时间</dt><dd><time dateTime={block.source.recorded_at}>{block.source.recorded_at}</time></dd><dt>数据范围</dt><dd>来源偏移 {block.coverage.offset}，本页 {block.coverage.row_count} 行。</dd></dl>)}
  {salesPage&&<button type="button" className="ar-sales-page-back" onClick={salesPage.back}>回到销售结果目录</button>}
  </div></article>;
}

function SalesDirectory({id,query,hasQuery,onEdit,onFocus,onSearchPage,pages,pageByBlock,matched}:{id:string;query:string;hasQuery:boolean;
 onEdit:(query:string)=>void;onFocus?:()=>void;onSearchPage:(blockId:string)=>void;pages:ResultTable[]|null;
 pageByBlock?:Map<string,SalesPageJump>;matched?:Map<string,ResultTable['rows']>;
}){
 const searchId=id+'-search',helpId=id+'-help';
 return <nav id={id} tabIndex={-1} aria-label="已核对的销售结果" className="ar-sales-pages ar-sales-jump-target">
  <h3>已核对的销售结果</h3>
  <p id={helpId}>只查找本次已保存的销售来源页，不会查询其他页，也不代表查全。下载仍包含全部已保存来源表。</p>
  <div className="ar-sales-search"><label htmlFor={searchId}>查找已保存销售来源页</label>
   <input id={searchId} type="search" maxLength={200} value={query} placeholder="例如商品名或单号"
     aria-describedby={helpId} onFocus={onFocus} onChange={e=>onEdit(e.target.value)}/>
   {query&&<button type="button" className="ar-sales-search-clear" onClick={()=>{onEdit('');document.getElementById(searchId)?.focus();}}>清除跨页查找</button>}
  </div>
  {pages?<>{hasQuery&&<p role="status">{matched&&[...matched.values()].some(rows=>rows.length>0)
    ? '各来源页的匹配记录如下，来源之间不合并。'
    : '本次已保存的销售来源页均未匹配，不代表其他查询范围没有业务。'}</p>}
   <ul>{pages.map(page=>{
    const jump=pageByBlock?.get(page.block_id);
    if(!jump)return null;
    return <li key={page.block_id}><button type="button" onClick={()=>{if(hasQuery)onSearchPage(page.block_id);jumpToSalesPage(jump.id);}}>
      结果 {jump.number} · {page.source.action==='sales_query'?'销售单据':page.coverage.bill_state_filter===12?'已过账销售明细':'销售明细'}
      {' · '}偏移 {page.coverage.offset} · {page.coverage.row_count} 行
      {hasQuery&&` · 匹配 ${matched?.get(page.block_id)?.length??0} 行`}
    </button></li>;
   })}</ul>
  </>:<p role="status">正在重新核对，暂不显示来源页和匹配结果。</p>}
 </nav>;
}

export type SavedAnswerPresentation = 'answer' | 'answer-with-evidence';

export function PublicResultTables({snapshot,onChoose,savedAnswer,rechecking=false,liveReading=false,sourceAnchorPrefix,canChooseReceiptDetail=false,readProductRemainder,productRemainderMemory,productRemainderEpoch=0}:{
 snapshot:ResultSnapshot|null;onChoose?:(ref:ChoiceRef,stillSelected?:()=>boolean)=>void;savedAnswer?:SavedAnswerPresentation;rechecking?:boolean;liveReading?:boolean;sourceAnchorPrefix?:string;canChooseReceiptDetail?:boolean;readProductRemainder?:ReadProductBarcodeRemainder;productRemainderMemory?:ProductBarcodeRemainderMemory;productRemainderEpoch?:number;
}) {
 // Retain presentation preferences, never row values, while fresh data is checked.
 const salesDirectoryId=useId()+'-sales-pages';
 const [blockChoices,setBlockChoices]=useState<BlockChoices|null>(null);
 const [detailViews,setDetailViews]=useState<DetailViews|null>(null);
 const [receiptPicks,setReceiptPicks]=useState<{scope:string;byBlock:Map<string,number>}|null>(null);
 const [filters,setFilters]=useState<ResultFilterMemory>(null);
 const [focused,setFocused]=useState<FocusedFilter|null>(null);
 const filterInputs=useRef(new Map<string,HTMLInputElement>());
 const [salesSearch,setSalesSearch]=useState<SalesSearch|null>(null);
 const tableOffsets=useRef<TableOffsets>({visibleScope:null,savedScope:null,positions:new Map()});
 const tableOffsetScope=snapshot?.state==='available'
  ? JSON.stringify([snapshot.work_id,snapshot.run_id,snapshot.snapshot_sha256]):null;
 useLayoutEffect(()=>{
  // Pixels are presentation state only. Keep them across an in-flight read, never
  // restore them into a denied, new or no-longer-visible result.
  const memory=tableOffsets.current;
  memory.visibleScope=tableOffsetScope;
  if(!canChooseReceiptDetail)setReceiptPicks(null);
  if(!snapshot&&rechecking)return;
  // Only keep the user's row position, never its source fields or a pending action.
  if(canChooseReceiptDetail)setReceiptPicks(previous=>previous?.scope===tableOffsetScope?previous:null);
  if(tableOffsetScope!==memory.savedScope){
   memory.positions.clear();
   memory.savedScope=tableOffsetScope;
  }
  setBlockChoices(previous=>previous?.scope===tableOffsetScope?previous:null);
  // Only view controls survive an in-flight recheck of the exact same authorized snapshot.
  setDetailViews(previous=>previous?.scope===tableOffsetScope?previous:null);
  setSalesSearch(previous=>previous?.scope===tableOffsetScope&&snapshot&&snapshot.blocks.filter(isSalesPage).length>1?previous:null);
 },[snapshot,rechecking,tableOffsetScope,canChooseReceiptDetail]);
 function chooseFor(blockId:string,choice:Choice) {
  if(!tableOffsetScope)return;
  setBlockChoices(previous=>{
   const byBlock=new Map(previous?.scope===tableOffsetScope?previous.byBlock:undefined);
   byBlock.set(blockId,choice);
   return {scope:tableOffsetScope,byBlock};
  });
 }
 function pickReceipt(block:OperationalReceiptPage,index:number){
  if(!tableOffsetScope||!canChooseReceiptDetail||block.action!=='purchase_receipts_query'||
    !Number.isSafeInteger(index)||index<-1||index>=block.items.length)return;
  setReceiptPicks(previous=>{
   const byBlock=new Map(previous?.scope===tableOffsetScope?previous.byBlock:undefined);
   if(index<0)byBlock.delete(block.block_id);
   else byBlock.set(block.block_id,index);
   return byBlock.size?{scope:tableOffsetScope,byBlock}:null;
  });
 }
 function editDetail(blockId:string,view:OperationalDetailView) {
  if(!tableOffsetScope)return;
  setDetailViews(previous=>{
   const byBlock=new Map(previous?.scope===tableOffsetScope?previous.byBlock:undefined);
   byBlock.set(blockId,view);
   return {scope:tableOffsetScope,byBlock};
  });
 }
 useEffect(()=>{setFilters(previous=>reconcileResultFilters(previous,snapshot));},[snapshot]);
 useEffect(()=>{
  // Only same current acknowledged result may keep the editor. Pending keeps
  // the user's own text, never any previous table, source or summary.
  if(!snapshot&&rechecking)return;
  setFocused(previous=>{
   if(!previous||snapshot?.state!=='available')return null;
   return previous.source.work_id===snapshot.work_id&&previous.source.run_id===snapshot.run_id&&
    previous.source.snapshot_sha256===snapshot.snapshot_sha256&&
    snapshot.blocks.some(b=>b.type==='report_table'&&b.block_id===previous.blockId)?previous:null;
  });
 },[snapshot,rechecking]);
 function edit(identity:FilterIdentity){
  const {key,source,blockId}=identity;
  return {query:readResultFilter(filters,source,blockId),
   setQuery:(text:string)=>setFilters(previous=>rememberResultFilter(previous,source,blockId,text)),
   filterRef:(node:HTMLInputElement|null)=>{if(node)filterInputs.current.set(key,node);else filterInputs.current.delete(key);},
   focusFilter:(element:HTMLInputElement)=>setFocused({...identity,element}),
   blurFilter:(element:HTMLInputElement)=>setFocused(previous=>previous?.element===element?null:previous)};
 }
 if(!snapshot){
  if(!rechecking||(!focused&&!salesSearch))return null;
  return <section aria-label="业务查询结果" className="ar-results">{[
   salesSearch&&<SalesDirectory key="sales-directory" id={salesDirectoryId} query={salesSearch.query} hasQuery={false} pages={null}
    onEdit={query=>setSalesSearch(previous=>previous?{...previous,query}:null)} onSearchPage={()=>{}}/>,
   focused&&<ResultCard key={focused.key} block={null} digest={focused.source.snapshot_sha256}
    choice={rowsChoice} choose={()=>{}} {...edit(focused)}/>,
  ]}</section>;
 }
 if(snapshot.state==='withheld')return <p role="status">此结果目前不可查看。不能据此判断业务数量，也不等于查询返回 0 行。</p>;
 if(snapshot.state==='not_recorded'||!snapshot.blocks.length){
  return <p role="status">{savedAnswer
   ? `本次未单独提供可筛选的明细表，请查看上方已保存的答复${savedAnswer==='answer-with-evidence'?'和依据':''}。没有明细表不等于查询返回 0 行。`
   : '本次没有可展示的业务结果记录。尚不能判断是否查到数据，不等于查询返回 0 行。'}</p>;
 }
 // This is a jump list for the already-authorized snapshot, not a paginator or proof
 // that separate queries cover one period. Preserve server order and duplicate offsets.
 const salesPages=snapshot.blocks.filter(isSalesPage);
 const pageByBlock=new Map<string,SalesPageJump>();
 const crossQuery=salesSearch?.scope===tableOffsetScope?salesSearch?.query??'':'';
 const hasCrossQuery=!!crossQuery.trim();
 const matchedByBlock=new Map<string,ResultTable['rows']>();
 if(salesPages.length>1)salesPages.forEach((page,i)=>{
  pageByBlock.set(page.block_id,{number:i+1,id:salesDirectoryId+'-'+(i+1),back:()=>jumpToSalesPage(salesDirectoryId)});
  if(hasCrossQuery)matchedByBlock.set(page.block_id,filterResultRows(page.rows,crossQuery));
 });
 return <LiveResultFilterReading live={liveReading} snapshot={snapshot} focused={focused}
  lookup={(next,block)=>filterInputs.current.get(cardKey(next.snapshot_sha256,block.block_id))}>
 <AssistantLiveSourceReading live={liveReading} snapshot={snapshot}>{[
  pageByBlock.size>1&&<SalesDirectory key="sales-directory" id={salesDirectoryId} query={crossQuery} hasQuery={hasCrossQuery}
   pages={salesPages} pageByBlock={pageByBlock} matched={matchedByBlock}
   onEdit={query=>{if(tableOffsetScope)setSalesSearch({scope:tableOffsetScope,query});}}
   onFocus={()=>{if(tableOffsetScope)setSalesSearch(previous=>previous?.scope===tableOffsetScope?previous:{scope:tableOffsetScope,query:''});}}
   onSearchPage={blockId=>chooseFor(blockId,rowsChoice)}/>,
  ...snapshot.blocks.map((b,index)=>{const card=b.type==='partial_stock_observation'
  ? <PartialStockCard key={snapshot.snapshot_sha256+':'+b.block_id} block={b}/>
  : b.type==='partial_endpoint_observation' ? <PartialEndpointCard key={snapshot.snapshot_sha256+':'+b.block_id} block={b}/>
  : b.type==='customer_receivables_detail' ? <CustomerReceivablesCard key={snapshot.snapshot_sha256+':'+b.block_id} page={b}/>
  : b.type==='operational_receipt_detail' ? <OperationalReceiptDetailCard key={snapshot.snapshot_sha256+':'+b.block_id} page={b}
    view={(detailViews?.scope===tableOffsetScope?detailViews.byBlock.get(b.block_id):undefined)??initialOperationalDetailView}
    setView={view=>editDetail(b.block_id,view)}/>
  : b.type==='product_barcode_page' ? <ProductBarcodePageCard key={snapshot.snapshot_sha256+':'+b.block_id}
    page={b} hasSavedSuccessor={b.next_offset!==null&&b.next_offset<b.verified_through}
    remainder={snapshot.run_id&&readProductRemainder&&canReadProductRemainder(b)
      ? <ProductBarcodeRemainderReader key={JSON.stringify([productRemainderEpoch,!!productRemainderMemory])} anchor={b} workId={snapshot.work_id} runId={snapshot.run_id} read={readProductRemainder}
         initialReading={productRemainderMemory?.reading(snapshot.snapshot_sha256,b)}
         interrupted={productRemainderMemory?.wasInterrupted(snapshot.snapshot_sha256,b)}
         onReadingChange={reading=>productRemainderMemory?.activity(snapshot.snapshot_sha256,b,reading)}
         remember={value=>productRemainderMemory?.save(snapshot.snapshot_sha256,b,value)}/>
      : undefined}/>
  : b.type==='operational_receipt_page' ? <OperationalReceiptCard key={snapshot.snapshot_sha256+':'+b.block_id} page={b}
    digest={snapshot.snapshot_sha256} onChooseDetail={canChooseReceiptDetail?onChoose:undefined}
    picked={canChooseReceiptDetail&&receiptPicks?.scope===tableOffsetScope?receiptPicks.byBlock.get(b.block_id):undefined}
    onPick={index=>pickReceipt(b,index)}/>
  : isTransferBlock(b) ? <OperationalTransferCard key={snapshot.snapshot_sha256+':'+b.block_id} page={b}/>
  : b.type==='purchase_order_detail' ? <PurchaseOrderDetailCard key={snapshot.snapshot_sha256+':'+b.block_id} page={b}/>
  : <ResultCard key={cardKey(snapshot.snapshot_sha256,b.block_id)} block={b} digest={snapshot.snapshot_sha256} onChoose={onChoose}
    salesPage={pageByBlock.get(b.block_id)} matchedSalesRows={matchedByBlock.get(b.block_id)} tableOffsetScope={tableOffsetScope??undefined} tableOffsets={tableOffsets}
    {...edit({key:cardKey(snapshot.snapshot_sha256,b.block_id),blockId:b.block_id,witness:liveFilterWitness(b,index),
      source:{work_id:snapshot.work_id,run_id:snapshot.run_id,snapshot_sha256:snapshot.snapshot_sha256,state:snapshot.state}})}
    choice={(blockChoices?.scope===tableOffsetScope?blockChoices.byBlock.get(b.block_id):undefined)??rowsChoice}
    choose={choice=>chooseFor(b.block_id,choice)}/>;
   return sourceAnchorPrefix?<div id={verifiedSourceAnchor(sourceAnchorPrefix,index)} key={snapshot.snapshot_sha256+':'+b.block_id}
    className="ar-verified-source-anchor" role="group" aria-label={verifiedSourceHeading(b)} tabIndex={-1}>{card}</div>:card;
  }),
 ]}</AssistantLiveSourceReading></LiveResultFilterReading>;
}
