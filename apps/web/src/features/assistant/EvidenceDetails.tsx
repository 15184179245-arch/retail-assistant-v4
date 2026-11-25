import { observationTimeLabel, parseObservationTime, type EvidenceCitation } from './evidenceCitation';
import './EvidenceDetails.css';
export function EvidenceDetails({citations,available=true}:{citations:EvidenceCitation[];available?:boolean}) {
  if (!available || !citations.length) return null;
  const observations=citations.filter(c=>c.kind==='guanjia_observation');
  const productImages=citations.filter(c=>c.kind==='product_image_result');
  const other=citations.filter(c=>c.kind!=='guanjia_observation'&&c.kind!=='product_image_result');
  return <details className="assistant-sources assistant-evidence"><summary>查看依据 · {citations.length}</summary>
    <div className="assistant-evidence-panel">
      {!!observations.length && <section aria-label="接口观察回执"><h4>接口观察回执</h4><p>答复附带的接口观察回执，不代表 ERP 数据更新时间或全量实时状态。</p>
        <ul>{observations.map((c,i)=>{const time=parseObservationTime(c.observedAt);return <li key={i}><span>{c.label}</span><small>{time ? <>本次接口观察时间：<time dateTime={time}>{observationTimeLabel(time)}</time></> : '未提供可用的本次接口观察时间。'}</small></li>;})}</ul>
      </section>}
      {!!productImages.length && <section aria-label="商品图片依据"><h4>商品图片依据</h4>
        <p>商品主档图片曾用于这次答复。这条来源回执本身不带原图。若本事项已保存可查看的商品图片，可在业务结果中打开。</p>
        <ul>{productImages.map((c,i)=><li key={i}>{c.label}</li>)}</ul>
      </section>}
      {!!other.length && <section aria-label="其他来源元数据"><h4>其他来源元数据</h4><p>答复附带的来源信息，不据此推断接口执行或 ERP 更新时间。</p><ul>{other.map((c,i)=><li key={i}>{c.label}</li>)}</ul></section>}
    </div>
  </details>;
}
