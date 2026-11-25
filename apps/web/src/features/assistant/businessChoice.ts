import type {ResultSnapshot,ResultTable} from './publicResultSnapshot';
import type {OperationalReceiptPage} from './operationalReceiptPage';
import {receiptCoordinates} from './sourceRowReference';

export type ChoiceKind='voucher'|'product'|'amount';
type SalesChoiceRef={digest:string;blockId:string;rowId:string;kind:ChoiceKind};
export type ReceiptDetailChoiceRef={digest:string;blockId:string;rowIndex:number;kind:'purchase_receipt_detail'};
export type ChoiceRef=SalesChoiceRef|ReceiptDetailChoiceRef;
export const canDraftReceiptDetail=(has:(capability:string)=>boolean)=>
 has('guanjia.company.read')&&has('guanjia.purchase_receipts.read')&&has('guanjia.purchase_receipt_detail.read');

export const choiceTitles:Record<ChoiceKind,string>={voucher:'核对单据来源',product:'核对商品和规格',amount:'核对金额口径'};
function col(block:ResultTable,key:string,row:number) {
 const i=block.columns.findIndex(c=>c.key===key);return i<0?null:block.rows[row]?.cells[i]??null;
}
function labelText(v:unknown) {return typeof v==='string'&&v.trim()?v.trim():null;}
export function sourceRowChoice(block:ResultTable,row:number) {
 if(!Number.isInteger(row)||row<0||row>=block.rows.length)return null;
 const voucher=labelText(col(block,'bill_code',row)), name=labelText(col(block,'product_name',row)),code=labelText(col(block,'product_code',row)),spec=labelText(col(block,'standard_name',row));
 const kinds:ChoiceKind[]=[];
 if(voucher)kinds.push('voucher');
 if(block.source.action==='sales_lines'&&(code||name))kinds.push('product');
 const total=col(block,'total',row);
 if(total!==null&&['number','string'].includes(typeof total)&&String(total).trim()!=='')kinds.push('amount');
 if(!kinds.length)return null;
 return {rowId:block.rows[row].row_id,kinds,name: name||code||voucher||'来源记录',detail:[name&&code?code:null,spec,voucher].filter(Boolean).join(' · ')};
}
/** Resolved only against a freshly authorized full snapshot, never model text.
 * Uses our own language and checked coordinates, not source values or instructions. */
export function verifiedChoiceDraft(fresh:ResultSnapshot,ref:ChoiceRef):string|null {
 if(fresh.state!=='available'||fresh.snapshot_sha256!==ref.digest||!fresh.run_id)return null;
 if(ref.kind==='purchase_receipt_detail'){
  const page=fresh.blocks.find((b): b is OperationalReceiptPage=>
    b.type==='operational_receipt_page'&&b.action==='purchase_receipts_query'&&b.block_id===ref.blockId);
  if(!page||!Number.isSafeInteger(ref.rowIndex)||ref.rowIndex<0||ref.rowIndex>=page.items.length)return null;
  return `请只读查看本事项已核对的这张采购入库单的商品明细（${receiptCoordinates(page.sequence_no,page.offset,ref.rowIndex)}）。请先核对单号和日期，再给出已核实的商品、原数量和录入单位。若无法核对同一张单，请说明，不要改动单据，也不要按库存或结算解释。`;
 }

 const block=fresh.blocks.find((b): b is ResultTable => b.type==='report_table'&&b.block_id===ref.blockId);if(!block)return null;
 const row=block.rows.findIndex(r=>r.row_id===ref.rowId),eligible=sourceRowChoice(block,row);
 if(!eligible?.kinds.includes(ref.kind))return null;
 const op={voucher:'请解释这行属于哪张单据、它的业务日期，并列出可核对的来源。',product:'请核对这行商品编码、名称和规格是否一致；缺少依据的地方单独说明，不要自动确认商品身份。',amount:'请区分这行的金额原值、币种和税口径；未提供的项目写明未确认，不要推算。'}[ref.kind];
 const kind=block.source.action==='sales_lines'?'销售明细':'销售单据';
 return '请核对本事项上次'+kind+'查询中的一行（执行步骤 '+block.sequence_no+'，来源偏移 '+block.coverage.offset+'，本页第 '+(row+1)+' 行）。'+op+'若无法重新读取同一来源，请说明，不要改动业务数据。';
}
