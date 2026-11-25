import {operationalReceiptNames} from './operationalReceiptPage';
import {operationalDetailNames} from './operationalReceiptDetail';
import {transferHeading} from './operationalTransfer';
import type {ResultBlock,ResultSnapshot} from './publicResultSnapshot';
import './verified-source-directory.css';

export function verifiedSourceHeading(block:ResultBlock):string{
 switch(block.type){
  case 'report_table':return block.source.action==='sales_lines'?'已过账销售商品明细':'销售单据';
  case 'operational_receipt_page':return operationalReceiptNames[block.action];
  case 'operational_receipt_detail':return operationalDetailNames[block.action];
  case 'operational_transfer_page':case 'operational_transfer_detail':return transferHeading(block);
  case 'purchase_order_detail':return '采购订单商品明细';
  case 'product_barcode_page':return '商品单位与条码';
  case 'customer_receivables_detail':return '客户单据应收明细';
  case 'partial_endpoint_observation':return operationalReceiptNames[block.action]+'（部分来源）';
  case 'partial_stock_observation':return '库存观察（部分来源）';
 }
}
export const verifiedSourceAnchor=(prefix:string,index:number)=>prefix+'-'+index;
export function needsVerifiedSourceDirectory(snapshot:ResultSnapshot|null):boolean {
 return snapshot?.state==='available'&&snapshot.blocks.length>1;
}

/** Jump only to cards actually present in the currently authorized snapshot. */
export function VerifiedSourceDirectory({snapshot,prefix}:{snapshot:ResultSnapshot|null;prefix:string}){
 if(!snapshot||!needsVerifiedSourceDirectory(snapshot))return null;
 return <nav aria-label="已核对来源目录" className="ar-verified-directory">
  <h3>已核对来源目录</h3>
  <ul>{snapshot.blocks.map((block,index)=>{
   const offset=block.type==='report_table'?block.coverage.offset:block.type==='operational_receipt_page'||block.type==='operational_transfer_page'||block.type==='product_barcode_page'?block.offset:null;
   return <li key={block.block_id}><button type="button" className="ar-read"
    onClick={()=>{
     const target=document.getElementById(verifiedSourceAnchor(prefix,index));
     target?.focus({preventScroll:true});target?.scrollIntoView({block:'start'});
    }}>
    来源 {index+1} · {verifiedSourceHeading(block)}{offset===null?'':` · 偏移 ${offset}`}
   </button></li>;
  })}</ul>
  <p>仅跳转到本事项当前显示的来源，不会重新查询。</p>
 </nav>;
}
