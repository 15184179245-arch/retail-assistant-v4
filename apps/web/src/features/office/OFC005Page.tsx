import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion, getSession } from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";
import type { OfficeEvidenceRow } from "./OFC002Page";

interface Line { line_ref: string; description: string; unit: string; quantity: string; unit_price: string }
export interface OfficeDocument {
  id: string; document_key: string; version: number; document_type: string;
  evidence_id: string; evidence_sha256: string; content_sha256: string;
  contents: { title: string; lines: (Line & { line_total: string })[] };
  total_amount: string; currency: string; state: string; submitted_by: string;
  superseded: boolean; exports: { format: string; sha256: string; byte_size: number }[];
}
interface ExportReceipt {
  document: OfficeDocument; filename: string; content_base64: string;
  byte_size: number; content_sha256: string; warning: string | null;
}
const base = "/api/v1/office-documents";
const blank = (): Line => ({ line_ref: "1", description: "", unit: "件", quantity: "1", unit_price: "0.00" });

// Persistent markers contain only opaque identifiers and digests, never the
// token or document body. A marker survives refresh and is scoped to an employee.
type Action = "create" | "review" | "export";
interface Marker { key: string; action: Action; body_sha256: string; target_sha256: string }
const prefix = "retail.office.pending.v1.";
async function digest(value: string): Promise<string> {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))),
    x => x.toString(16).padStart(2, "0")).join("");
}
function commandAction(path: string): Action {
  if (path === base) return "create";
  if (path.startsWith(base + "/") && path.endsWith("/review")) return "review";
  if (path.startsWith(base + "/") && path.endsWith("/export")) return "export";
  throw new Error("无法识别操作，已停止提交");
}
function validMarker(value: unknown): value is Marker {
  if (!value || typeof value !== "object") return false;
  const x = value as Record<string, unknown>;
  return typeof x.key === "string" && /^[0-9a-f-]{36}$/.test(x.key) &&
    (x.action === "create" || x.action === "review" || x.action === "export") &&
    typeof x.body_sha256 === "string" && /^[0-9a-f]{64}$/.test(x.body_sha256) &&
    typeof x.target_sha256 === "string" && /^[0-9a-f]{64}$/.test(x.target_sha256);
}
function validReceipt(action: Action, value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const x = value as Record<string, unknown>;
  const document = action === "export" ? x.document : x;
  if (!document || typeof document !== "object") return false;
  const d = document as Record<string, unknown>;
  return typeof d.id === "string" && d.id.length > 0 && Number.isInteger(d.version) && (d.version as number) > 0 &&
    typeof d.content_sha256 === "string" && /^[0-9a-f]{64}$/.test(d.content_sha256) &&
    typeof d.superseded === "boolean" && Array.isArray(d.exports) && (action !== "export" ||
      (typeof x.content_base64 === "string" && typeof x.content_sha256 === "string" && /^[0-9a-f]{64}$/.test(x.content_sha256) &&
        Number.isInteger(x.byte_size) && (x.byte_size as number) > 0));
}
export class DocumentCommandGate {
  pending = false;
  unknown = false;
  private namespace = "";
  private markerKeys(): string[] {
    const store = globalThis.localStorage;
    const keys: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key?.startsWith(this.namespace + ".")) keys.push(key);
    }
    return keys;
  }
  async hydrate(scope: number): Promise<void> {
    const identity = getSession();
    if (!identity || scope !== getScopeVersion()) throw new Error("登录身份已变化");
    const hash = await digest(JSON.stringify([identity.organizationId, identity.storeId, identity.subjectId]));
    if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
    this.namespace = prefix + hash;
    try { this.unknown = this.markerKeys().length > 0; }
    catch { throw new Error("无法保存操作状态，请检查浏览器存储后重试"); }
  }
  private async exclusive<T>(scope: number, work: () => Promise<T>): Promise<T> {
    const identity = getSession();
    if (!identity || scope !== getScopeVersion()) throw new Error("登录身份已变化");
    const name = prefix + await digest(JSON.stringify([identity.organizationId, identity.storeId, identity.subjectId]));
    if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
    if (!globalThis.navigator?.locks?.request) throw new Error("浏览器不支持跨页面操作锁，请更换浏览器");
    return globalThis.navigator.locks.request(name, { ifAvailable: true }, async lock => {
      if (!lock) throw new Error("另一页面正在处理操作，请回查后再试");
      if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
      return work();
    });
  }
  async run<T>(scope: number, path: string, body: unknown, baseUrl = ""): Promise<T> {
    if (scope !== getScopeVersion() || this.pending || this.unknown) throw new Error("操作未完成，请先回查记录并联系负责人核实");
    this.pending = true;
    let markerKey = "";
    try {
      return await this.exclusive(scope, async () => {
        await this.hydrate(scope);
        if (this.unknown) throw new Error("操作未完成，请先回查记录并联系负责人核实");
        const action = commandAction(path);
        const key = crypto.randomUUID();
        const marker: Marker = { key, action, body_sha256: await digest(JSON.stringify(body)), target_sha256: await digest(path) };
        if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
        markerKey = this.namespace + "." + key;
        // If persistence fails, never send the command.
        try { globalThis.localStorage.setItem(markerKey, JSON.stringify(marker)); }
        catch { markerKey = ""; throw new Error("无法保存操作状态，请检查浏览器存储后重试"); }
        try {
          const result = await authorizedRequest<T>(path, { method: "POST", body, baseUrl,
            headers: { "Idempotency-Key": key } });
          if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
          if (!validReceipt(action, result)) throw new Error("服务端结果不完整，请回查记录");
          globalThis.localStorage.removeItem(markerKey); markerKey = "";
          return result;
        } catch (error) {
          if (markerKey) {
            // A definite rejection is clearable only while this employee lock is held.
            const uncertain = scope !== getScopeVersion() || !(error instanceof ApiError) || error.status === 0 ||
              error.status === 408 || error.status >= 500 || error.code === "INVALID_ENVELOPE";
            if (!uncertain) {
              try { globalThis.localStorage.removeItem(markerKey); markerKey = ""; }
              catch { /* retain the marker and fail closed */ }
            }
            if (markerKey) this.unknown = true;
          }
          throw error;
        }
      });
    } finally { this.pending = false; }
  }
  async reconcile(scope: number, baseUrl = ""): Promise<void> {
    if (this.pending || scope !== getScopeVersion()) throw new Error("操作进行中或登录身份已变化");
    this.pending = true;
    try {
      await this.exclusive(scope, async () => {
      await this.hydrate(scope);
      for (const storageKey of this.markerKeys()) {
        let marker: unknown;
        try { marker = JSON.parse(globalThis.localStorage.getItem(storageKey) ?? "null"); }
        catch { continue; }
        if (!validMarker(marker) || storageKey !== this.namespace + "." + marker.key) continue;
        const result = await authorizedRequest<{ status: string }>(base + "/commands/status?action=" + marker.action,
          { baseUrl, headers: { "Idempotency-Key": marker.key } });
        if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
        if (result?.status === "committed") globalThis.localStorage.removeItem(storageKey);
      }
      this.unknown = this.markerKeys().length > 0;
      });
    } finally { this.pending = false; }
  }
}
let gateScope = -1;
let scopeGate = new DocumentCommandGate();
function gateFor(scope: number) {
  if (gateScope !== scope) { gateScope = scope; scopeGate = new DocumentCommandGate(); }
  return scopeGate;
}
export function exportAction(row: OfficeDocument, format: "pdf" | "xlsx"): "read" | "create" | null {
  if (typeof row.superseded !== "boolean" || !Array.isArray(row.exports)) return null;
  if (row.exports.some(receipt => receipt.format === format)) return "read";
  if (!row.superseded && ["approved", "exported"].includes(row.state)) return "create";
  return null;
}
export function documentVersion(row: OfficeDocument) {
  return { expected_version: row.version, content_sha256: row.content_sha256 };
}
export async function verifyExport(receipt: ExportReceipt): Promise<Uint8Array<ArrayBuffer>> {
  const bytes = Uint8Array.from(atob(receipt.content_base64), c => c.charCodeAt(0));
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), x => x.toString(16).padStart(2, "0")).join("");
  if (bytes.length !== receipt.byte_size || hash !== receipt.content_sha256) throw new Error("导出文件校验失败，禁止下载");
  return bytes;
}

