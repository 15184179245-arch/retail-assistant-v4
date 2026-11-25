import {useId,useLayoutEffect,useRef} from 'react';
import type {ReceiptDetailChoiceRef} from './businessChoice';
import {operationalReceiptNames,type OperationalReceiptPage} from './operationalReceiptPage';
import './operational-receipt-card.css';
import './return-source-short-reading.css';

/** Each card is a separately authorized source page, not a final answer or a ledger result. */
export function OperationalReceiptCard({page,digest,onChooseDetail,picked=-1,onPick}:{page:OperationalReceiptPage;digest?:string;
 onChooseDetail?:(ref:ReceiptDetailChoiceRef,stillSelected:()=>boolean)=>void;picked?:number;onPick?:(rowIndex:number)=>void}){
 const pickerVersion=useRef(0);
 useLayoutEffect(()=>()=>{pickerVersion.current++;},[]);
 const selectedRow=page.items[picked];
 const id=useId(),name=operationalReceiptNames[page.action];
 const returnPage=page.action==='purchase_returns'||page.action==='sales_returns';
 return <article className="ar-card ar-operational-receipt" aria-labelledby={id+'-title'}>
  <header>
   <span className="ar-kicker">来源：管家婆 · 单据列表</span>
   <h3 id={id+'-title'}>{name} · 已核对来源页</h3>
   <p>来源偏移 {page.offset}，本页 {page.returned_count} 张单据。
    {page.reported_total!==null?` 来源报本次查询范围共 ${page.reported_total} 张。`:' 未提供可展示的来源总数。'}
   </p>
   <p id={id+'-scope'}>各来源页分别显示，不相加。单据和状态码只是此次查询的来源记录，不代表查全或已完成入库、退款、结算。</p>
   {page.items.length===0&&<p>已核对的来源在这一页返回 0 张；不表示其他页或其他范围没有单据。</p>}
   {page.items.length>0&&<p className="ar-operational-scroll-hint" id={id+'-scroll'}>内容超出宽度时，可在表内左右滑动，查看日期和状态码。</p>}
  </header>
  {page.action==='purchase_receipts_query'&&page.items.length>0&&digest&&onChooseDetail&&
   <div className="ar-panel ar-next">
    <label htmlFor={id+'-pick'}>选一张采购入库单（仅本页）</label>
    <select id={id+'-pick'} value={selectedRow?picked:-1} aria-describedby={id+'-draft-help'} onChange={e=>{pickerVersion.current++;onPick?.(Number(e.target.value));}}>
     <option value={-1}>请选择单据</option>
     {page.items.map((row,i)=><option key={i} value={i}>第 {i+1} 行 · {row.bill_code}</option>)}
    </select>
    {selectedRow&&<p className="ar-receipt-selection">已选本页第 {picked+1} 行：<bdi>{selectedRow.bill_code}</bdi></p>}
    <button type="button" className="ar-read" disabled={picked<0||picked>=page.items.length}
     onClick={()=>{
      const version=pickerVersion.current;
      onChooseDetail({kind:'purchase_receipt_detail',digest,blockId:page.block_id,rowIndex:picked},
        ()=>pickerVersion.current===version);
     }}>把只读明细问题加入草稿</button>
    <p id={id+'-draft-help'}>保留已有草稿。你可以编辑后再发送，不会自动查单或修改业务。</p>
   </div>}
  {returnPage&&page.items.length>0&&<p id={id+'-keyboard'} className="assistant-sr-only">
   在表格区域可用翻页键浏览，按 Home（开头）键回看表头。
  </p>}
  {page.items.length>0&&<div className={'ar-operational-scroll'+(returnPage?' ar-return-source-short-reading':'')}
   role="region" tabIndex={0} aria-label={name+'单据表'} aria-describedby={returnPage?id+'-scroll '+id+'-keyboard':id+'-scroll'}>
   <table aria-labelledby={id+'-title'} aria-describedby={id+'-scope'}>
    <thead><tr><th scope="col">单号</th><th scope="col">单据日期原值</th><th scope="col">状态码</th></tr></thead>
    <tbody>{page.items.map((row,index)=><tr key={index}><th scope="row"><bdi>{row.bill_code}</bdi></th>
     <td><bdi>{row.bill_date??'日期未核实'}</bdi></td><td>{row.bill_state}</td></tr>)}</tbody>
   </table>
  </div>}
 </article>;
}
