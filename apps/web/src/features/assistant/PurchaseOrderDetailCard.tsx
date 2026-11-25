import type { PurchaseOrderDetailPage } from './purchaseOrderDetailPage';
import './purchase-order-detail.css';

/** Display only. The Host, not this component, authorizes the current readback. */
export function PurchaseOrderDetailCard({page}:{page:PurchaseOrderDetailPage}) {
 return <article className={'ar-card ar-purchase-order-detail'+(page.detail_schema===2?' ar-purchase-order-detail--with-quantity':'')} aria-label="采购订单商品明细">
  <header>
   <span className="ar-kicker">来源：管家婆 · 采购订单</span>
   <h3>采购订单商品明细</h3>
   <p>来源返回 {page.returned_count} 条商品记录，{page.detail_schema===2?'订购量和单位按保存原值显示，不换算。':'仅展示编码、名称和规格。'}不代表已核齐整单，也不能据此判断是否已收货入库。</p>
   {page.detail_schema===2&&<p>原明细保存时间：<time dateTime={page.source_recorded_at}>{page.source_recorded_at}</time>。这是已保存的明细，不是实时查询。</p>}
  </header>
  <dl className="ar-purchase-order-identity">
   <div><dt>单号</dt><dd><bdi>{page.bill_code}</bdi></dd></div>
   <div><dt>单据日期原值</dt><dd><bdi>{page.bill_date}</bdi></dd></div>
  </dl>
  {page.items.length===0?<p>本次来源未返回可展示的商品记录。这不等于整单没有商品。</p>:
   <ol className="ar-purchase-order-lines" aria-label="已返回的商品记录">
    {page.items.map((item,index)=><li key={index}><dl>
     <div><dt>商品编码</dt><dd><bdi>{item.product_code}</bdi></dd></div>
     <div><dt>商品名称</dt><dd><bdi>{item.product_name}</bdi></dd></div>
     <div><dt>规格</dt><dd>{item.specification?.trim()?<bdi>{item.specification}</bdi>:'来源未列规格'}</dd></div>
     {'reported_order_quantity' in item&&<>
      <div><dt>订购量原值</dt><dd>{item.reported_order_quantity===null?'来源未列数量':<bdi>{item.reported_order_quantity}</bdi>}</dd></div>
      <div><dt>单位原值</dt><dd>{item.reported_order_unit===null?'来源未列单位':<bdi>{item.reported_order_unit}</bdi>}</dd></div>
     </>}
    </dl></li>)}
   </ol>}
 </article>;
}
