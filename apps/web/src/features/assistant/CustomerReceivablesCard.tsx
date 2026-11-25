import {useId,type ReactNode} from 'react';
import type {CustomerReceivablesPage,CustomerReceivablesRow} from './customerReceivablesPage';
import './customer-receivables.css';

const dateLabel='单据日期';
const codeLabel='单号';
const receivableLabel='应收增加（原值）';
const advanceLabel='预收增加（原值）';

function sourceAmount(row:CustomerReceivablesRow,value:string|null):string {
  if(row.amounts_withheld_by_source)return '来源未开放';
  return value===null?'来源未提供':value;
}

function BillCell({label,children}:{label:string;children:ReactNode}) {
  return <td role="cell">
    <span className="ar-customer-label" aria-hidden="true">{label}</span>
    <span>{children}</span>
  </td>;
}

/** Display-only. No arbitrary number formatting, totals, write actions or raw tool attachment. */
export function CustomerReceivablesCard({page}:{page:CustomerReceivablesPage}) {
  const noteId=useId();
  const withheld=page.items.some(row=>row.amounts_withheld_by_source);
  const missing=page.items.some(row=>!row.amounts_withheld_by_source&&
    (row.receivable_increase_reported===null||row.advance_receipt_increase_reported===null));
  const amountNote=withheld||missing;
  return <article className="ar-card ar-customer-card" aria-label="客户单据应收和预收变动">
    <header>
      <span className="ar-kicker">来源：管家婆 · 客户单据对账明细</span>
      <h3>客户单据：应收增加与预收增加</h3>
      <p>{page.start_date} 至 {page.end_date}，本页已核对 {page.returned_count} 条普通单据。</p>
      <p>这是单据发生额，不是当前余额，也不代表应付金额。币种未核实，两列不相抵。</p>
      {amountNote&&<p id={noteId}>
        {withheld&&'「来源未开放」表示本次不能查看该单据的金额。'}
        {missing&&'「来源未提供」表示本次来源没有返回这一项金额。'}
        {withheld&&missing?'这两种情况都不等于零。':'这不等于零。'}
      </p>}
      {page.rows_omitted_unverified_date>0&&<p>另有 {page.rows_omitted_unverified_date} 条因单据日期不能核对，未列出。不是上游总条数。</p>}
    </header>
    {page.items.length===0?<p role="status">本页没有可核对的普通单据。这不代表没有往来记录或欠款为零。</p>:
      <div className="ar-table-scroll" tabIndex={0} role="region" aria-label="客户普通单据本页明细">
        {/* Explicit roles keep the four-column relationships when CSS stacks rows on narrow screens. */}
        <table role="table" aria-describedby={amountNote?noteId:undefined}>
          <caption>按来源原值展示，只限本页；缺失的金额不按零处理。</caption>
          <thead role="rowgroup"><tr role="row">
            <th scope="col" role="columnheader">{dateLabel}</th><th scope="col" role="columnheader">{codeLabel}</th>
            <th scope="col" role="columnheader">{receivableLabel}</th><th scope="col" role="columnheader">{advanceLabel}</th>
          </tr></thead>
          <tbody role="rowgroup">{page.items.map((row,index)=><tr key={index} role="row">
            <BillCell label={dateLabel}>{row.bill_date}</BillCell>
            <BillCell label={codeLabel}>{row.bill_code??'来源未提供'}</BillCell>
            <BillCell label={receivableLabel}>{sourceAmount(row,row.receivable_increase_reported)}</BillCell>
            <BillCell label={advanceLabel}>{sourceAmount(row,row.advance_receipt_increase_reported)}</BillCell>
          </tr>)}</tbody>
        </table>
      </div>}
  </article>;
}
