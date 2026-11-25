import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion, subscribe } from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";
import { getCapabilityEpoch, hasCapability, subscribeCapabilities, useCapabilities } from "../../lib/auth/capabilities";
import { businessPermission } from "../../lib/auth/businessPermissions";
import { matchesPurchaseReceipt, purchaseFingerprint, purchasePendingJournal, type PurchaseIntentBody } from "./purchaseIntentRecovery";

interface Line { id: string; original_description: string; product_id: string | null; product_version: number | null; quantity: string | null; confirmed_by: string | null }
interface Intent { id: string; version: number; status: "awaiting_arrival"; evidence: { supplier_name: string; source_kind: string; original_text: string; content_sha256: string }; lines: Line[] }
function errorText(e: unknown): string { return e instanceof ApiError ? e.userMessage : "请求失败，请检查网络后重试。"; }

function ConfirmLine({ intent, line, refresh, canWrite }: { intent: Intent; line: Line; refresh: () => Promise<void>; canWrite: boolean }) {
  const [productId, setProductId] = useState("");
  const [productVersion, setProductVersion] = useState("");
  const [quantity, setQuantity] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const key = useRef<string | null>(null);
  const invalidate = () => { key.current = null; };
  async function confirm() {
    if (!hasCapability(businessPermission.purchasing.write) || busy || !productId.trim() || !productVersion || !quantity) return;
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
  if (!canWrite) return <p>尚未确认商品和数量。</p>;
  return <div className="login-form">
    <label className="field">商品 ID（人工核对）<input value={productId} onChange={e => { invalidate(); setProductId(e.target.value); }} /></label>
    <label className="field">所见商品版本<input type="number" min="1" value={productVersion} onChange={e => { invalidate(); setProductVersion(e.target.value); }} /></label>
    <label className="field">确认数量<input type="number" min="0.001" step="0.001" value={quantity} onChange={e => { invalidate(); setQuantity(e.target.value); }} /></label>
    <button className="login-button" type="button" disabled={busy} onClick={() => void confirm()}>确认商品和数量（不入库）</button>
    {error ? <p className="login-error" role="alert">{error}</p> : null}
  </div>;
}

type PurchaseDraft = {
  revision: number;
  source: "wechat_paste" | "delivery_note_text";
  supplier: string;
  original: string;
  descriptions: string;
  consent: boolean;
  fileReading: boolean;
};
const emptyDraft: PurchaseDraft = {
  revision: 0, source: "wechat_paste", supplier: "", original: "", descriptions: "", consent: false, fileReading: false,
};

export function PurchaseIntentPage({ session }: { session: Session }) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  return <PurchaseIntentWorkspace key={scope} scope={scope} session={session} />;
}

