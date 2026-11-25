import type { PartialStockObservation } from './publicResultSnapshot';

/** Display only a verified public snapshot fact. No action or guessed price/unit. */
export function PartialStockCard({block}: {block: PartialStockObservation}) {
  return <article className="ar-card ar-partial-stock">
    <header>
      <span className="ar-kicker">本次工作已保存的来源</span>
      <h3>原库存观察已核实，售价未能核实</h3>
    </header>
    <dl className="ar-partial-stock-values">
      <div><dt>商品</dt><dd>{block.product.name}<small>货号 {block.product.code}</small></dd></div>
      <div><dt>库存原值</dt><dd><strong>{block.stock.quantity_raw}</strong><small>单位未核实</small></dd></div>
    </dl>
    <p>原观察时间（UTC）：<time dateTime={block.stock.observed_at}>{block.stock.observed_at}</time>。这是当时的库存观察，不是当前库存，也不代表可售数量。</p>
    <p>售价未核实，不代表没有售价。</p>
    <details>
      <summary>查看库存依据</summary>
      <dl><dt>来源</dt><dd>管家婆库存回执</dd></dl>
    </details>
  </article>;
}
