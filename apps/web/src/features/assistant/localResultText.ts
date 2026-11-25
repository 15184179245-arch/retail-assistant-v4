import type {EvidenceCitation} from './evidenceCitation';
import {isTransferBlock,transferDocumentCells,transferDocumentColumns,transferHeading,transferLineCells,
 transferLineColumns,transferScopeNotice,type TransferBlock} from './operationalTransfer';
import type {ResultSnapshot,ResultTable,Scalar} from './publicResultSnapshot';
import type {PurchaseOrderDetailPage,PurchaseOrderProductLine} from './purchaseOrderDetailPage';
import {operationalReceiptNames,type OperationalReceiptPage} from './operationalReceiptPage';

import {operationalDetailNames,priceFields,priceStates,type OperationalReceiptDetailPage,type ReceiptPriceAmounts} from './operationalReceiptDetail';

export function savedAnswerText(answer:string,citations:EvidenceCitation[]):string {
 return [
  '已保存的助理答复',
  '',
  answer,
  ...(citations.length?['','答复附带的来源（仅标签与已记录的观察时间）',
   ...citations.map(c=>'- '+c.label+(c.observedAt?' · 观察记录：'+c.observedAt:''))]:[]),
 ].join('\n');
}
function plain(value:Scalar):string{
 return value===null?'来源未提供':typeof value==='string'?JSON.stringify(value):String(value);
}
function sheet(cells:string[],rows:Scalar[][]):string[]{
 return ['列：'+cells.map(plain).join(' | '),...rows.map((row,i)=>'行 '+(i+1)+'：'+row.map(plain).join(' | '))];
}
function report(block:ResultTable,index:number):string[]{
 const c=block.coverage;
 const title=block.source.action==='sales_lines'?'已过账销售商品明细':'销售单据';
 return [
  '来源页 '+index+'：管家婆 · '+title,
  '查询偏移：'+c.offset+'；本页 '+c.row_count+' 行；来源所报 '+c.reported_total+' 行。',
  c.page_exhausted?'已到来源所报末尾，末页本身不证明查全。':'本页未标明到达来源所报末尾。',
  '来源记录时间：'+block.source.recorded_at,
  block.notice,
  ...sheet(block.columns.map(col=>col.label),block.rows.map(row=>row.cells)),
 ];
}
function order(block:PurchaseOrderDetailPage,index:number):string[]{
 const columns=['商品编码','商品名称','规格'];
 const baseCells=(row:PurchaseOrderProductLine)=>[row.product_code,row.product_name,row.specification];
 const hasOrderQuantity=block.detail_schema===2;
 const rows=hasOrderQuantity
  ?block.items.map(row=>[...baseCells(row),row.reported_order_quantity,row.reported_order_unit])
  :block.items.map(baseCells);
 return [
  '来源页 '+index+'：管家婆 · 采购订单商品明细',
  '单号原值：'+plain(block.bill_code)+'；单据日期原值：'+plain(block.bill_date),
  '本回执返回 '+block.returned_count+' 条商品记录。不代表查全整单或已经收货入库。',
  ...sheet(hasOrderQuantity?[...columns,'订购量原值','单位原值']:columns,rows),
 ];
}
function receipts(block:OperationalReceiptPage,index:number):string[]{
 return [
  '来源页 '+index+'：管家婆 · '+operationalReceiptNames[block.action],
  '来源偏移：'+block.offset+'；本页 '+block.returned_count+' 张单据；'+
   (block.reported_total===null?'未提供可展示的来源总数。':'来源报本次查询范围共 '+block.reported_total+' 张。'),
  '来源状态码不证明入库、退款或结算已完成。',
  ...sheet(['单号','单据日期原值','状态码'],block.items.map(row=>[row.bill_code,row.bill_date,row.bill_state])),
 ];
}
function receiptDetail(block:OperationalReceiptDetailPage,index:number):string[]{
 const money=block.price_amount_state==='reported'||block.price_amount_state==='partially_reported';
 const keys=Object.keys(priceFields) as (keyof ReceiptPriceAmounts)[];
 return [
  '来源页 '+index+'：管家婆 · '+operationalDetailNames[block.action],
  '单号原值：'+plain(block.bill_code)+'；单据日期原值：'+plain(block.bill_date)+'；来源状态码：'+block.bill_state+'。',
  '本次来源返回 '+block.returned_count+' 条商品记录，不代表已查全、库存变动或实际结算。',
  priceStates[block.price_amount_state],
  ...(money&&block.price_amount_state!=='reported'?['币种、税口径和实付未核实。']:[]),
  ...sheet(['商品编码','商品名称','规格','数量原值','录入单位',...(money?keys.map(key=>priceFields[key]):[])],
   block.items.map(line=>[line.product_code||null,line.product_name||null,line.specification,line.reported_quantity,line.reported_unit,
    ...(money?keys.map(key=>line.price_amounts?.[key]??null):[])])),
 ];
}
function transfer(block:TransferBlock,index:number):string[]{
 return [
  '来源页 '+index+'：管家婆 · '+transferHeading(block),
  ...(block.type==='operational_transfer_page'
    ?['来源偏移：'+block.offset+'；本页 '+block.returned_count+' 张单据；来源未提供可展示的总数。',
      ...sheet(transferDocumentColumns,block.items.map(transferDocumentCells))]
    :[...sheet(transferDocumentColumns,[transferDocumentCells(block)]),
      '本次来源返回 '+block.returned_count+' 条商品记录。',
      ...sheet(transferLineColumns,block.items.map(transferLineCells))]),
  transferScopeNotice,
 ];
}
type ExportPage=ResultTable|PurchaseOrderDetailPage|OperationalReceiptPage|OperationalReceiptDetailPage|TransferBlock;
function isExportPage(block:ResultSnapshot['blocks'][number]):block is ExportPage {
 return block.type==='report_table'||block.type==='purchase_order_detail'||block.type==='operational_receipt_page'||block.type==='operational_receipt_detail'||isTransferBlock(block);
}
export function hasExportableResult(snapshot:ResultSnapshot|null):boolean {
 return snapshot?.state==='available'&&snapshot.blocks.some(isExportPage);
}
/** Only printable table projections; no URLs, source digests, actor IDs or non-table amounts. */
export function savedResultText(snapshot:ResultSnapshot):string {
 if(snapshot.state!=='available')throw Error('结果目前不可读取。');
 const pages=snapshot.blocks.filter(isExportPage);
 if(!pages.length)throw Error('没有可导出的来源页。');
 return ['本次已授权的业务来源表',
  '仅包含本事项已保存的授权表页，不受页面筛选影响；未请求其他页，不合并不同来源，也不代表整个业务范围。','',
  ...pages.flatMap((page,i)=>[...(page.type==='report_table'?report(page,i+1):page.type==='operational_receipt_page'?receipts(page,i+1):page.type==='operational_receipt_detail'?receiptDetail(page,i+1):isTransferBlock(page)?transfer(page,i+1):order(page,i+1)), '']),
 ].join('\n');
}
