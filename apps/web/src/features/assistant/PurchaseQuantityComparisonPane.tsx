import {useEffect,useId,useLayoutEffect,useRef,useState,type ReactNode} from 'react';
import {usePath} from '../../app/router';
import {parsePurchaseQuantityPair,parsePurchaseQuantitySources,type PurchaseQuantityCandidate,type PurchaseQuantityDocument,
 type PurchaseQuantityPair,type PurchaseQuantitySource,type PurchaseQuantitySources,type PurchaseQuantityChoices} from './purchaseQuantityComparison';
import './purchase-quantity-comparison.css';

export type PurchaseQuantitySelection={orderStep:string;receiptStep:string};
export type ReadPurchaseQuantityComparison=(selection:PurchaseQuantitySelection|null,signal:AbortSignal)=>Promise<unknown>;
const emptySelection={order:'',receipt:''};
const clues:Record<PurchaseQuantityCandidate['state'],string>={
 same_reported_unit_name_only:'录入单位名称相同；未确认是同一个单位',
 reported_unit_names_differ:'录入单位名称不同；不要换算或相减',
 unit_not_reported:'一侧缺录入单位；不能比较数量',
 quantity_not_reported:'一侧缺原量；不按零计',
 multiple_source_rows:'同编码在来源里有多行；未配对或合计',
 only_in_selected_order:'只见于这张已选订单；不代表未入库',
 only_in_selected_receipt:'只见于这张已选入库单；不代表未订购',
};

function Windows<T>({values,label,draw}:{values:T[];label:string;draw:(part:T[])=>ReactNode}){
 const [page,select]=useState(0);
 const first=page*25,last=Math.min(first+25,values.length);
 return <>{draw(values.slice(first,last))}
  {values.length>25&&<div className="ar-purchase-quantity-nav" role="group" aria-label={label}>
   <button className="ar-read" type="button" disabled={page===0} onClick={()=>select(Math.max(0,page-1))}>上一组</button>
   <span role="status">第 {first+1} 至 {last} 行，共 {values.length} 行</span>
   <button className="ar-read" type="button" disabled={last>=values.length} onClick={()=>select(Math.min(Math.ceil(values.length/25)-1,page+1))}>下一组</button>
  </div>}
 </>;
}
function OriginalDocument({document,order}:{document:PurchaseQuantityDocument;order:boolean}){
 const title=useId();
 return <article aria-labelledby={title} className="ar-purchase-quantity-document">
  <h4 id={title}>{order?'采购订单 · 订购量原值':'采购入库单 · 入库单数量原值'}</h4>
  <p>单号 <bdi>{document.bill_code}</bdi> · 单据日期 <bdi>{document.bill_date}</bdi></p>
  <p>原来源记录时间：<time dateTime={document.source_recorded_at}>{document.source_recorded_at}</time>。本单保存 {document.returned_count} 行。</p>
  {document.items.length===0?<p>这张已选单据在保存的明细里没有显示商品行，不代表业务数量为零。</p>:
   <Windows values={document.items} label={order?'已选订单明细翻页':'已选入库单明细翻页'} draw={items=><dl>
    {items.map(item=><div key={item.ordinal} className="ar-purchase-quantity-row">
     <dt>第 {item.ordinal} 行 · <bdi>{item.product_name||'名称未提供'}</bdi></dt>
     <dd>编码 <bdi>{item.product_code||'未提供'}</bdi> · 规格 <bdi>{item.specification||'未提供'}</bdi></dd>
     <dd>{order?'订购量':'入库单数量'} <bdi>{item.quantity??'未提供'}</bdi> · 录入单位 <bdi>{item.unit??'未提供'}</bdi></dd>
    </div>)}
   </dl>}/>}
 </article>;
}
function SourceSelect({label,sources,value,onChange}:{label:string;sources:PurchaseQuantitySource[];value:string;onChange:(value:string)=>void}){
 const id=useId();
 return <div className="ar-purchase-quantity-source">
  <label htmlFor={id}>{label}</label>
  <select id={id} value={value} onChange={e=>onChange(e.target.value)} disabled={!sources.length}>
   <option value="">{sources.length?'请选择已保存的来源':'本轮没有当前可复核的来源'}</option>
   {sources.map((source,i)=><option key={source.step_id} value={String(i)}>{source.bill_code} · {source.bill_date} · 第 {source.sequence_no} 步</option>)}
  </select>
 </div>;
}

