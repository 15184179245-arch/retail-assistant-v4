import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion } from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";

interface Line { id: string; original_description: string; product_id: string | null; product_version: number | null; quantity: string | null; confirmed_by: string | null }
interface Intent { id: string; version: number; status: "awaiting_arrival"; evidence: { supplier_name: string; source_kind: string; original_text: string; content_sha256: string }; lines: Line[] }
function errorText(e: unknown): string { return e instanceof ApiError ? e.userMessage : "请求失败，请检查网络后重试。"; }

function ConfirmLine({ intent, line, refresh }: { intent: Intent; line: Line; refresh: () => Promise<void> }) {
  const [productId, setProductId] = useState("");
  const [productVersion, setProductVersion] = useState("");
  const [quantity, setQuantity] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const key = useRef<string | null>(null);
  const invalidate = () => { key.current = null; };
  async function confirm() {
    if (busy || !productId.trim() || !productVersion || !quantity) return;
    setBusy(true); setError("");
    const scope = getScopeVersion();
    key.current ??= crypto.randomUUID();
    try {
      await authorizedRequest("/api/v1/purchasing/intents/" + encodeURIComponent(intent.id) +
        "/lines/" + encodeURIComponent(line.id) + "/confirm", {
        method: "POST", headers: { "Idempotency-Key": key.current },
        body: { expected_version: intent.version, product_id: productId.trim(),
                product_version: Number(productVersion), quantity },
      });
      if (scope !== getScopeVersion()) return;
      key.current = null;
      await refresh();
    } catch (e) { if (scope === getScopeVersion()) setError(errorText(e)); }
    finally { setBusy(false); }
  }
  if (line.confirmed_by) return <p>员工已确认：商品 {line.product_id} · 数量 {line.quantity} · 商品版本 {line.product_version}</p>;
  return <div className="login-form">
    <label className="field">商品 ID（人工核对）<input value={productId} onChange={e => { invalidate(); setProductId(e.target.value); }} /></label>
    <label className="field">所见商品版本<input type="number" min="1" value={productVersion} onChange={e => { invalidate(); setProductVersion(e.target.value); }} /></label>
    <label className="field">确认数量<input type="number" min="0.001" step="0.001" value={quantity} onChange={e => { invalidate(); setQuantity(e.target.value); }} /></label>
    <button className="login-button" type="button" disabled={busy} onClick={() => void confirm()}>确认商品和数量（不入库）</button>
    {error ? <p className="login-error" role="alert">{error}</p> : null}
  </div>;
}

export function PurchaseIntentPage({ session }: { session: Session }) {
  const [source, setSource] = useState<"wechat_paste" | "delivery_note_text">("wechat_paste");
  const [supplier, setSupplier] = useState("");
  const [original, setOriginal] = useState("");
  const [descriptions, setDescriptions] = useState("");
  const [consent, setConsent] = useState(false);
  const [items, setItems] = useState<Intent[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const key = useRef<string | null>(null);
  const invalidate = () => { key.current = null; };
  async function refresh() {
    const scope = getScopeVersion();
    const rows = await authorizedRequest<Intent[]>("/api/v1/purchasing/intents");
    if (scope === getScopeVersion()) setItems(rows);
  }
  useEffect(() => {
    setItems([]); setError(""); key.current = null;
    refresh().catch(e => setError(errorText(e)));
  }, [session]);
  async function readText(file?: File) {
    if (!file) return;
    if (file.size > 20000 || !(/\.txt$/i.test(file.name) || file.type === "text/plain")) {
      setError("仅支持不超过 20KB 的授权纯文本文件。"); return;
    }
    const scope = getScopeVersion();
    const text = await file.text();
    if (scope !== getScopeVersion()) return;
    if (text.length > 10000) { setError("文字不得超过 10000 字。"); return; }
    invalidate(); setOriginal(text); setError("");
  }
  async function create() {
    if (busy || !consent) return;
    setBusy(true); setError("");
    const scope = getScopeVersion();
    key.current ??= crypto.randomUUID();
    try {
      await authorizedRequest("/api/v1/purchasing/intents", {
        method: "POST", headers: { "Idempotency-Key": key.current },
        body: { source_kind: source, supplier_name: supplier, original_text: original,
                consent_confirmed: consent,
                line_descriptions: descriptions.split("\n").map(s => s.trim()).filter(Boolean) },
      });
      if (scope !== getScopeVersion()) return;
      key.current = null; setOriginal(""); setDescriptions(""); setConsent(false);
      await refresh();
    } catch (e) { if (scope === getScopeVersion()) setError(errorText(e)); }
    finally { setBusy(false); }
  }
  return <section className="welcome-card" style={{ maxWidth: 960 }} aria-labelledby="purchase-title">
    <p className="eyebrow">管理端 · 人工待到货</p>
    <h2 id="purchase-title">微信订货证据与待到货清单</h2>
    <p className="welcome-copy">仅导入已获授权保存的文字材料；不连接微信、不自动生成采购单或入库。原文仅对获权员工可见。</p>
    <div className="login-form" style={{ maxWidth: 640 }}>
      <label className="field">材料来源<select value={source} onChange={e => { invalidate(); setSource(e.target.value as typeof source); }}>
        <option value="wechat_paste">授权微信记录（手工粘贴）</option><option value="delivery_note_text">送货凭据（文本）</option>
      </select></label>
      <label className="field">供应商<input maxLength={200} value={supplier} onChange={e => { invalidate(); setSupplier(e.target.value); }} /></label>
      <label className="field">原始凭据（保留原文）<textarea rows={5} maxLength={10000} value={original} onChange={e => { invalidate(); setOriginal(e.target.value); }} /></label>
      <label className="field">或选取本地授权 .txt 文件（不自动上传）<input type="file" accept=".txt,text/plain" onChange={e => void readText(e.target.files?.[0])} /></label>
      <label className="field">待核对品名（每行一项，不自动抽取）<textarea rows={4} value={descriptions} onChange={e => { invalidate(); setDescriptions(e.target.value); }} /></label>
      <label><input type="checkbox" checked={consent} onChange={e => { invalidate(); setConsent(e.target.checked); }} /> 我确认有权导入此材料，且知悉原文会保存于当前门店。</label>
      <button type="button" className="login-button" disabled={busy || !consent} onClick={() => void create()}>保存待到货意向（不下单）</button>
    </div>
    {error ? <p className="login-error" role="alert">{error}</p> : null}
    <h3>当前门店待到货意向（最多 100 条）</h3>
    {items.length === 0 ? <p>暂无待到货记录，或尚未获得读取权限。</p> : <ul>
      {items.map(intent => <li key={intent.id} style={{ marginBottom: 28 }}>
        <strong>{intent.evidence.supplier_name}</strong> · {intent.status} · 意向版本 {intent.version}
        <p>来源：{intent.evidence.source_kind} · SHA256：{intent.evidence.content_sha256}</p>
        <details><summary>查看授权凭据原文</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{intent.evidence.original_text}</pre></details>
        <ul>{intent.lines.map(line => <li key={line.id}>
          <p>待核对：{line.original_description}</p>
          <ConfirmLine intent={intent} line={line} refresh={refresh} />
        </li>)}</ul>
      </li>)}
    </ul>}
  </section>;
}
