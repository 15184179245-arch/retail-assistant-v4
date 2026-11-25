import {observationTimeLabel} from './evidenceCitation';
import type {PartialEndpointObservation} from './publicResultSnapshot';

const sourceName:{[K in PartialEndpointObservation['action']]:string}={
 sales_returns:'销售退货单',purchase_receipts_query:'采购入库单',
};
export function PartialEndpointCard({block}:{block:PartialEndpointObservation}) {
 const source=sourceName[block.action];
 return <article className="ar-card ar-partial-endpoint" aria-label={source+' · 已核实的一页'}>
  <header><span className="ar-kicker">部分结果 · 管家婆</span><h3>{source}：本页已核实 {block.returned_count} 条</h3>
   <p>本事项未完成，没有完整答复。这里只显示已核实的一页。本回执未提供分页位置或来源总数，不能判断是否查全；其他部分未核实，不代表没有记录。</p></header>
  <dl className="ar-facts"><dt>来源</dt><dd>管家婆 · {source}</dd>
   <dt>来源记录时间</dt><dd><time dateTime={block.recorded_at}>{observationTimeLabel(block.recorded_at)}</time></dd></dl>
  {block.returned_count===0&&<p>0 条仅指来源在本页返回的条数，不代表其他页或整个业务范围。</p>}
 </article>;
}
