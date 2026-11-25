import { useEffect, useRef, useState } from "react";

/** PUR-003: host-supplied scoped facts, NEVER an ERP network or write client. */
export interface ReceivingReviewBatch {
  id: string;
  version: number;
  status: "observed" | "reviewed";
  created_by: string;
  confirmed_by: string | null;
  delivery_note_sha256: string;
  open_discrepancies: number;
  lines: Array<{
    line_key: string;
    product_id: string | null;
    unknown_product_label: string | null;
    observed_quantity: string;
    unit: string;
    evidence_sha256: string;
  }>;
  discrepancies: Array<{
    id: string; kind: string; latest_decision: string | null;
  }>;
}
export interface PurchaseDocumentClaim {
  document_kind: "purchase_order" | "goods_receipt";
  claimed_document_no: string;
  evidence_type: "erp_screen_view" | "erp_paper_document";
  evidence_sha256: string;
  source_observed_at: string;
  currency: "CNY";
  total_amount: string | null;
  human_checked: true;
  note: string;
  previous_receipt_digest: string | null;
  lines: Array<{
    line_key: string; product_id: string; unit: string; quantity: string;
    unit_price: string | null; line_total: string | null; source_line_sha256: string;
  }>;
}
import type { Session } from "../../lib/auth/session";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState } from "../../components/EmptyState";

export interface ReceivingReviewProps {
  batch?: ReceivingReviewBatch | null;
  session?: Session | null;
  latestReceiptDigest?: string | null;
  /** Supplied by a future authorized host, not implemented by this card. */
  onAttest?: (
    batchId: string,
    expectedVersion: number,
    payload: PurchaseDocumentClaim,
    idempotencyKey: string,
  ) => Promise<void>;
}

const sha = /^[0-9a-f]{64}$/;
const decimal = /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?$/;
const claimedNo = /^[A-Za-z0-9][A-Za-z0-9_./-]{0,79}$/;