export function OFC005Page({ session, baseUrl = "", initialItems = [] }: {
  session: Session | null; baseUrl?: string; initialItems?: OfficeDocument[];
}) {
  // Parent keys this page by auth scope; old drafts disappear on the same render.
  const scope = getScopeVersion();
  const mounted = useRef(true);
  const gate = gateFor(scope);
  const [items, setItems] = useState(initialItems);
  const [sources, setSources] = useState<OfficeEvidenceRow[]>([]);
  const [selected, setSelected] = useState<OfficeDocument | null>(null);
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("quotation");
  const [currency, setCurrency] = useState("CNY");
  const [source, setSource] = useState("");
  const [lines, setLines] = useState<Line[]>([blank()]);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [gateReady, setGateReady] = useState(false);
  const [message, setMessage] = useState("");
  const current = () => mounted.current && scope === getScopeVersion();
  const showError = (e: unknown) => { if (current()) setMessage(e instanceof ApiError ? e.userMessage : e instanceof Error ? e.message : "请求失败"); };
  async function refresh() {
    const rows = await authorizedRequest<{ items: OfficeDocument[] }>(base, { baseUrl });
    if (current()) { setItems(rows.items); setSelected(old => rows.items.find(x => x.id === old?.id) ?? null); }
  }
  useEffect(() => {
    mounted.current = true;
    if (session) {
      setGateReady(false);
      void gate.hydrate(scope).then(() => { if (current()) setGateReady(true); }).catch(showError);
      void refresh().catch(showError);
      void authorizedRequest<{ items: OfficeEvidenceRow[] }>("/api/v1/office-intake/evidence?limit=50", { baseUrl })
        .then(x => { if (current()) setSources(x.items); }).catch(showError);
    }
    return () => { mounted.current = false; };
  }, [scope, baseUrl]);
  async function command<T>(path: string, body: unknown, done: (result: T) => Promise<void> | void) {
    setBusy(true); setMessage("");
    try {
      const result = await gate.run<T>(scope, path, body, baseUrl);
      if (!current()) return;
      await done(result);
      if (current()) await refresh();
    } catch (e) { showError(e); }
    finally { if (current()) setBusy(false); }
  }
  function loadVersion(row: OfficeDocument) {
    setSelected(row); setKey(row.document_key); setTitle(row.contents.title);
    setKind(row.document_type); setCurrency(row.currency); setSource(row.evidence_id);
    setLines(row.contents.lines.map(({ line_ref, description, unit, quantity, unit_price }) => ({ line_ref, description, unit, quantity, unit_price })));
    setReason("");
  }
  async function download(receipt: ExportReceipt) {
    const bytes = await verifyExport(receipt);
    if (!current()) return;
    const url = URL.createObjectURL(new Blob([bytes]));
    const a = document.createElement("a"); a.href = url; a.download = receipt.filename;
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    setMessage(receipt.warning ?? "文件已校验并下载；仍为非正式参考文档");
  }
  async function readExport(row: OfficeDocument, fileFormat: "pdf" | "xlsx") {
    setBusy(true); setMessage("");
    try {
      const receipt = await authorizedRequest<ExportReceipt>(`${base}/${encodeURIComponent(row.id)}/exports/${fileFormat}`, { baseUrl });
      if (current()) await download(receipt);
    } catch (e) { showError(e); }
    finally { if (current()) setBusy(false); }
  }
  async function reconcilePending() {
    setBusy(true); setMessage("");
    try {
      await gate.reconcile(scope, baseUrl);
      if (current()) setMessage(gate.unknown ? "仍有未知结果，请联系负责人核实，勿重复提交。" : "回查完成，当前没有待回查操作，可以继续工作。");
      if (current()) await refresh();
    } catch (e) { showError(e); }
    finally { if (current()) setBusy(false); }
  }
  if (!session) return <p>请先登录员工账号</p>;
  const locked = busy || !gateReady || gate.pending || gate.unknown;
  const sourceRow = sources.find(x => x.id === source);
  return <section aria-label="办公文档审核导出">
    <h2>办公文档</h2><p>报价、对账与到货参考草稿。原件、金额和规格须人工核对，由另一位有权限员工审核后导出；不生成管家婆正式单据。</p>
    {message && <p role="status">{message}</p>}
    {gate.unknown && <div role="alert"><p>上次操作结果未知，已暂停写入。请回查记录并联系负责人核实，勿重复提交。</p><button type="button" disabled={busy} onClick={() => void reconcilePending()}>回查已登记的操作</button></div>}
    <button type="button" disabled={busy} onClick={() => void refresh().catch(showError)}>回查记录</button>
    <ul>{items.map(row => <li key={row.id}><button type="button" disabled={busy} onClick={() => loadVersion(row)}>{row.contents.title} · {row.document_key} v{row.version} · {row.state}{row.superseded ? " · 已被新版本替代" : ""}</button></li>)}</ul>
    <form onSubmit={e => {
      e.preventDefault(); if (!sourceRow) return;
      void command<OfficeDocument>(base, { document_key: key, expected_version: selected?.document_key === key ? selected.version : 0,
        document_type: kind, evidence_id: sourceRow.id, evidence_sha256: sourceRow.sha256, currency, title, lines },
        row => { loadVersion(row); setMessage("新版本已保存，等待独立审核"); });
    }}>
      <fieldset disabled={locked}><legend>{selected ? "编辑后另存新版本" : "建立草稿"}</legend>
        <label>文档编号 <input required maxLength={120} value={key} onChange={e => setKey(e.target.value)} /></label>
        <label>标题 <input required maxLength={120} value={title} onChange={e => setTitle(e.target.value)} /></label>
        <label>类型 <select value={kind} onChange={e => setKind(e.target.value)}><option value="quotation">报价参考</option><option value="reconciliation">对账参考</option><option value="receiving_list">到货清单</option></select></label>
        <label>币种 <select value={currency} onChange={e => setCurrency(e.target.value)}><option>CNY</option><option>USD</option></select></label>
        <label>原始材料 <select required value={source} onChange={e => setSource(e.target.value)}><option value="">请从材料收件箱选择</option>{sources.map(x => <option key={x.id} value={x.id}>{x.original_name}</option>)}</select></label>
        {lines.map((line, index) => <fieldset key={index}><legend>明细 {index + 1}</legend>{([
          ["line_ref", "明细编号", 80], ["description", "商品与完整规格", 150], ["unit", "单位", 24], ["quantity", "数量", 24], ["unit_price", "单价", 24],
        ] as const).map(([field, label, max]) => <label key={field}>{label} <input required maxLength={max} value={line[field]} onChange={e => setLines(old => old.map((x, i) => i === index ? { ...x, [field]: e.target.value } : x))} /></label>)}
          <button type="button" disabled={lines.length === 1} onClick={() => setLines(old => old.filter((_, i) => i !== index))}>移除明细</button></fieldset>)}
        <button type="button" disabled={lines.length >= 60} onClick={() => setLines(old => [...old, { ...blank(), line_ref: crypto.randomUUID() }])}>添加明细</button>
        <button type="submit" disabled={!sourceRow}>保存待审核版本</button>
      </fieldset>
    </form>
    {selected && <section aria-label="已保存版本核对"><h3>核对已保存版本 v{selected.version}</h3>
      <p>以下为已保存内容；上方编辑须保存为新版本后才会进入审核。</p>
      <p>{selected.contents.title} · 合计 {selected.total_amount} {selected.currency} · {selected.state}</p>
      <p>来源 {selected.evidence_id} · 内容摘要 {selected.content_sha256}</p>
      <table><thead><tr><th>编号</th><th>完整规格</th><th>单位</th><th>数量</th><th>单价</th><th>金额</th></tr></thead><tbody>{selected.contents.lines.map((l, i) => <tr key={i}><td>{l.line_ref}</td><td>{l.description}</td><td>{l.unit}</td><td>{l.quantity}</td><td>{l.unit_price}</td><td>{l.line_total}</td></tr>)}</tbody></table>
      <label>审核依据 <input value={reason} maxLength={1000} disabled={locked} onChange={e => setReason(e.target.value)} /></label>
      {(["approved", "rejected"] as const).map(decision => <button key={decision} disabled={locked || selected.state !== "draft" || selected.superseded !== false || selected.submitted_by === session.subjectId || !reason.trim()} onClick={() => void command<OfficeDocument>(`${base}/${encodeURIComponent(selected.id)}/review`, { ...documentVersion(selected), decision, reason }, row => { setSelected(row); setMessage("审核结果已记录"); })}>{decision === "approved" ? "确认核对并批准" : "退回修改"}</button>)}
      {(["pdf", "xlsx"] as const).map(file_format => {
        const action = exportAction(selected, file_format);
        return <button key={file_format} disabled={busy || !action || (action === "create" && locked)} onClick={() => {
          if (action === "read") void readExport(selected, file_format);
          else if (action === "create") void command<ExportReceipt>(`${base}/${encodeURIComponent(selected.id)}/export`, { ...documentVersion(selected), file_format }, download);
        }}>{action === "read" ? "下载已导出" : "导出"} {file_format.toUpperCase()}</button>;
      })}
    </section>}
  </section>;
}
