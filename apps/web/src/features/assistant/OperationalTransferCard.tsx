import {useId} from 'react';
import './operational-receipt-card.css';
import {transferDocumentCells,transferDocumentColumns,transferHeading,transferLineCells,transferLineColumns,transferScopeNotice,type TransferBlock} from './operationalTransfer';

type Cells=(string|number|null)[];
function SourceTable({cols,rows,label}:{cols:string[];rows:Cells[];label:string}){
 return <div className="ar-table-scroll" tabIndex={0} role="region" aria-label={label}>
  <table className="ar-table"><thead><tr>{cols.map(key=><th scope="col" key={key}>{key}</th>)}</tr></thead>
   <tbody>{rows.map((row,i)=><tr key={i}>{row.map((v,j)=><td key={j}><bdi>{v??'来源未提供'}</bdi></td>)}</tr>)}</tbody></table>
 </div>;
}
export function OperationalTransferCard({page}:{page:TransferBlock}){
 const title=transferHeading(page),id=useId();
 return <article className="ar-card" aria-labelledby={id}>
  <header><span className="ar-kicker">来源：管家婆</span><h3 id={id}>{title} · 已核对来源</h3></header>
  <div className="ar-panel">
  {page.type==='operational_transfer_page'?<>
   <p>来源偏移 {page.offset}；本页 {page.returned_count} 张单据。来源未提供可展示的总数。</p>
   {page.items.length===0&&<p>已核对的来源在这一页返回 0 张；不表示其他页或其他范围没有单据。</p>}
   {page.items.length>0&&<SourceTable cols={transferDocumentColumns} rows={page.items.map(transferDocumentCells)} label="调拨单来源表"/>}
  </>:<>
   <SourceTable cols={transferDocumentColumns} rows={[transferDocumentCells(page)]} label="原调拨单"/>
   <p>本次来源返回 {page.returned_count} 条商品记录。</p>
   {page.items.length>0&&<SourceTable cols={transferLineColumns} rows={page.items.map(transferLineCells)} label="调拨商品行来源表"/>}
  </>}
  <p>{transferScopeNotice}</p>
  </div>
 </article>;
}
