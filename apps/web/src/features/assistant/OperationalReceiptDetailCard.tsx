import {useId,useMemo,useRef} from 'react';
import {operationalDetailNames,priceFields,priceStates,type OperationalReceiptDetailPage,type ReceiptPriceAmounts} from './operationalReceiptDetail';
import {operationalDetailPage,type OperationalDetailView} from './operationalReceiptDetailView';
import './operational-receipt-detail.css';
import './purchase-return-reading.css';
import './purchase-receipt-reading.css';

const moneyKeys=Object.keys(priceFields) as (keyof ReceiptPriceAmounts)[];
function Field({label,value,missing='来源未提供'}:{label:string;value:string|number|null;missing?:string}){
 return <div><dt>{label}</dt><dd><bdi>{value??missing}</bdi></dd></div>;
}

/** Only display the authorized Host projection. No totals, currency inference, or upstream link. */
export function OperationalReceiptDetailCard({page,view,setView}:{page:OperationalReceiptDetailPage;view:OperationalDetailView;setView:(view:OperationalDetailView)=>void}){
 const title=useId(),range=useId(),searchId=useId(),name=operationalDetailNames[page.action];
 const receiptReading=page.action==='purchase_receipt_detail',keyboardHelp=range+'-keyboard';
 const marker=useRef<HTMLParagraphElement>(null);
 const {rows,matchCount,filtered,pageCount,pageIndex,first,last}=useMemo(()=>operationalDetailPage(page.items,view),[page.items,view]);
 const money=page.price_amount_state==='reported'||page.price_amount_state==='partially_reported';
 function goTo(index:number,fromBottom=false){
  setView({...view,pageIndex:index});
  if(fromBottom){
   marker.current?.focus({preventScroll:true});
   marker.current?.scrollIntoView({block:'start'});
  }
 }
 function arrows(fromBottom:boolean){
  return <>
   <button type="button" className="ar-read" disabled={pageIndex<=0} onClick={()=>goTo(pageIndex-1,fromBottom)}>上一页</button>
   <button type="button" className="ar-read" disabled={pageIndex>=pageCount-1} onClick={()=>goTo(pageIndex+1,fromBottom)}>下一页</button>
  </>;
 }
 return <article className={'ar-card ar-operational-detail'+(page.action==='purchase_return_detail'?' ar-purchase-return-reading':'')} aria-labelledby={title}>
  <header>
   <span className="ar-kicker">来源：管家婆 · 逐单读取</span>
   <h3 id={title}>{name}</h3>
   <p>本次来源返回 {page.returned_count} 条商品记录，不代表查全整单、库存变动或实际结算。
    {page.action==='purchase_return_detail'&&'也不能据此确认已经退款。'}
   </p>
   <p>{priceStates[page.price_amount_state]}</p>
   {money&&page.price_amount_state!=='reported'&&<p>币种、税口径和实付未核实。</p>}
  </header>
  <dl className="ar-operational-detail-fields ar-operational-detail-summary">
   <Field label="单号" value={page.bill_code}/><Field label="单据日期原值" value={page.bill_date} missing="日期未核实"/>
   <Field label="来源状态码" value={page.bill_state}/>
  </dl>
  {!page.items.length&&page.price_amount_state!=='no_lines'&&<p className="ar-operational-detail-range">这次明细没有可展示的商品行。</p>}
  {page.items.length>0&&<>
   {page.items.length>1&&<div className="ar-operational-detail-range ar-operational-detail-search">
    <label htmlFor={searchId}>查找本单已保存商品</label>
    <input id={searchId} type="search" value={view.query} maxLength={200}
     placeholder="商品名、编码或规格" onChange={e=>setView({query:e.target.value,pageIndex:0})} aria-describedby={searchId+'-scope'}/>
    <p id={searchId+'-scope'}>只查找、翻看本单已保存商品；不会重查，下载仍含全部已保存行。</p>
   </div>}
   <p id={range} tabIndex={-1} ref={marker} role="status" className="ar-operational-detail-range">
    {matchCount ? (filtered?`本单匹配 ${matchCount} / ${page.returned_count} 条；显示匹配第 ${first}–${last} 条。`:
     `显示本单已保存商品第 ${first}–${last} 条，共 ${page.returned_count} 条。`):
     '本单已保存商品没有匹配项，不代表其他范围没有商品。'}
   </p>
   {pageCount>1&&<nav className="ar-operational-detail-nav" aria-label="本单已保存商品分页">
    <label htmlFor={range+'-page'}>本单页码</label>
    <select id={range+'-page'} value={pageIndex} onChange={e=>goTo(Number(e.target.value))}>
     {Array.from({length:pageCount},(_,i)=><option key={i} value={i}>第 {i+1} / {pageCount} 页</option>)}
    </select>
    {arrows(false)}
   </nav>}
   {receiptReading&&rows.length>0&&<p id={keyboardHelp} className="assistant-sr-only">可用翻页键继续浏览商品行。</p>}
   {rows.length>0&&<ol className={'ar-operational-detail-lines'+(receiptReading?' ar-purchase-receipt-keyboard-list':'')}
    tabIndex={receiptReading?0:undefined} aria-describedby={receiptReading?range+' '+keyboardHelp:range} aria-label={name+'的商品行'}>
    {rows.map(({line,ordinal})=><li key={ordinal} value={ordinal}>
     <h4>{receiptReading&&<span className="assistant-sr-only">第 {ordinal} 行：</span>}<bdi>{line.product_name||'商品名称：来源未提供'}</bdi></h4>
     <dl className="ar-operational-detail-fields">
      <Field label="商品编码" value={line.product_code||null}/><Field label="规格" value={line.specification}/>
      <Field label="数量原值" value={line.reported_quantity}/><Field label="录入单位" value={line.reported_unit} missing="单位未核实"/>
     </dl>
     {money&&(line.price_amounts?<dl className="ar-operational-detail-fields ar-operational-detail-prices">
      {moneyKeys.map(key=><Field key={key} label={priceFields[key]} value={line.price_amounts?.[key]??null} missing="无可展示值"/>)}
     </dl>:<p>这一行没有可展示的价格金额。</p>)}
    </li>)}
   </ol>}
   {pageCount>1&&<nav className="ar-operational-detail-nav" aria-label="本单明细底部分页">{arrows(true)}</nav>}
  </>}
 </article>;
}
