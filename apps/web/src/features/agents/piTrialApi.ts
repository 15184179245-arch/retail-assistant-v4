import { ApiError } from "../../lib/api/client";
import type { Session } from "../../lib/auth/session";
import { authorizedRequest, getSession, getScopeVersion, subscribe } from "../../lib/auth/session";

export const TRIAL_TEXT = "软件开发合成验收：请用只读 tasks.list_mine 列出我自己的代码审查待办，并指出 CAMPAIGN-001 是否在列表中；不创建或修改记录。";
export const TRIAL_SAMPLE_SHA256 = "fcec3cbc008051fcc71fcee1797b3aca202ade9bcf3b7fa1a07adbcd142e8af6";
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const sha = /^[a-f0-9]{64}$/;
const states = ["created", "running", "waiting_for_human", "waiting_for_external", "completed", "failed", "cancelled"];
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
export interface TrialStep { id: string; sequence: number; kind: string; state: string }
export interface TrialCitation { kind: string; objectId: string; versionRef: string; version: string; label: string; sourceRef: string | null; observedAt: string | null }
export interface TrialRun { runId: string; state: string; steps: TrialStep[]; answer?: string; answerStatus?: string; citations?: TrialCitation[] }
export interface TrialNative { receiptId: string; modelStepId: string; toolStepId: string | null; nativeCallId: string; tool: string; state: string; outputRef: string | null }
export interface TrialProvider {
  receiptId: string; stepId: string; attemptId: string; ledgerId: string; state: string;
  responseDigest: string | null; messageDigest: string | null; requestIdDigest: string | null;
  usagePresence: string;
}
export interface TrialRuntime extends TrialRun { manifest: string; toolset: string; profile: string; natives: TrialNative[]; providers: TrialProvider[] }
export interface TrialCost { ledgerId: string; state: string; currency: string; reserved: string; estimated: string | null }
export interface Observation<T> { startedAt: string; collectedAt: string; data: T }
export interface TrialInfo {
  mode: "synthetic-only-v1" | "real-probe" | "real-readonly-trial";
  runId: string; sampleText: string; sampleSha256: string; expiresAt: string; startAvailable: boolean;
}
export interface TrialView {
  runId: string; attempted: boolean; busy: boolean; notice: string;
  info?: Observation<TrialInfo>; infoBusy?: boolean; infoNotice?: string;
  run?: Observation<TrialRun>; runtime?: Observation<TrialRuntime>;
  costs?: Observation<TrialCost[]>; costNotice?: string;
  costRead?: { startedAt: string; collectedAt: string };
}
export interface TrialStorage { getItem(key: string): string | null; setItem(key: string, value: string): void }
interface Journal { version: 1; runId: string; attempted: true; sample: string }
function identity(session: Session): string {
  return JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
}
function sameIdentity(a: Session | null, b: Session): boolean {
  return !!a && identity(a) === identity(b) && a.accessToken === b.accessToken;
}
export function trialSessionIsCurrent(session: Session): boolean { return sameIdentity(getSession(), session); }
function text(value: unknown, max = 100000): string {
  if (typeof value !== "string" || value.length > max) throw new Error("protocol");
  return value;
}
function unique(values: string[]): void { if (new Set(values).size !== values.length) throw new Error("protocol"); }
function checkedInfo(value: unknown): TrialInfo {
  const data = row(value);
  const keys = ["mode", "run_id", "sample_text", "sample_sha256", "expires_at", "start_available"];
  if (Object.keys(data).length !== keys.length || keys.some(key => !(key in data)) ||
      typeof data.run_id !== "string" || !uuid.test(data.run_id) || data.sample_text !== TRIAL_TEXT ||
      data.sample_sha256 !== TRIAL_SAMPLE_SHA256 || typeof data.expires_at !== "string" ||
      !Number.isFinite(Date.parse(data.expires_at)) || typeof data.start_available !== "boolean" ||
      (data.start_available && Date.parse(data.expires_at) <= Date.now())) throw new Error("protocol");
  return { mode: member(data.mode, ["synthetic-only-v1", "real-probe", "real-readonly-trial"]) as TrialInfo["mode"],
    runId: data.run_id, sampleText: data.sample_text, sampleSha256: data.sample_sha256,
    expiresAt: data.expires_at, startAvailable: data.start_available };
}
function checkedRun(value: unknown, runId: string, runtime = false): TrialRun {
  const data = row(value);
  if (data.run_id !== runId) throw new Error("protocol");
  const steps = list(data.steps).map(value => {
    const step = row(value);
    if (!Number.isInteger(step.sequence_no) || (step.sequence_no as number) < 1) throw new Error("protocol");
    return { id: id(runtime ? step.step_id : step.id), sequence: step.sequence_no as number,
      kind: member(step.kind, ["model", "tool", "checkpoint"]), state: member(step.state, stepStates) };
  });
  unique(steps.map(s => s.id)); unique(steps.map(s => String(s.sequence)));
  const result: TrialRun = { runId, state: member(data.state, states), steps };
  if (!runtime) {
    result.answerStatus = member(data.answer_status, ["available", "not_recorded", "unavailable"]);
    // Only the authenticated ordinary receipt decides answer/source availability.
    if (result.answerStatus === "available") {
      result.answer = text(data.answer);
      result.citations = list(data.citations).map(value => {
        const c = row(value);
        const observedAt = c.observed_at === null ? null : text(c.observed_at, 100);
        if (observedAt !== null && !Number.isFinite(Date.parse(observedAt))) throw new Error("protocol");
        return { kind: member(c.kind, ["knowledge", "catalog", "memory", "guanjia_observation"]), objectId: text(c.object_id, 200),
          versionRef: text(c.version_ref, 500), version: text(c.version, 200), label: text(c.label, 2000),
          sourceRef: c.source_ref === null ? null : text(c.source_ref, 2000), observedAt };
      });
    }
  }
  return result;
}
function checkedRuntime(value: unknown, runId: string): TrialRuntime {
  const data = row(value), run = checkedRun(data, runId, true);
  const launch = row(data.launch), runtime = row(data.runtime);
  const memory = launch.tool_profile === "pi-memory-v1";
  const family = memory ? "memory" : "readonly";
  const stream = data.contract_version === 3;
  const profileMatches = stream
    ? !memory && launch.tool_profile === "pi-readonly-v1" && launch.wire_protocol === "v4.pi-readonly/3"
      && ["host-pi-readonly-node24-linux-online-stream-v1", "host-pi-readonly-node24-darwin-online-stream-v1"].includes(String(launch.launch_profile))
    : data.contract_version === 2 && launch.wire_protocol === `v4.pi-${family}/2`
      && launch.tool_profile === (memory ? "pi-memory-v1" : "pi-readonly-v1")
      && [`host-pi-${family}-node24-linux-online-v1`, `host-pi-${family}-node24-darwin-online-v1`].includes(String(launch.launch_profile));
  if (!profileMatches || launch.status !== "recorded" ||
      runtime.status !== "recorded" || runtime.engine !== "@earendil-works/pi-coding-agent" || runtime.version !== "1.4.0" ||
      runtime.execution_proof !== "not_proven" || data.recovery !== "A_stop_only" ||
      data.result_replay !== "unsupported" || data.step_citation_association !== "unsupported") throw new Error("protocol");
  if (stream) {
    // /3 publishes committed metadata only. Reject private extensions rather
    // than silently accepting raw transport or tool data into a public view.
    const exact = (object: Row, keys: string[]) => {
      if (Object.keys(object).length !== keys.length || keys.some(key => !(key in object))) throw new Error("protocol");
    };
    exact(data, ["contract_version", "run_id", "state", "runtime", "launch", "steps", "native_calls", "provider_calls", "recovery", "result_replay", "step_citation_association"]);
    exact(runtime, ["status", "basis", "reason", "engine", "version", "adapter_contract", "execution_proof"]);
    if (runtime.basis !== "run_runtime_binding" || runtime.reason !== null || runtime.adapter_contract !== "host-run-receipt-v1") throw new Error("protocol");
    exact(launch, ["status", "reason", "wire_protocol", "tool_profile", "launch_profile", "source_manifest_sha256", "toolset_version"]);
    if (launch.reason !== null) throw new Error("protocol");
    for (const step of list(data.steps).map(row)) exact(step, ["step_id", "kind", "state", "sequence_no", "output_ref", "error_code"]);
    for (const call of list(data.native_calls).map(row)) exact(call, ["receipt_id", "model_step_id", "tool_step_id", "native_call_id", "pi_name", "tool_key", "tool_version", "toolset_version", "state", "output_ref", "error_code"]);
    for (const call of list(data.provider_calls).map(row)) exact(call, ["receipt_id", "model_step_id", "attempt_id", "provider_request_id_sha256", "response_digest", "message_digest", "usage_presence", "ledger_id", "state", "created_at", "updated_at"]);
  }
  const manifest = digest(launch.source_manifest_sha256);
  if (!manifest || typeof launch.toolset_version !== "string" || !/^sha256:[a-f0-9]{64}$/.test(launch.toolset_version)) throw new Error("protocol");
  const toolset = launch.toolset_version;
  const byId = new Map(run.steps.map(step => [step.id, step]));
  const mapping: Record<string, string> = { "tasks.list_mine": "v4_tasks_list_mine_v1", "knowledge.search": "v4_knowledge_search_v1", "catalog.search": "v4_catalog_search_v1", "memory.search": "v4_memory_search_v1", ...(memory ? {"memory.save": "v4_memory_save_v1", "memory.correct": "v4_memory_correct_v1", "memory.withdraw": "v4_memory_withdraw_v1"} : {}) };
  const natives = list(data.native_calls).map(value => {
    const c = row(value), modelStepId = id(c.model_step_id), model = byId.get(modelStepId);
    const state = member(c.state, ["accepted", "dispatch_intent", "succeeded", "failed", "unknown"]);
    const tool = member(c.tool_key, Object.keys(mapping));
    const toolStepId = c.tool_step_id === null ? null : id(c.tool_step_id);
    const outputRef = c.output_ref === null ? null : text(c.output_ref, 71);
    if (!model || model.kind !== "model" || model.state !== "succeeded" || mapping[tool] !== c.pi_name ||
        c.tool_version !== "1" || c.toolset_version !== toolset ||
        (outputRef !== null && !/^sha256:[a-f0-9]{64}$/.test(outputRef))) throw new Error("protocol");
    const toolStep = toolStepId === null ? undefined : byId.get(toolStepId);
    if (toolStepId !== null && (!toolStep || toolStep.kind !== "tool" || toolStep.sequence <= model.sequence)) throw new Error("protocol");
    if (state === "accepted" && (toolStepId !== null || outputRef !== null || c.error_code !== null) ||
        state === "dispatch_intent" && (!toolStepId || outputRef !== null || c.error_code !== null) ||
        state === "succeeded" && (!toolStep || toolStep.state !== "succeeded" || !outputRef || c.error_code !== null) ||
        ["failed", "unknown"].includes(state) && (outputRef !== null || typeof c.error_code !== "string")) throw new Error("protocol");
    if (state === "succeeded") {
      const rawTool = list(data.steps).map(row).find(step => step.step_id === toolStepId);
      if (rawTool?.output_ref !== outputRef) throw new Error("protocol");
    }
    const nativeCallId = text(c.native_call_id, 100);
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(nativeCallId)) throw new Error("protocol");
    return { receiptId: id(c.receipt_id), modelStepId, toolStepId, nativeCallId, tool, state, outputRef };
  });
  unique(natives.map(c => c.receiptId)); unique(natives.map(c => c.nativeCallId));
  unique(natives.flatMap(c => c.toolStepId === null ? [] : [c.toolStepId]));
  const providers = list(data.provider_calls).map(value => {
    const call = row(value), state = member(call.state, providerStates);
    const responseDigest = digest(call.response_digest), messageDigest = digest(call.message_digest);
    const requestIdDigest = digest(call.provider_request_id_sha256), stepId = id(call.model_step_id);
    if (byId.get(stepId)?.kind !== "model" || (state === "responded" && !responseDigest) ||
        (["permitted", "send_intent"].includes(state) && (responseDigest || messageDigest || call.usage_presence !== null))) throw new Error("protocol");
    let usagePresence = "unknown";
    if (call.usage_presence !== null) {
      const presence = row(call.usage_presence), keys = ["prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"];
      if (Object.keys(presence).length !== 4 || keys.some(key => typeof presence[key] !== "boolean")) throw new Error("protocol");
      usagePresence = keys.map(key => `${key}=${presence[key]}`).join(", ");
    }
    if (typeof call.created_at !== "string" || typeof call.updated_at !== "string" ||
        !Number.isFinite(Date.parse(call.created_at)) || !Number.isFinite(Date.parse(call.updated_at)) ||
        Date.parse(call.updated_at) < Date.parse(call.created_at)) throw new Error("protocol");
    return { receiptId: id(call.receipt_id), stepId, attemptId: id(call.attempt_id), ledgerId: id(call.ledger_id),
      state, responseDigest, messageDigest, requestIdDigest, usagePresence };
  });
  for (const key of ["receiptId", "stepId", "attemptId", "ledgerId"] as const) unique(providers.map(c => c[key]));
  if (providers.length !== run.steps.filter(s => s.kind === "model").length) throw new Error("protocol");
  return { ...run, manifest, toolset, profile: String(launch.launch_profile), providers, natives };
}
function checkedCosts(value: unknown, runId: string, runtime?: TrialRuntime): TrialCost[] {
  const data = row(value);
  // The bounded two-model trial reads both ledgers together; refuse partial cost summaries.
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
  if (new Set(items.map(item => item.ledgerId)).size !== items.length) throw new Error("protocol");
  return items;
}