function PurchaseIntentWorkspace({ session, scope }: { session: Session; scope: number }) {
  const authority = useCapabilities();
  const authorityEpoch = useSyncExternalStore(subscribeCapabilities, getCapabilityEpoch, getCapabilityEpoch);
  const canRead = authority.hasCapability(businessPermission.purchasing.read);
  const canWrite = authority.hasCapability(businessPermission.purchasing.write);
  const [draft, setDraft] = useState(emptyDraft);
  // Synchronous revision fence, including edits before React commits the new DOM.
  const latestDraft = useRef(draft);
  const mounted = useRef(false);
  const [list, setList] = useState<{ epoch: number; items: Intent[]; error: string } | null>(null);
  const items = canRead && list?.epoch === authorityEpoch ? list.items : [];
  const listError = canRead && list?.epoch === authorityEpoch ? list.error : "";
  const [error, setError] = useState("");
  const [fileError, setFileError] = useState("");
  const [saveNotice, setSaveNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const latestRead = useRef<AbortController | null>(null);
  const checked = useRef<{ fingerprint: string; key: string; current: () => boolean } | null>(null);
  const isCurrent = () => mounted.current && getScopeVersion() === scope;
  const underCurrentGrant = () => isCurrent() && getCapabilityEpoch() === authorityEpoch;
  const canEdit = () => underCurrentGrant() && hasCapability(businessPermission.purchasing.write);

  function nextDraft(fields: Partial<Omit<PurchaseDraft, "revision">>) {
    const next = { ...latestDraft.current, fileReading: false, ...fields, revision: latestDraft.current.revision + 1 };
    latestDraft.current = next;
    setDraft(next);
    return next;
  }
  function edit(fields: Partial<Pick<PurchaseDraft, "source" | "supplier" | "original" | "descriptions">>) {
    if (!canEdit()) return;
    nextDraft({ ...fields, consent: false });
    setFileError("");
  }

  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useLayoutEffect(() => subscribeCapabilities(() => {
    // A grant transition invalidates consent even if React batches it away.
    if (latestDraft.current.fileReading || latestDraft.current.consent) nextDraft({ consent: false });
    latestRead.current?.abort(); latestRead.current = null; checked.current = null;
    setFileError(""); setError(""); setSaveNotice("");
  }), []);

  async function refresh() {
    if (!underCurrentGrant() || !hasCapability(businessPermission.purchasing.read)) return null;
    latestRead.current?.abort();
    const request = new AbortController();
    latestRead.current = request;
    const current = () => latestRead.current === request && !request.signal.aborted && underCurrentGrant() &&
      hasCapability(businessPermission.purchasing.read);
    try {
      const rows = await authorizedRequest<Intent[]>("/api/v1/purchasing/intents", { signal: request.signal, cache: "no-store" });
      if (!current()) return null;
      if (!Array.isArray(rows)) throw Error("Unverified purchasing list");
      setList({ epoch: authorityEpoch, items: rows, error: "" });
      return { rows, current };
    } catch (e) {
      if (current()) setList({ epoch: authorityEpoch, items: [], error: errorText(e) });
      return null;
    }
  }
  useEffect(() => {
    void refresh();
    return () => { latestRead.current?.abort(); latestRead.current = null; };
  }, [session, authorityEpoch]);

  async function readText(file?: File) {
    if (!file || !canEdit()) return;
    const selected = nextDraft({ fileReading: true, consent: false });
    setFileError("");
    const ownsRead = () => canEdit() && latestDraft.current === selected;
    const fail = (message: string) => {
      if (!ownsRead()) return;
      nextDraft({});
      setFileError(message);
    };
    if (file.size > 20000 || !(/\.txt$/i.test(file.name) || file.type === "text/plain")) {
      fail("仅支持不超过 20KB 的授权纯文本文件。"); return;
    }
    try {
      const text = await file.text();
      if (!ownsRead()) return;
      if (text.length > 10000) { fail("文字不得超过 10000 字。"); return; }
      nextDraft({ original: text });
    } catch {
      fail("无法读取文本文件，原始凭据未改变。可重新选择或手工粘贴。");
    }
  }

  async function create() {
    const submitted = draft;
    const matchesDraft = () => canEdit() && latestDraft.current === submitted;
    if (!matchesDraft() || submitting.current || submitted.fileReading || !submitted.consent) return;
    submitting.current = true;
    setBusy(true); setError(""); setSaveNotice("");
    const body: PurchaseIntentBody = {
      source_kind: submitted.source, supplier_name: submitted.supplier, original_text: submitted.original,
      consent_confirmed: true,
      line_descriptions: submitted.descriptions.split("\n").map(s => s.trim()).filter(Boolean),
    };
    try {
      let fingerprint: string, journal: ReturnType<typeof purchasePendingJournal>, attempt: ReturnType<typeof journal.reserve>;
      try {
        fingerprint = await purchaseFingerprint(body);
        if (!matchesDraft()) return;
        journal = purchasePendingJournal(globalThis.sessionStorage, session);
        attempt = journal.reserve(fingerprint);
      } catch {
        if (canEdit()) setError("无法保存本标签的核对标识，本次没有发送。请先核对待到货清单。");
        return;
      }

      async function checkOriginal() {
        const proof = await refresh();
        if (!matchesDraft()) return;
        if (!proof?.current()) {
          setError("上次保存尚未确认，未能核对清单，因此没有再次提交。原稿和编号保留。");
          return;
        }
        checked.current = { fingerprint, key: attempt.key, current: proof.current };
        const possible = proof.rows.some(row => matchesPurchaseReceipt(row, body, session));
        setSaveNotice((possible ? "清单有匹配记录，但它不是原请求的回执。" : "最近的清单未能确认原请求。") +
          "再次点击保存会用原编号提交同一份内容；若原请求未保存，本次可能保存。");
      }

      if (attempt.existing) {
        const previous = checked.current;
        if (previous?.fingerprint !== fingerprint || previous.key !== attempt.key || !previous.current()) {
          await checkOriginal();
          return;
        }
      }
      checked.current = null;
      let receipt: unknown;
      try {
        receipt = await authorizedRequest("/api/v1/purchasing/intents", {
          method: "POST", headers: { "Idempotency-Key": attempt.key }, body,
        });
      } catch (e) {
        if (!isCurrent()) return;
        const freshRejection = !attempt.existing && e instanceof ApiError && e.status === 422 && e.code === "VALIDATION_ERROR";
        if (freshRejection) {
          try { journal.resolve(fingerprint, attempt.key); } catch { /* Retain the original key if storage fails. */ }
          if (matchesDraft()) setError(errorText(e));
        } else if (matchesDraft()) {
          // Read-only after an uncertain response. Another explicit click is the only way to use the original key again.
          await checkOriginal();
        } else if (canEdit()) {
          setError("上一版保存未确认，当前编辑已保留且未发送。");
        }
        return;
      }
      if (!isCurrent()) return;
      if (!matchesPurchaseReceipt(receipt, body, session)) {
        if (matchesDraft()) await checkOriginal();
        else if (canEdit()) setError("上一版未收到匹配回执，当前编辑已保留且未发送。");
        return;
      }
      try { journal.resolve(fingerprint, attempt.key); } catch { /* The same key remains safe after a verified receipt. */ }
      if (canEdit()) {
        const wasSubmitted = latestDraft.current === submitted;
        if (wasSubmitted) nextDraft({ original: "", descriptions: "", consent: false });
        setSaveNotice(wasSubmitted ? "待到货意向已保存。" : "上一版待到货意向已保存；当前编辑仍未保存。");
      }
      await refresh();
    } finally {
      submitting.current = false;
      if (isCurrent()) setBusy(false);
    }
  }
  return <section className="welcome-card" style={{ maxWidth: 960 }} aria-labelledby="purchase-title">
    <p className="eyebrow">管理端 · 人工待到货</p>
    <h2 id="purchase-title">微信订货证据与待到货清单</h2>
    <p className="welcome-copy">仅导入已获授权保存的文字材料；不连接微信、不自动生成采购单或入库。原文仅对获权员工可见。</p>
    {canWrite ? <div className="login-form" style={{ maxWidth: 640 }}>
      <label className="field">材料来源<select value={draft.source} onChange={e => edit({ source: e.target.value as PurchaseDraft["source"] })}>
        <option value="wechat_paste">授权微信记录（手工粘贴）</option><option value="delivery_note_text">送货凭据（文本）</option>
      </select></label>
      <label className="field">供应商<input maxLength={200} value={draft.supplier} onChange={e => edit({ supplier: e.target.value })} /></label>
      <label className="field">原始凭据（保留原文）<textarea rows={5} maxLength={10000} value={draft.original} onChange={e => edit({ original: e.target.value })} /></label>
      <label className="field">或选取本地授权 .txt 文件（不自动上传）<input type="file" accept=".txt,text/plain" onChange={e => {
        const file = e.currentTarget.files?.[0];
        e.currentTarget.value = "";
        void readText(file);
      }} /></label>
      {draft.fileReading ? <p role="status">正在读取本地文件；此时不会上传。</p> : null}
      {fileError ? <p className="login-error" role="alert">{fileError}</p> : null}
      <label className="field">待核对品名（每行一项，不自动抽取）<textarea rows={4} value={draft.descriptions} onChange={e => edit({ descriptions: e.target.value })} /></label>
      <label><input type="checkbox" checked={draft.consent} disabled={draft.fileReading} onChange={e => {
        if (canEdit() && !latestDraft.current.fileReading && latestDraft.current === draft) nextDraft({ consent: e.target.checked });
      }} /> 我确认有权导入此材料，且知悉原文会保存于当前门店。</label>
      <button type="button" className="login-button" disabled={busy || draft.fileReading || !draft.consent} onClick={() => void create()}>保存待到货意向（不下单）</button>
      {saveNotice ? <p role="status">{saveNotice}</p> : null}
    </div> : <p>当前身份可查看待到货记录，不能导入材料或确认行。</p>}
    {canWrite && error ? <p className="login-error" role="alert">{error}</p> : null}
    {listError ? <p className="login-error" role="alert">{listError}</p> : null}
    <h3>当前门店待到货意向（最多 100 条）</h3>
    {items.length === 0 ? <p>暂无待到货记录，或尚未获得读取权限。</p> : <ul>
      {items.map(intent => <li key={intent.id} style={{ marginBottom: 28 }}>
        <strong>{intent.evidence.supplier_name}</strong> · {intent.status} · 意向版本 {intent.version}
        <p>来源：{intent.evidence.source_kind} · SHA256：{intent.evidence.content_sha256}</p>
        <details><summary>查看授权凭据原文</summary><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{intent.evidence.original_text}</pre></details>
        <ul>{intent.lines.map(line => <li key={line.id}>
          <p>待核对：{line.original_description}</p>
          <ConfirmLine intent={intent} line={line} refresh={async () => { await refresh(); }} canWrite={canWrite} />
        </li>)}</ul>
      </li>)}
    </ul>}
  </section>;
}
