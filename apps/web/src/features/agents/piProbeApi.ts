import { ApiError } from "../../lib/api/client";
import type { Session } from "../../lib/auth/session";
import { authorizedRequest, getSession, getScopeVersion, subscribe } from "../../lib/auth/session";

export const PROBE_TEXT = "软件开发合成验收：请用只读 tasks.list_mine 列出我自己的代码审查待办，并指出 CAMPAIGN-001 是否在列表中；不创建或修改记录。";
export const PROBE_SAMPLE_SHA256 = "fcec3cbc008051fcc71fcee1797b3aca202ade9bcf3b7fa1a07adbcd142e8af6";
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const sha = /^[a-f0-9]{64}$/;
const states = ["created", "running", "failed", "cancelled"];
const stepStates = ["pending", "running", "succeeded", "failed", "unknown", "cancelled"];
const providerStates = ["permitted", "send_intent", "responded", "unknown", "not_sent"];
const ledgerStates = ["reserved", "settled", "unknown", "reconciliation_required", "released"];
type Row = Record<string, unknown>;
function row(value: unknown): Row {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("protocol");
  return value as Row;
}
function member(value: unknown, allowed: string[]): string {
  if (typeof value !== "string" || !allowed.includes(value)) throw new Error("protocol");
  return value;
}
function id(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(value)) throw new Error("protocol");
  return value;
}
function digest(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || !sha.test(value)) throw new Error("protocol");
  return value;
}
function list(value: unknown): unknown[] {
  if (!Array.isArray(value) || value.length > 50) throw new Error("protocol");
  return value;
}
export interface ProbeStep { id: string; sequence: number; kind: string; state: string }
export interface ProbeRun { runId: string; state: string; steps: ProbeStep[] }
export interface ProbeProvider {
  receiptId: string; stepId: string; attemptId: string; ledgerId: string; state: string;
  responseDigest: string | null; messageDigest: string | null; requestIdDigest: string | null;
  usagePresence: string;
}
export interface ProbeRuntime extends ProbeRun { manifest: string; providers: ProbeProvider[] }
export interface ProbeCost { ledgerId: string; state: string; currency: string; reserved: string; estimated: string | null }
export interface Observation<T> { startedAt: string; collectedAt: string; data: T }
export interface ProbeView {
  runId: string; attempted: boolean; busy: boolean; notice: string;
  run?: Observation<ProbeRun>; runtime?: Observation<ProbeRuntime>;
  costs?: Observation<ProbeCost[]>; costNotice?: string;
  costRead?: { startedAt: string; collectedAt: string };
}
export interface ProbeStorage { getItem(key: string): string | null; setItem(key: string, value: string): void }
interface Journal { version: 1; runId: string; attempted: true; sample: string }
function identity(session: Session): string {
  return JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
}
function sameIdentity(a: Session | null, b: Session): boolean {
  return !!a && identity(a) === identity(b) && a.accessToken === b.accessToken;
}
export function probeSessionIsCurrent(session: Session): boolean { return sameIdentity(getSession(), session); }
function checkedRun(value: unknown, runId: string, runtime = false): ProbeRun {
  const data = row(value);
  if (data.run_id !== runId) throw new Error("protocol");
  const steps = list(data.steps).map(value => {
    const step = row(value);
    // H4f never dispatches a tool or produces a business answer.
    if (step.kind !== "model" || !Number.isInteger(step.sequence_no) || (step.sequence_no as number) < 1) throw new Error("protocol");
    return { id: id(runtime ? step.step_id : step.id), sequence: step.sequence_no as number,
      kind: "model", state: member(step.state, stepStates) };
  });
  if (steps.length > 1 || new Set(steps.map(s => s.id)).size !== steps.length) throw new Error("protocol");
  return { runId, state: member(data.state, states), steps };
}
function checkedRuntime(value: unknown, runId: string): ProbeRuntime {
  const data = row(value);
  const run = checkedRun(data, runId, true);
  const launch = row(data.launch), runtime = row(data.runtime);
  if (data.contract_version !== 2 || launch.status !== "recorded" ||
      launch.wire_protocol !== "v4.pi-readonly/2" || launch.tool_profile !== "pi-readonly-v1" ||
      !["host-pi-readonly-node24-linux-online-v1", "host-pi-readonly-node24-darwin-online-v1"].includes(String(launch.launch_profile)) ||
      runtime.status !== "recorded" || runtime.engine !== "@earendil-works/pi-coding-agent" || runtime.version !== "1.4.0" ||
      runtime.execution_proof !== "not_proven" || data.recovery !== "A_stop_only" ||
      data.result_replay !== "unsupported" || data.step_citation_association !== "unsupported" ||
      list(data.native_calls).length !== 0) throw new Error("protocol");
  const manifest = digest(launch.source_manifest_sha256);
  if (!manifest || typeof launch.toolset_version !== "string" || !/^sha256:[a-f0-9]{64}$/.test(launch.toolset_version)) throw new Error("protocol");
  const providers = list(data.provider_calls).map(value => {
    const call = row(value);
    const state = member(call.state, providerStates);
    const responseDigest = digest(call.response_digest), messageDigest = digest(call.message_digest);
    const requestIdDigest = digest(call.provider_request_id_sha256);
    const stepId = id(call.model_step_id);
    if (!run.steps.some(step => step.id === stepId) || (state === "responded" && !responseDigest) ||
        (["permitted", "send_intent"].includes(state) && (responseDigest || messageDigest || call.usage_presence !== null))) throw new Error("protocol");
    let usagePresence = "unknown";
    if (call.usage_presence !== null) {
      const presence = row(call.usage_presence);
      const keys = ["prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"];
      if (Object.keys(presence).length !== 4 || keys.some(key => typeof presence[key] !== "boolean")) throw new Error("protocol");
      usagePresence = keys.map(key => `${key}=${presence[key]}`).join(", ");
    }
    if (typeof call.created_at !== "string" || typeof call.updated_at !== "string" ||
        !Number.isFinite(Date.parse(call.created_at)) || !Number.isFinite(Date.parse(call.updated_at)) ||
        Date.parse(call.updated_at) < Date.parse(call.created_at)) throw new Error("protocol");
    return { receiptId: id(call.receipt_id), stepId, attemptId: id(call.attempt_id), ledgerId: id(call.ledger_id),
      state, responseDigest, messageDigest, requestIdDigest, usagePresence };
  });
  if (providers.length !== run.steps.length || new Set(providers.map(p => p.stepId)).size !== providers.length) throw new Error("protocol");
  return { ...run, manifest, providers };
}
function checkedCosts(value: unknown, runId: string, runtime?: ProbeRuntime): ProbeCost[] {
  const data = row(value);
  // This one-request probe cannot need pagination. Refuse partial cost summaries.
  if (data.next_cursor !== null) throw new Error("protocol");
  const items = list(data.items).map(value => {
    const item = row(value);
    if (item.run_id !== runId || item.provider_key !== "opencode-go" || item.model !== "deepseek-v4.1-flash" ||
        item.currency !== "USD" || typeof item.reserved_amount !== "string" || !/^\d+(\.\d+)?$/.test(item.reserved_amount) ||
        (item.estimated_amount !== null && (typeof item.estimated_amount !== "string" || !/^\d+(\.\d+)?$/.test(item.estimated_amount)))) throw new Error("protocol");
    const ledgerId = id(item.id);
    if (runtime && !runtime.providers.some(p => p.ledgerId === ledgerId && p.stepId === item.step_id)) throw new Error("protocol");
    return { ledgerId, state: member(item.status, ledgerStates), currency: "USD", reserved: item.reserved_amount,
      estimated: item.estimated_amount as string | null };
  });
  if (items.length > 1 || new Set(items.map(item => item.ledgerId)).size !== items.length) throw new Error("protocol");
  return items;
}