/** Scope-bound one-shot coordinator. Storage contains only locators, never answers or credentials. */
export class PiTrialController {
  private view: TrialView = { runId: "", attempted: false, busy: false, notice: "请输入预配置的原 Run UUID。" };
  private listeners = new Set<() => void>();
  private version = getScopeVersion();
  private live = true;
  private serial = 0;
  private lifecycle = 0;
  private observed?: TrialRuntime;
  private key: string;
  private unsubscribe?: () => void;
  private infoPending?: Promise<void>;
  constructor(private session: Session, private storage: TrialStorage, private baseUrl = "") {
    this.key = `retail.pi-trial.v1.scope:${identity(session)}`;
  }
  getSnapshot = (): TrialView => this.view;
  subscribe = (listener: () => void): (() => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private set(patch: Partial<TrialView>): void { this.view = { ...this.view, ...patch }; for (const listener of this.listeners) listener(); }
  private valid(generation = this.lifecycle): boolean { return generation === this.lifecycle && this.live && this.version === getScopeVersion() && trialSessionIsCurrent(this.session); }
  private requireCurrent(generation = this.lifecycle): void { if (!this.valid(generation)) throw new Error("scope"); }
  private hide(): void {
    this.lifecycle++;
    this.serial++;
    this.view = { runId: "", attempted: true, busy: false, notice: "身份或权限已改变，旧试用内容已隐藏。" };
    for (const listener of this.listeners) listener();
  }
  private journal(): Journal | null {
    const raw = this.storage.getItem(this.key);
    if (raw === null) return null;
    const value = row(JSON.parse(raw));
    if (value.version !== 1 || value.attempted !== true || value.sample !== TRIAL_SAMPLE_SHA256 ||
        typeof value.runId !== "string" || !uuid.test(value.runId)) throw new Error("journal");
    return value as unknown as Journal;
  }
  start(): void {
    // StrictMode replays effects on the same controller. Retire all old async
    // work, then start a fresh GET recovery; never revive an old send callback.
    this.lifecycle++;
    this.serial++;
    this.unsubscribe?.();
    this.live = true;
    this.set({ busy: false, info: undefined, infoBusy: true, infoNotice: "正在核对 Host 试用配置。",
      run: undefined, runtime: undefined, costs: undefined, costRead: undefined, costNotice: undefined });
    this.unsubscribe = subscribe(() => { if (!this.valid()) this.hide(); });
    try {
      this.requireCurrent();
      const journal = this.journal();
      if (journal) { this.set({ runId: journal.runId, attempted: true }); void this.refresh(); }
    } catch { this.set({ attempted: true, notice: "本地定位不可核验，禁止发送；请保留原 Run 记录。" }); }
    this.infoPending = this.loadInfo();
  }
  private async loadInfo(): Promise<void> {
    const generation = this.lifecycle;
    try {
      const info = await this.observation("/api/v1/agent-runs/pi-trial-info", checkedInfo);
      if (!this.valid(generation)) return;
      if (this.view.runId && this.view.runId !== info.data.runId) throw new Error("configuration-conflict");
      this.set({ info, infoNotice: info.data.startAvailable ? "Host 已配置；仅点击发送才开始试用。" :
        "当前配置不可开始新试用（已过期或已有 Run）；可只读回查原 Run。" });
    } catch (error) {
      if (!this.valid(generation)) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) { this.hide(); return; }
      this.set({ info: undefined, infoNotice: "Host 配置待核或与原 Run 冲突；禁止发送，可只读回查原 Run。" });
    } finally { if (this.valid(generation)) this.set({ infoBusy: false }); }
  }
  dispose(): void { this.live = false; this.lifecycle++; this.serial++; this.unsubscribe?.(); }
  private async observation<T>(path: string, check: (value: unknown) => T): Promise<Observation<T>> {
    const generation = this.lifecycle;
    this.requireCurrent(generation);
    const startedAt = new Date().toISOString();
    const value = await authorizedRequest<unknown>(path, { method: "GET", baseUrl: this.baseUrl });
    this.requireCurrent(generation);
    return { startedAt, collectedAt: new Date().toISOString(), data: check(value) };
  }
  /** Adopt an existing CLI/other-browser Run as read-only; this path never creates a Run. */
  async readOriginal(runId: string, includeCosts = false): Promise<void> {
    const generation = this.lifecycle;
    if (!this.valid(generation) || this.view.busy || (this.view.runId && this.view.runId !== runId)) return;
    if (!uuid.test(runId)) { this.set({ notice: "需要逐字填写预配置的小写规范 Run UUID。" }); return; }
    this.set({ busy: true, ...(this.view.info && this.view.info.data.runId !== runId ? {
      info: undefined, infoNotice: "当前 Host 配置与原 Run 不同；原 Run 的模型模式待核，仅可回查。",
    } : {}) });
    let remembered = false;
    try {
      const locate = async () => {
        this.requireCurrent(generation);
        const previous = this.journal();
        if (previous && previous.runId !== runId) throw new Error("journal");
        const tombstone = `retail.pi-trial.v1.run:${runId}`;
        const existing = this.storage.getItem(tombstone);
        const encoded = JSON.stringify({ version: 1, runId, attempted: true, sample: TRIAL_SAMPLE_SHA256 } satisfies Journal);
        // A read-only adoption permanently disables POST here, even if GET is 404/403/unknown.
        // Never overwrite another tab's original tombstone or copy its identity/content.
        if (existing === null) this.storage.setItem(tombstone, encoded);
        this.storage.setItem(this.key, encoded);
        if (this.storage.getItem(tombstone) !== (existing ?? encoded) || this.storage.getItem(this.key) !== encoded) throw new Error("journal");
        this.requireCurrent(generation);
        this.set({ runId, attempted: true, notice: "已保留原 Run 定位，仅鉴权回查，不发送样本。" });
        remembered = true;
      };
      // Serialize with the POST path when available; GET recovery also works without Web Locks.
      if (globalThis.navigator?.locks) await navigator.locks.request("retail.pi-trial.v1.send", locate);
      else await locate();
    } catch {
      // Storage is a send prerequisite, not a GET prerequisite. Never overwrite an
      // unverifiable locator; keep this manual recovery ephemeral and POST-disabled.
      if (this.valid(generation)) {
        this.set({ runId, attempted: true, notice: "持久定位不可用；本次仅临时回查原 Run，禁止发送。" });
        remembered = true;
      }
    } finally { if (this.valid(generation)) this.set({ busy: false }); }
    if (remembered && this.valid(generation)) await this.refresh(includeCosts);
  }
  async send(runId: string): Promise<void> {
    const generation = this.lifecycle;
    // A click may arrive while the GET is pending. It never supplies authority;
    // re-check the exact Host config, scope and deadline once that GET settles.
    await this.infoPending;
    if (!this.valid(generation) || this.view.busy || this.view.attempted) return;
    const info = this.view.info?.data;
    if (!info || !info.startAvailable || info.runId !== runId || Date.parse(info.expiresAt) <= Date.now()) {
      this.set({ notice: "Host 配置不可用、已过期或 UUID 不符；禁止发送，仅可回查原 Run。" }); return;
    }
    if (!uuid.test(runId)) { this.set({ notice: "需要逐字填写预配置的小写规范 Run UUID。" }); return; }
    this.set({ busy: true });
    let attempted = false;
    try {
      // Web Locks serialize tabs. Without this guarantee only GET recovery is allowed.
      if (!globalThis.navigator?.locks) throw new Error("locks");
      await navigator.locks.request("retail.pi-trial.v1.send", async () => {
        this.requireCurrent(generation);
        if (Date.parse(info.expiresAt) <= Date.now()) throw new Error("expired");
        const previous = this.journal();
        if (previous) { this.set({ runId: previous.runId, attempted: true }); return; }
        const tombstone = `retail.pi-trial.v1.run:${runId}`;
        if (this.storage.getItem(tombstone) !== null) throw new Error("attempted");
        const journal: Journal = { version: 1, runId, attempted: true, sample: TRIAL_SAMPLE_SHA256 };
        const encoded = JSON.stringify(journal);
        // Both writes and readback must succeed before any POST; never remove either marker.
        this.storage.setItem(tombstone, encoded);
        this.storage.setItem(this.key, encoded);
        if (this.storage.getItem(tombstone) !== encoded || this.storage.getItem(this.key) !== encoded) throw new Error("journal");
        this.set({ runId, attempted: true, notice: "已记录发送尝试。后续仅回查原 Run。" });
        this.requireCurrent(generation);
        attempted = true;
        const response = await authorizedRequest<unknown>("/api/v1/agent-runs/pi-readonly", {
          method: "POST", baseUrl: this.baseUrl, body: { text: info.sampleText, run_id: runId },
          headers: { "Idempotency-Key": runId }, timeoutMs: 120000,
        });
        this.requireCurrent(generation);
        checkedRun(response, runId); // POST is not a GET observation or success proof.
      });
    } catch (error) {
      if (!this.valid(generation)) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) { this.hide(); return; }
      this.set({ attempted: this.view.attempted || attempted,
        notice: "发送未获可核验结果或已被禁止。不要重发、换 UUID；仅回查原 Run。" });
    } finally { if (this.valid(generation)) this.set({ busy: false }); }
    if (this.valid(generation) && this.view.attempted && this.view.runId) await this.refresh();
  }
  async refresh(includeCosts = false): Promise<void> {
    const generation = this.lifecycle;
    if (!this.valid(generation) || this.view.busy || !uuid.test(this.view.runId)) return;
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
      if (previous && (previous.manifest !== runtime.data.manifest || previous.toolset !== runtime.data.toolset || previous.profile !== runtime.data.profile ||
          previous.steps.some(old => { const next = runtime.data.steps.find(s => s.id === old.id); return !next || next.kind !== old.kind || next.sequence !== old.sequence; }) ||
          previous.natives.some(old => { const next = runtime.data.natives.find(c => c.receiptId === old.receiptId); return !next || next.nativeCallId !== old.nativeCallId || next.modelStepId !== old.modelStepId || next.tool !== old.tool || (old.toolStepId !== null && next.toolStepId !== old.toolStepId) || (old.outputRef !== null && next.outputRef !== old.outputRef); }) || previous.providers.some(old => {
        const next = runtime.data.providers.find(p => p.receiptId === old.receiptId);
        return !next || next.stepId !== old.stepId || next.ledgerId !== old.ledgerId || next.attemptId !== old.attemptId ||
          (old.responseDigest !== null && old.responseDigest !== next.responseDigest) ||
          (old.messageDigest !== null && old.messageDigest !== next.messageDigest) ||
          (old.requestIdDigest !== null && old.requestIdDigest !== next.requestIdDigest);
      }))) throw new Error("protocol");
      let costs: Observation<TrialCost[]> | undefined;
      let costRead: TrialView["costRead"];
      let costNotice = "费用尚未请求；需独立费用权限。";
      if (includeCosts) {
        const startedAt = new Date().toISOString();
        try {
          costs = await this.observation(`/api/v1/agent-costs?run_id=${runId}&limit=50`, value => checkedCosts(value, runId, runtime.data));
          costNotice = "费用可见；本地估算与真实 Go 扣费不是同一概念。";
        } catch (error) {
          if (error instanceof ApiError && error.status === 403) costNotice = "费用不可见（403，缺少独立费用权限）。";
          else if (error instanceof ApiError && error.status === 401) throw error;
          else costNotice = "费用暂不可核验（独立快照新增账本、关联冲突、分页或读取失败）；费用已隐藏，可仅回查。";
        } finally { costRead = { startedAt, collectedAt: new Date().toISOString() }; }
      }
      if (!this.valid(generation) || ticket !== this.serial) return;
      this.observed = runtime.data;
      this.set({ run, runtime, costs, costNotice, costRead,
        notice: runtime.data.state === "failed" ? "Run.failed：请核对失败或未知状态；只回查原 Run，不重发。" :
          "已读取同一正式 Run 的独立快照；Host 配置与运行时登记均不单独证明真实模型执行或营业可用。" });
    } catch (error) {
      if (!this.valid(generation) || ticket !== this.serial) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) this.hide();
      else this.set({ run: undefined, runtime: undefined, costs: undefined,
        notice: "原 Run 回查失败或回执冲突（含 404/409/版本/摘要）。内容已隐藏，请保留原 Run，仅回查，不重发。" });
    } finally { if (this.valid(generation) && ticket === this.serial) this.set({ busy: false }); }
  }
}