export function ReceivingReviewPage({
  batch,
  session: _session,
  latestReceiptDigest = null,
  onAttest,
}: ReceivingReviewProps) {
  if (!batch) {
    return (
      <div style={{ maxWidth: 840, margin: "24px auto", padding: "0 16px" }}>
        <PageHeader
          title="采购到货清点复核"
          eyebrow="PURCHASING / RECEIVING REVIEW"
          source="Android 现场端扫码清点观察草稿"
          statusKind="waiting"
        />
        <EmptyState
          type="empty"
          title="暂无待复核的到货清点批次"
          description="现场到货清点数据由店员在 Android 现场端扫码录入并提交后，将在此处由仓管或店长对照正式采购/到货单进行人工逐行核对。"
        />
      </div>
    );
  }
  const [kind, setKind] = useState<PurchaseDocumentClaim["document_kind"]>("purchase_order");
  const [evidenceType, setEvidenceType] = useState<PurchaseDocumentClaim["evidence_type"]>("erp_screen_view");
  const [documentNo, setDocumentNo] = useState("");
  const [proof, setProof] = useState("");
  const [observedAt, setObservedAt] = useState("");
  const [total, setTotal] = useState("");
  const [note, setNote] = useState("");
  const [accepted, setAccepted] = useState(false);
  const [lines, setLines] = useState<Record<string, {
    quantity: string; unitPrice: string; lineTotal: string; proof: string;
  }>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pendingKey = useRef<string | null>(null);

  useEffect(() => {
    setLines({}); setAccepted(false); setError("");
    setDocumentNo(""); setProof(""); setTotal(""); setNote(""); setObservedAt("");
    pendingKey.current = null;
  }, [batch.id, batch.version, latestReceiptDigest]);

  const reviewable = batch.status === "reviewed" &&
    batch.confirmed_by !== null && batch.open_discrepancies === 0 &&
    batch.lines.length > 0 && batch.lines.every(x => Boolean(x.product_id));
  const setField = (key: string, field: "quantity" | "unitPrice" | "lineTotal" | "proof", value: string) => {
    pendingKey.current = null;
    setLines(previous => ({
      ...previous,
      [key]: {
        quantity: previous[key]?.quantity ?? batch.lines.find(x => x.line_key === key)?.observed_quantity ?? "",
        unitPrice: previous[key]?.unitPrice ?? "",
        lineTotal: previous[key]?.lineTotal ?? "",
        proof: previous[key]?.proof ?? "",
        [field]: value,
      },
    }));
  };
  const save = async () => {
    if (!onAttest || !reviewable || busy) return;
    setError("");
    if (!claimedNo.test(documentNo) || !sha.test(proof) ||
        !observedAt || !Number.isFinite(Date.parse(observedAt)) ||
        !/(Z|[+-]\d{2}:\d{2})$/.test(observedAt) ||
        note.trim().length === 0 || !accepted) {
      setError("请完整填写单号、独立凭证 SHA、带时区观察时间、人工核验说明并勾选声明。");
      return;
    }
    const parsed = batch.lines.map(line => {
      const form = lines[line.line_key];
      return {
        line_key: line.line_key, product_id: line.product_id ?? "", unit: line.unit,
        quantity: form?.quantity ?? line.observed_quantity,
        unit_price: form?.unitPrice ? form.unitPrice : null,
        line_total: form?.lineTotal ? form.lineTotal : null,
        source_line_sha256: form?.proof ?? "",
      };
    });
    if (parsed.some(x => !sha.test(x.source_line_sha256) ||
        !decimal.test(x.quantity) || (x.unit_price !== null && !decimal.test(x.unit_price)) ||
        (x.line_total !== null && !decimal.test(x.line_total)))) {
      setError("逐行来源 SHA、数量或单价金额格式不合法；不得用不明来源金额推断正式成本。");
      return;
    }
    const anyPrice = parsed.some(x => x.unit_price !== null || x.line_total !== null);
    const allPrice = parsed.every(x => x.unit_price !== null && x.line_total !== null);
    if (anyPrice !== allPrice || (allPrice ? !decimal.test(total) : total.trim() !== "")) {
      setError("价格证据必须逐行齐全才能提交总金额，否则金额全部留空。");
      return;
    }
    const payload: PurchaseDocumentClaim = {
      document_kind: kind, claimed_document_no: documentNo,
      evidence_type: evidenceType, evidence_sha256: proof,
      source_observed_at: observedAt, currency: "CNY",
      total_amount: allPrice ? total : null,
      lines: parsed, human_checked: true, note: note.trim(),
      previous_receipt_digest: latestReceiptDigest,
    };
    pendingKey.current ??= crypto.randomUUID();
    setBusy(true);
    try {
      await onAttest(batch.id, batch.version, payload, pendingKey.current);
      pendingKey.current = null;
      setAccepted(false);
    } catch {
      setError("人工引用记录未确认保存，请核对当前批次版本和幂等键，禁止盲目重复关联。");
    } finally {
      setBusy(false);
    }
  };
  return <section aria-labelledby="purchase-document-review">
    <h2 id="purchase-document-review">正式采购单号人工对照 · 仅引用，不入库</h2>
    <p>本系统的采购意向、分批观察和管家婆正式单据是三种不同证据。本页不访问管家婆、不下单、不调整库存。</p>
    <p>批次 {batch.id} · 所见版本 {batch.version} · 状态 {batch.status} · 未关闭差异 {batch.open_discrepancies}</p>
    {!reviewable && <p role="alert">请先完成独立人工复核、关闭差异并确认每行商品身份；目前不能关联正式单据。</p>}
    <ul>{batch.discrepancies.map(x => <li key={x.id}>
      差异 {x.kind} · 人审 {x.latest_decision ?? "尚未裁决"}
    </li>)}</ul>
    <fieldset disabled={!reviewable || busy}>
      <legend>独立查看管家婆屏幕或正式纸质单后的人工声明</legend>
      <label>单据类别 <select value={kind} onChange={e => { pendingKey.current = null; setKind(e.target.value as typeof kind); }}>
        <option value="purchase_order">采购单号（不代表已入库）</option>
        <option value="goods_receipt">收货单号（不代表库存已过账）</option>
      </select></label>
      <label>人工见到的单号 <input value={documentNo} maxLength={80} onChange={e => { pendingKey.current = null; setDocumentNo(e.target.value); }} /></label>
      <label>独立证据来源 <select value={evidenceType} onChange={e => { pendingKey.current = null; setEvidenceType(e.target.value as typeof evidenceType); }}>
        <option value="erp_screen_view">本人独立查看 ERP 屏幕</option>
        <option value="erp_paper_document">本人独立对照正式打印单</option>
      </select></label>
      <label>单据独立凭据 SHA256 <input value={proof} maxLength={64} onChange={e => { pendingKey.current = null; setProof(e.target.value); }} /></label>
      <label>实际观察时间（ISO8601 含时区）<input value={observedAt} placeholder="2026-10-26T10:00:00+08:00" onChange={e => { pendingKey.current = null; setObservedAt(e.target.value); }} /></label>
      <table><thead><tr><th>本批行</th><th>商品</th><th>单位</th><th>实收量（人工对照）</th><th>单价（无凭据留空）</th><th>金额（无凭据留空）</th><th>单据该行凭据 SHA256</th></tr></thead>
        <tbody>{batch.lines.map(line => <tr key={line.line_key}>
          <td>{line.line_key}</td><td>{line.product_id ?? "未知"}</td><td>{line.unit}</td>
          <td><input aria-label={"本批行 " + line.line_key + " 数量"} value={lines[line.line_key]?.quantity ?? line.observed_quantity} onChange={e => setField(line.line_key, "quantity", e.target.value)} /></td>
          <td><input aria-label={"本批行 " + line.line_key + " 单价"} value={lines[line.line_key]?.unitPrice ?? ""} onChange={e => setField(line.line_key, "unitPrice", e.target.value)} /></td>
          <td><input aria-label={"本批行 " + line.line_key + " 金额"} value={lines[line.line_key]?.lineTotal ?? ""} onChange={e => setField(line.line_key, "lineTotal", e.target.value)} /></td>
          <td><input aria-label={"本批行 " + line.line_key + " 来源 SHA"} value={lines[line.line_key]?.proof ?? ""} maxLength={64} onChange={e => setField(line.line_key, "proof", e.target.value)} /></td>
        </tr>)}</tbody>
      </table>
      <label>单据总金额（CNY；无正式单价来源请留空）
        <input value={total} onChange={e => { pendingKey.current = null; setTotal(e.target.value); }} /></label>
      <label>独立人工核验说明 <textarea rows={3} value={note} maxLength={1000} onChange={e => { pendingKey.current = null; setNote(e.target.value); }} /></label>
      <label><input type="checkbox" checked={accepted} onChange={e => { pendingKey.current = null; setAccepted(e.target.checked); }} />
        我确认逐行人工对照了独立来源；知悉这不是经上游认证的正式入库凭证</label>
      <button type="button" disabled={!onAttest || busy || !accepted} onClick={() => void save()}>
        {onAttest ? "提交人工关联收据（非 ERP 入库）" : "等待授权宿主接线，当前只供核对"}
      </button>
    </fieldset>
    <p><strong>上游真实性未独立核验；ERP 已入库：否；账面库存变化：无。</strong></p>
    {error && <p role="alert">{error}</p>}
  </section>;
}