/** Persistent, scope-bound one-shot coordinator. No answers or raw receipts enter view/storage. */
export class PiProbeController {
  private view: ProbeView = { runId: "", attempted: false, busy: false, notice: "请输入预配置的原 Run UUID。" };
  private listeners = new Set<() => void>();
  private version = getScopeVersion();
  private live = true;
  private serial = 0;
  private observed?: ProbeRuntime;
  private key: string;
  private unsubscribe?: () => void;
  constructor(private session: Session, private storage: ProbeStorage, private baseUrl = "") {
    this.key = `retail.pi-probe.v1.scope:${identity(session)}`;
  }
  getSnapshot = (): ProbeView => this.view;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(patch: Partial<ProbeView>): void { this.view = { ...this.view, ...patch }; for (const listener of this.listeners) listener(); }
  private valid(): boolean { return this.live && this.version === getScopeVersion() && probeSessionIsCurrent(this.session); }
  private requireCurrent(): void { if (!this.valid()) throw new Error("scope"); }
  private hide(): void {
    this.serial++;
    this.view = { runId: "", attempted: true, busy: false, notice: "身份或权限已改变，旧诊断内容已隐藏。" };
    for (const listener of this.listeners) listener();
  }
  private journal(): Journal | null {
    const raw = this.storage.getItem(this.key);
    if (raw === null) return null;
    const value = row(JSON.parse(raw));
    if (value.version !== 1 || value.attempted !== true || value.sample !== PROBE_SAMPLE_SHA256 ||
        typeof value.runId !== "string" || !uuid.test(value.runId)) throw new Error("journal");
    return value as unknown as Journal;
  }
  start(): void {
    this.live = true;
    this.unsubscribe = subscribe(() => { if (!this.valid()) this.hide(); });
    try {
      this.requireCurrent();
      const journal = this.journal();
      if (journal) { this.set({ runId: journal.runId, attempted: true }); void this.refresh(); }
    } catch { this.set({ attempted: true, notice: "本地定位不可核验，禁止发送；请保留原 Run 记录。" }); }
  }
  dispose(): void { this.live = false; this.serial++; this.unsubscribe?.(); }
  private async observation<T>(path: string, check: (value: unknown) => T): Promise<Observation<T>> {
    this.requireCurrent();
    const startedAt = new Date().toISOString();
    const value = await authorizedRequest<unknown>(path, { method: "GET", baseUrl: this.baseUrl });
    this.requireCurrent();
    return { startedAt, collectedAt: new Date().toISOString(), data: check(value) };
  }
  /** Adopt an existing CLI/other-browser Run as read-only; this path never creates a Run. */
  async readOriginal(runId: string, includeCosts = false): Promise<void> {
    if (!this.valid() || this.view.busy || (this.view.attempted && this.view.runId !== runId)) return;
    if (!uuid.test(runId)) { this.set({ notice: "需要逐字填写预配置的小写规范 Run UUID。" }); return; }
    this.set({ busy: true });
    let remembered = false;
    try {
      const locate = async () => {
        this.requireCurrent();
        const previous = this.journal();
        if (previous && previous.runId !== runId) throw new Error("journal");
        const tombstone = `retail.pi-probe.v1.run:${runId}`;
        const existing = this.storage.getItem(tombstone);
        const encoded = JSON.stringify({ version: 1, runId, attempted: true, sample: PROBE_SAMPLE_SHA256 } satisfies Journal);
        // A read-only adoption permanently disables POST here, even if GET is 404/403/unknown.
        // Never overwrite another tab's original tombstone or copy its identity/content.
        if (existing === null) this.storage.setItem(tombstone, encoded);
        this.storage.setItem(this.key, encoded);
        if (this.storage.getItem(tombstone) !== (existing ?? encoded) || this.storage.getItem(this.key) !== encoded) throw new Error("journal");
        this.requireCurrent();
        this.set({ runId, attempted: true, notice: "已保留原 Run 定位，仅鉴权回查，不发送样本。" });
        remembered = true;
      };
      // Serialize with the POST path when available; GET recovery also works without Web Locks.
      if (globalThis.navigator?.locks) await navigator.locks.request("retail.pi-probe.v1.send", locate);
      else await locate();
    } catch {
      if (this.valid()) this.set({ notice: "本地原 Run 定位不可核验或已固定其他 Run；禁止发送，保留原记录。" });
    } finally { if (this.valid()) this.set({ busy: false }); }
    if (remembered && this.valid()) await this.refresh(includeCosts);
  }
  async send(runId: string): Promise<void> {
    if (!this.valid() || this.view.busy || this.view.attempted) return;
    if (!uuid.test(runId)) { this.set({ notice: "需要逐字填写预配置的小写规范 Run UUID。" }); return; }
    this.set({ busy: true });
    let attempted = false;
    try {
      // Web Locks serialize tabs. Without this guarantee only GET recovery is allowed.
      if (!globalThis.navigator?.locks) throw new Error("locks");
      await navigator.locks.request("retail.pi-probe.v1.send", async () => {
        this.requireCurrent();
        const previous = this.journal();
        if (previous) { this.set({ runId: previous.runId, attempted: true }); return; }
        const tombstone = `retail.pi-probe.v1.run:${runId}`;
        if (this.storage.getItem(tombstone) !== null) throw new Error("attempted");
        const journal: Journal = { version: 1, runId, attempted: true, sample: PROBE_SAMPLE_SHA256 };
        const encoded = JSON.stringify(journal);
        // Both writes and readback must succeed before any POST; never remove either marker.
        this.storage.setItem(tombstone, encoded);
        this.storage.setItem(this.key, encoded);
        if (this.storage.getItem(tombstone) !== encoded || this.storage.getItem(this.key) !== encoded) throw new Error("journal");
        this.set({ runId, attempted: true, notice: "已记录发送尝试。后续仅回查原 Run。" });
        this.requireCurrent();
        attempted = true;
        const response = await authorizedRequest<unknown>("/api/v1/agent-runs/pi-readonly", {
          method: "POST", baseUrl: this.baseUrl, body: { text: PROBE_TEXT, run_id: runId },
          headers: { "Idempotency-Key": runId }, timeoutMs: 120000,
        });
        this.requireCurrent();
        checkedRun(response, runId); // POST is not a GET observation or success proof.
      });
    } catch (error) {
      if (!this.valid()) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) { this.hide(); return; }
      this.set({ attempted: this.view.attempted || attempted,
        notice: "发送未获可核验结果或已被禁止。不要重发、换 UUID；仅回查原 Run。" });
    } finally { if (this.valid()) this.set({ busy: false }); }
    if (this.valid() && this.view.attempted && this.view.runId) await this.refresh();
  }
  async refresh(includeCosts = false): Promise<void> {
    if (!this.valid() || this.view.busy || !uuid.test(this.view.runId)) return;
    const runId = this.view.runId, ticket = ++this.serial;
    const previous = this.observed;
    this.set({ busy: true, run: undefined, runtime: undefined, costs: undefined, costNotice: undefined, costRead: undefined,
      notice: "正在分别读取原 Run；各快照并非同一事务。" });
    try {
      const run = await this.observation(`/api/v1/agent-runs/${runId}`, value => checkedRun(value, runId));
      const runtime = await this.observation(`/api/v1/agent-runs/${runId}/runtime-receipt`, value => checkedRuntime(value, runId));
      for (const step of run.data.steps) {
        const other = runtime.data.steps.find(s => s.id === step.id);
        if (!other || other.kind !== step.kind || other.sequence !== step.sequence) throw new Error("protocol");
      }
      if (previous && (previous.manifest !== runtime.data.manifest || previous.providers.some(old => {
        const next = runtime.data.providers.find(p => p.receiptId === old.receiptId);
        return !next || next.stepId !== old.stepId || next.ledgerId !== old.ledgerId || next.attemptId !== old.attemptId ||
          (old.responseDigest !== null && old.responseDigest !== next.responseDigest) ||
          (old.messageDigest !== null && old.messageDigest !== next.messageDigest) ||
          (old.requestIdDigest !== null && old.requestIdDigest !== next.requestIdDigest);
      }))) throw new Error("protocol");
      let costs: Observation<ProbeCost[]> | undefined;
      let costRead: ProbeView["costRead"];
      let costNotice = "费用尚未请求；需独立费用权限。";
      if (includeCosts) {
        const startedAt = new Date().toISOString();
        try {
          costs = await this.observation(`/api/v1/agent-costs?run_id=${runId}&limit=50`, value => checkedCosts(value, runId, runtime.data));
          costNotice = "费用可见；本地估算与真实 Go 扣费不是同一概念。";
        } catch (error) {
          if (error instanceof ApiError && error.status === 403) costNotice = "费用不可见（403，缺少独立费用权限）。";
          else throw error;
        } finally { costRead = { startedAt, collectedAt: new Date().toISOString() }; }
      }
      if (!this.valid() || ticket !== this.serial) return;
      this.observed = runtime.data;
      this.set({ run, runtime, costs, costNotice, costRead,
        notice: runtime.data.state === "failed" ? "诊断已停止：Run.failed；模型内容与工具结果未核实。此回执不构成业务答案或真实 Go 成功证明。" :
          "仅为诊断回执；未知结果保留，不派发工具或第二模型。请仅回查原 Run。" });
    } catch (error) {
      if (!this.valid() || ticket !== this.serial) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) this.hide();
      else this.set({ run: undefined, runtime: undefined, costs: undefined,
        notice: "原 Run 回查失败或回执冲突（含 404/409/版本/摘要）。内容已隐藏，请保留原 Run，仅回查，不重发。" });
    } finally { if (this.valid() && ticket === this.serial) this.set({ busy: false }); }
  }
}