/** No list fetch until the click, no pair fetch until both human choices. No derived accounting. */
export function PurchaseQuantityComparisonPane({workId,runId,read,initialChoices,remember}:{
 workId:string;runId:string;read:ReadPurchaseQuantityComparison;initialChoices?:PurchaseQuantityChoices;
 remember?:(choices:PurchaseQuantityChoices|null)=>void;
}){
 const path=usePath(),panelId=useId(),frame=useRef<HTMLElement>(null),inFlight=useRef<AbortController|null>(null);
 const binding=JSON.stringify([path,workId,runId]),previousBinding=useRef(binding);
 const selected=useRef<PurchaseQuantityChoices>(initialChoices??{order:null,receipt:null});
 const [open,setOpen]=useState(!!initialChoices),[sources,setSources]=useState<PurchaseQuantitySources|null>(null);
 const [choice,setChoice]=useState(emptySelection),[pair,setPair]=useState<PurchaseQuantityPair|null>(null);
 const [pending,setPending]=useState(false),[notice,setNotice]=useState(initialChoices?'已保留选择。数量需重新核对，不会自动查单。':'');
 function keep(value:PurchaseQuantityChoices){
  selected.current=value;
  remember?.(value.order||value.receipt?value:null);
 }
 function stop(){const current=inFlight.current;inFlight.current=null;current?.abort();setPending(false);}
 function clearSelection(){setChoice(emptySelection);keep({order:null,receipt:null});}
 function close(){stop();setOpen(false);setSources(null);clearSelection();setPair(null);setNotice('');}
 useLayoutEffect(()=>{
  if(previousBinding.current!==binding){previousBinding.current=binding;close();}
  return ()=>{const current=inFlight.current;inFlight.current=null;current?.abort();};
 },[binding]);
 useEffect(()=>{
  function hidden(){if(document.visibilityState==='hidden')close();}
  document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',close);
  return ()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',close);};
 },[]);
 const interactive=()=>document.visibilityState!=='hidden'&&navigator.onLine!==false&&!!frame.current&&!frame.current.closest('[hidden]');
 async function load(selection:PurchaseQuantitySelection|null,consume:(raw:unknown)=>void){
  if(!interactive()||inFlight.current)return;
  const job=new AbortController();inFlight.current=job;setPending(true);
  const current=()=>inFlight.current===job&&!job.signal.aborted&&interactive();
  try{
   const raw=await read(selection,job.signal);
   if(current())consume(raw);
  }catch{
   if(current()){
    setSources(null);clearSelection();setPair(null);
    setNotice(selection?'当前无法重新核实已选两份来源，结果已隐藏。请重新读取已保存的采购明细。':
     '当前无法读取本轮已保存的采购明细。没有重新查单。');
   }
  }finally{
   if(inFlight.current===job){inFlight.current=null;setPending(false);}
  }
 }
 function getSources(){
  if(!interactive()||inFlight.current)return;
  const prior=selected.current;
  setOpen(true);setSources(null);setPair(null);setChoice(emptySelection);setNotice('正在核对本轮已保存的采购明细…');
  void load(null,raw=>{
   const result=parsePurchaseQuantitySources(raw,{workId,runId});setSources(result);
   const o=result.orders.findIndex(source=>source.step_id===prior.order);
   const r=result.receipts.findIndex(source=>source.step_id===prior.receipt);
   setChoice({order:o<0?'':String(o),receipt:r<0?'':String(r)});
   keep({order:result.orders[o]?.step_id??null,receipt:result.receipts[r]?.step_id??null});
   setNotice(result.orders.length&&result.receipts.length?'请选择一张采购订单和一张入库单。不会自动配对。':
    '本轮没有两份当前可复核的明细。并不表示没有采购或入库记录。');
  });
 }
 const {order,receipt}=selected.current;
 function select(side:keyof typeof choice,value:string){
  const candidates=side==='order'?sources?.orders:sources?.receipts;
  stop();setPair(null);setChoice(old=>({...old,[side]:value}));
  keep({...selected.current,[side]:value===''?null:candidates?.[Number(value)]?.step_id??null});
  setNotice('选完两张单据后，点击查看数量。');
 }
 function compare(){
  if(!order||!receipt||!interactive()||inFlight.current)return;
  setPair(null);setNotice('正在核对这两张已选单据…');
  const currentOrder=sources?.orders.find(source=>source.step_id===order)??order;
  const currentReceipt=sources?.receipts.find(source=>source.step_id===receipt)??receipt;
  void load({orderStep:order,receiptStep:receipt},raw=>{
   setPair(parsePurchaseQuantityPair(raw,{workId,runId},{order:currentOrder,receipt:currentReceipt}));setNotice('');
  });
 }
 return <section className="ar-purchase-quantity-panel" ref={frame} aria-label="已保存的采购数量对照">
  <button type="button" className="ar-read" aria-expanded={open} aria-controls={panelId} onClick={open?close:getSources}>
   {open?'收起采购数量对照':'对照已保存的采购订单与入库单'}
  </button>
  <div id={panelId} hidden={!open}>{open&&<>
   <p>只读本轮已保存的明细，不查新单。两张单据是否关联还未核实；这些数量不能用来判断到货、履约或库存，也不计算差额。</p>
   {notice&&<p role="status">{notice}</p>}
   {!sources&&!pending&&<>
    {order&&receipt&&!pair&&<button type="button" className="ar-read" onClick={compare}>重新核对这两单</button>}{' '}
    <button type="button" className="ar-read" onClick={getSources}>{order||receipt?'读取当前来源以改选或补选':'重新读取已保存的采购明细'}</button>
   </>}
   {sources&&<>
    <div className="ar-purchase-quantity-selection">
     <SourceSelect label="选择采购订单" sources={sources.orders} value={choice.order} onChange={value=>select('order',value)}/>
     <SourceSelect label="选择采购入库单" sources={sources.receipts} value={choice.receipt} onChange={value=>select('receipt',value)}/>
    </div>
    <button type="button" className="ar-read" disabled={!order||!receipt||pending} onClick={compare}>查看所选两单的原始数量</button>
   </>}
   {pair&&<section aria-label="两单数量与录入单位">
    <div className="ar-purchase-quantity-documents">
     <OriginalDocument document={pair.order} order/>
     <OriginalDocument document={pair.receipt} order={false}/>
    </div>
    {pair.same_code_candidates.length>0&&<details>
     <summary>看商品编码线索（不代表两单对应）</summary>
     <Windows label="商品编码线索翻页" values={pair.same_code_candidates} draw={items=><ul>
      {items.map(item=><li key={item.product_code}><bdi>{item.product_code}</bdi> · {clues[item.state]}。订单行：
       {' '+(item.order_ordinals.join('、')||'本单无此编码线索')}；入库单行：{item.receipt_ordinals.join('、')||'本单无此编码线索'}。</li>)}
     </ul>}/>
    </details>}
   </section>}
  </>}</div>
 </section>;
}
