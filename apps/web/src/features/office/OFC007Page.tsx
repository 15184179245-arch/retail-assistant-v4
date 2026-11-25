import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion, getSession } from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";

/*
 * OFC-007 B3: the unified exception / handover queue.
 *
 * The page only talks to the Case endpoints. It never calls receiving, ERP
 * dispatch/stage/reconcile, sales handoff, workflow perform, tasks or approval
 * mutation: a case is tracking, and closing it says "checked in this queue",
 * never "the upstream business is settled". Every source shows the exact label
 * of what the source really recorded, and a missing closure proof disables the
 * close button instead of inventing one.
 */

export type SourceType =
  | "receiving_discrepancy" | "erp_sandbox" | "erp_intent"
  | "sales_handoff" | "workflow" | "task";
export type CaseKind =
  | "shortage" | "overage" | "wrong_item" | "identity_ambiguous" | "partial"
  | "unknown_write" | "overdue" | "price_change" | "workflow_attention";
export type CaseState = "open" | "owned" | "transfer_pending" | "resolved";
export type Operation =
  | "create" | "claim" | "takeover" | "propose_transfer" | "accept_transfer"
  | "cancel_transfer" | "resolve" | "reopen";
export type CommandOperation = Exclude<Operation, "create">;
export type SourceVerification =
  | "employee_observation" | "synthetic_erp" | "manual_erp_evidence"
  | "sales_record" | "workflow_tracking" | "task_record";
export type ResolutionKind =
  "verified_resolution" | "reconciled_partial" | "verified_rejection";

export interface SourceLocator { source_type: SourceType; source_id: string; kind: CaseKind }
export interface SourceReference { kind: string; id: string; version: number | null; digest: string | null }
export interface SourceLine {
  id: string; state: string; version: number | null;
  receipt_digest: string | null; evidence_digest: string | null;
  error_code: string | null; observed_quantity: string | null;
  expected_quantity: string | null; cumulative_observed_quantity: string | null;
  unit: string | null; source_verification: SourceVerification;
}
export interface ExceptionSource extends SourceLocator {
  source_digest: string; source_version: number | null; source_state: string;
  anomaly_present: boolean; can_resolve: boolean;
  resolution_kind: ResolutionKind | null; resolution_digest: string | null;
  source_verification: SourceVerification;
  references: SourceReference[]; lines: SourceLine[];
  effect: "case_tracking_only"; mode: "human_supervised";
}
export interface CaseCreateInput extends SourceLocator {
  expected_source_digest: string; expected_source_version: number | null; reason: string;
}
export interface CaseCommandInput {
  operation: CommandOperation; expected_version: number; expected_source_digest: string;
  reason: string; to_owner_id: string | null; expected_resolution_digest: string | null;
}
export interface ReceiptReferences extends SourceLocator {
  source_version: number | null; source_digest: string; source_state: string;
  source_verification: SourceVerification; resolution_kind: ResolutionKind | null;
}
export interface CaseCommandReceipt {
  id: string; case_id: string; actor_id: string; operation: Operation;
  case_version: number; request_digest: string; source_digest: string;
  reason_digest: string; from_owner_id: string | null; to_owner_id: string | null;
  resolution_digest: string | null; references: ReceiptReferences; created_at: string;
}
export interface CaseSummary extends SourceLocator {
  id: string; state: CaseState; version: number; initial_source_digest: string;
  source_version: number | null; owner_id: string | null;
  pending_owner_id: string | null; owner_available: boolean;
  pending_owner_available: boolean | null; created_by: string;
  created_at: string; updated_at: string; resolved_at: string | null;
  resolution_digest: string | null; effect: "case_tracking_only"; mode: "human_supervised";
}
export interface CaseView extends CaseSummary {
  source: ExceptionSource | null; blocking_code: "SOURCE_UNAVAILABLE" | null;
  source_changed: boolean; command_receipt: CaseCommandReceipt | null; replayed: boolean;
}
export interface CaseListPage {
  items: CaseSummary[]; next_cursor: string | null;
  effect: "case_tracking_only"; mode: "human_supervised";
}
export interface CaseReceiptPage {
  case_id: string; case_version: number; receipts: CaseCommandReceipt[];
  next_after_version: number | null;
  effect: "case_tracking_only"; mode: "human_supervised";
}
export interface CaseCommandStatus { status: "found" | "not_found"; case: CaseView | null }

// Enum shapes are copied from the frozen B2a DTOs, not invented here.
export const SOURCE_TYPES: SourceType[] = ["receiving_discrepancy", "erp_sandbox",
  "erp_intent", "sales_handoff", "workflow", "task"];
export const CASE_KINDS: CaseKind[] = ["shortage", "overage", "wrong_item",
  "identity_ambiguous", "partial", "unknown_write", "overdue", "price_change",
  "workflow_attention"];
export const CASE_STATES: CaseState[] = ["open", "owned", "transfer_pending", "resolved"];
export const COMMAND_OPERATIONS: CommandOperation[] = ["claim", "takeover",
  "propose_transfer", "accept_transfer", "cancel_transfer", "resolve", "reopen"];
export const SOURCE_VERIFICATIONS: SourceVerification[] = ["employee_observation",
  "synthetic_erp", "manual_erp_evidence", "sales_record", "workflow_tracking", "task_record"];
export const RESOLUTION_KINDS: ResolutionKind[] = ["verified_resolution",
  "reconciled_partial", "verified_rejection"];

const KINDS_BY_SOURCE: Record<SourceType, CaseKind[]> = {
  receiving_discrepancy: ["shortage", "overage", "wrong_item", "identity_ambiguous", "price_change"],
  erp_sandbox: ["partial", "unknown_write"],
  erp_intent: ["unknown_write"],
  sales_handoff: ["unknown_write"],
  workflow: ["workflow_attention"],
  task: ["overdue"],
};
const VERIFICATION_BY_SOURCE: Record<SourceType, SourceVerification> = {
  receiving_discrepancy: "employee_observation",
  erp_sandbox: "synthetic_erp",
  erp_intent: "manual_erp_evidence",
  sales_handoff: "sales_record",
  workflow: "workflow_tracking",
  task: "task_record",
};

export function kindsFor(sourceType: SourceType): CaseKind[] { return KINDS_BY_SOURCE[sourceType]; }
export function verificationFor(sourceType: SourceType): SourceVerification {
  return VERIFICATION_BY_SOURCE[sourceType];
}
export function defaultKind(sourceType: SourceType): CaseKind { return KINDS_BY_SOURCE[sourceType][0]; }

// What each source actually recorded, in the words the frozen contract fixes.
// These labels never upgrade an observation into a formal accounting result.
export const SOURCE_LABELS: Record<SourceVerification, string> = {
  employee_observation: "员工观察，未入正式账",
  synthetic_erp: "隔离合成回执，非正式ERP",
  manual_erp_evidence: "人工核对记录",
  sales_record: "收银交接记录，以原系统为准",
  workflow_tracking: "仅跟踪记录",
  task_record: "仅跟踪记录",
};
export const SOURCE_TYPE_LABELS: Record<SourceType, string> = {
  receiving_discrepancy: "到货差异", erp_sandbox: "合成ERP批次",
  erp_intent: "ERP执行意图", sales_handoff: "收银交接", workflow: "办公流程",
  task: "任务逾期",
};
export const KIND_LABELS: Record<CaseKind, string> = {
  shortage: "少货", overage: "多货", wrong_item: "错货",
  identity_ambiguous: "身份歧义", partial: "部分成功", unknown_write: "未知写",
  overdue: "逾期", price_change: "价格变动", workflow_attention: "流程待处理",
};
export const STATE_LABELS: Record<CaseState, string> = {
  open: "待认领", owned: "已认领", transfer_pending: "待转交确认", resolved: "已关闭（仅跟踪）",
};
export const COMMAND_LABELS: Record<CommandOperation, string> = {
  claim: "认领", takeover: "接管", propose_transfer: "申请转交",
  accept_transfer: "接受转交", cancel_transfer: "取消转交",
  resolve: "关闭跟踪", reopen: "重新打开",
};
export const RESOLUTION_LABELS: Record<ResolutionKind, string> = {
  verified_resolution: "已核对来源解决", reconciled_partial: "部分结果已核对",
  verified_rejection: "已核对被拒",
};

export function sourceStateLabel(state: string): string {
  return ({ unreviewed: "待复核", accept_observation: "已接受原观察", request_recount: "待重新清点",
    hold_identity: "身份待核", hold_price: "价格待核", return_supplier_review: "待供应商核对",
    observed: "已记录观察", reviewed: "已复核观察", prepared: "已准备", dispatched: "发送结果待核",
    pending: "待核对", unknown: "结果未知", partial: "部分结果", sandbox_verified: "合成回执已核对",
    failed: "失败或拒绝", staged: "已登记执行意图", manual_rejected: "人工核对为拒绝",
    receipt_unknown: "回执未知", confirmed: "原交接已确认", sent: "原交接已发送",
    assigned: "已分派", in_progress: "进行中", verified: "已核验", closed: "原跟踪已关闭",
    active: "进行中", paused: "已暂停", needs_human: "需人工处理" } as Record<string, string>)[state] ?? state;
}
export function quantityLabel(line: SourceLine): string {
  return "本次观察 " + (line.observed_quantity ?? "未记录") + "；期望 " +
    (line.expected_quantity ?? "未记录") + "；累计观察 " + (line.cumulative_observed_quantity ?? "未记录");
}
function shortReference(value: string): string {
  return value.length > 30 ? value.slice(0, 12) + "…" + value.slice(-8) : value;
}
function unitLabel(value: string | null): string {
  if (!value) return "未记录";
  return ({ piece: "件", box: "箱", kg: "千克", meter: "米" } as Record<string, string>)[value] ?? value;
}

const base = "/api/v1/office/exceptions";
const markerPrefix = "retail.office.exception.pending.v1.";
const encoder = new TextEncoder();

async function digestHex(text: string): Promise<string> {
  const bytes = new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(text)));
  return Array.from(bytes, x => x.toString(16).padStart(2, "0")).join("");
}

/** Same canonical JSON the server hashes: sorted keys, no spaces, UTF-8 kept. */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonicalJson).join(",") + "]";
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return "{" + Object.keys(record).sort()
      .map(key => JSON.stringify(key) + ":" + canonicalJson(record[key])).join(",") + "}";
  }
  return JSON.stringify(value) ?? "null";
}

export interface RequestDigestInput {
  scope: [string, string]; actorId: string; operation: Operation;
  caseId: string | null; input: CaseCreateInput | CaseCommandInput;
}

/**
 * Exact server algorithm: SHA256(utf8(canonical JSON)) of the frozen envelope.
 * A mismatch is what the gate uses to refuse clearing an unknown marker.
 */
export async function requestDigest(value: RequestDigestInput): Promise<string> {
  return digestHex(canonicalJson({
    scope: value.scope, actor_id: value.actorId, operation: value.operation,
    case_id: value.caseId, input: value.input,
  }));
}

function requireNonBlank(text: string, field: string): string {
  if (!text.trim() || text !== text.trim()) {
    throw new Error(field + "必填，且不能以空白开头或结尾");
  }
  return text;
}

/** Build the create payload only from a freshly re-read source projection. */
export function createInput(locator: SourceLocator, source: ExceptionSource, reason: string): CaseCreateInput {
  if (source.source_type !== locator.source_type || source.source_id !== locator.source_id ||
      source.kind !== locator.kind) {
    throw new Error("来源核验结果与所选对象不一致，请重新读取来源");
  }
  if (!source.anomaly_present) throw new Error("当前来源没有所选异常，无需登记");
  return {
    source_type: source.source_type, source_id: source.source_id, kind: source.kind,
    expected_source_digest: source.source_digest,
    expected_source_version: source.source_version,
    reason: requireNonBlank(reason, "登记原因"),
  };
}

/** Build one command against the version and digest the employee is looking at. */
export function commandInput(
  view: CaseView, operation: CommandOperation, reason: string,
  extra: { toOwnerId?: string; resolutionDigest?: string } = {},
): CaseCommandInput {
  if (!view.source) throw new Error("来源当前不可读，请先重新核对");
  if (operation === "propose_transfer" && !extra.toOwnerId) {
    throw new Error("申请转交必须指定接手人");
  }
  if (operation !== "propose_transfer" && extra.toOwnerId) {
    throw new Error("只有申请转交可以指定接手人");
  }
  if (operation === "resolve" && !extra.resolutionDigest) {
    throw new Error("关闭跟踪必须绑定来源关闭依据");
  }
  if (operation === "resolve" && (resolveBlockedReason(view) !== null ||
      extra.resolutionDigest !== view.source.resolution_digest)) {
    throw new Error("关闭跟踪必须使用当前来源的完整核验依据");
  }
  if (operation !== "resolve" && extra.resolutionDigest) {
    throw new Error("只有关闭跟踪可以携带关闭依据");
  }
  return {
    operation,
    expected_version: view.version,
    expected_source_digest: view.source.source_digest,
    reason: requireNonBlank(reason, "处理依据"),
    // Both nullable keys are always present so the digest stays byte-identical
    // to the server's expectation for every operation.
    to_owner_id: operation === "propose_transfer" ? extra.toOwnerId ?? null : null,
    expected_resolution_digest: operation === "resolve" ? extra.resolutionDigest ?? null : null,
  };
}

/** Which buttons this case + this employee may render; the server still decides. */
export function actionAvailability(
  view: CaseView, subjectId: string,
): Record<CommandOperation, boolean> {
  const mine = view.owner_id === subjectId;
  const pendingMine = view.pending_owner_id === subjectId;
  const pending = view.state === "transfer_pending";
  const readable = view.source !== null && view.blocking_code === null;
  return {
    claim: readable && view.state === "open",
    // A separate server capability and explicit reason govern takeover;
    // account availability is evidence, not an invented authorization rule.
    takeover: readable && (view.state === "owned" || pending) && view.owner_id !== null && !mine,
    propose_transfer: readable && view.state === "owned" && mine,
    accept_transfer: readable && pending && pendingMine,
    cancel_transfer: readable && pending && mine,
    resolve: readable && mine && resolveBlockedReason(view) === null,
    reopen: readable && view.state === "resolved" && view.source!.anomaly_present,
  };
}

/** Why the close button is unavailable; null means it may be offered. */
export function resolveBlockedReason(view: CaseView): string | null {
  if (view.state === "resolved") return "此跟踪记录已关闭";
  if (view.state !== "owned") return "仅当前负责人可关闭跟踪";
  if (!view.source) return "来源当前不可读，无法关闭";
  if (!view.source.can_resolve) return "来源未提供关闭依据，暂不可关闭";
  if (view.source.lines.some(row => row.state === "unknown" || row.state === "pending")) {
    return "仍有未核清的来源行，暂不可关闭";
  }
  return null;
}

export interface LineVerdict {
  line: SourceLine; state: string; receipt: string; evidence: string;
  error: string | null; verified: boolean;
}

/**
 * Per-line reading of a partially successful source: only a line the source
 * itself stamped with a receipt digest counts as verified. Pending and unknown
 * lines stay visible and unverified, never merged into "all done".
 */
export function lineVerdicts(lines: SourceLine[]): LineVerdict[] {
  return lines.map(line => ({
    line, state: line.state,
    receipt: line.receipt_digest ?? (line.source_verification === "employee_observation" ? "无逐行回执" : "未核清"),
    evidence: line.evidence_digest ?? "",
    error: line.error_code,
    verified: line.receipt_digest !== null && line.state !== "unknown" && line.state !== "pending",
  }));
}

export function receiptMatches(
  marker: { digest: string; case: string | null }, view: CaseView, subjectId: string,
): boolean {
  const receipt = view.command_receipt;
  if (view.effect !== "case_tracking_only" || view.mode !== "human_supervised" ||
      !Number.isInteger(view.version) || view.version <= 0 || !CASE_STATES.includes(view.state)) return false;
  if (!receipt || !receipt.id || !Number.isInteger(receipt.case_version) || receipt.case_version <= 0 ||
      receipt.case_version > view.version) return false;
  if (receipt.actor_id !== subjectId) return false;
  if (receipt.request_digest !== marker.digest) return false;
  if (receipt.case_id !== view.id) return false;
  if (!receipt.references || receipt.references.source_id !== view.source_id ||
      receipt.references.source_type !== view.source_type || receipt.references.kind !== view.kind ||
      receipt.references.source_digest !== receipt.source_digest) return false;
  return marker.case === null ? receipt.operation === "create" && receipt.case_version === 1 : receipt.case_id === marker.case;
}

/** A history response may never be reused under a different Case heading. */
export function historyRowsForCase(caseId: string, page: CaseReceiptPage, expectedVersion?: number): CaseCommandReceipt[] {
  if (page.case_id !== caseId || !Array.isArray(page.receipts) || page.receipts.some(row => row.case_id !== caseId)) {
    throw new Error("交接历史与当前异常不一致，请重新读取");
  }
  if (expectedVersion !== undefined && page.case_version !== expectedVersion) {
    throw new ApiError("读取期间异常版本已变化", { status: 409, code: "CASE_VIEW_CHANGED",
      userMessage: "读取期间异常记录已更新，请重新打开并核对。" });
  }
  return page.receipts;
}

interface Marker { key: string; digest: string; case: string | null }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function validMarker(value: unknown): value is Marker {
  if (!value || typeof value !== "object") return false;
  const x = value as Record<string, unknown>;
  return typeof x.key === "string" && uuid.test(x.key) &&
    typeof x.digest === "string" && /^[0-9a-f]{64}$/.test(x.digest) &&
    (x.case === null || (typeof x.case === "string" && x.case.length > 0));
}

/**
 * Persisted write gate. A marker holds only an opaque idempotency key, the
 * request digest and the target case id: never the token, reason text, source
 * content, quantities or prices. A lost reply leaves the marker in place and
 * only the read-only status endpoint may clear it, after an exact match.
 */
export class ExceptionCommandGate {
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
    const hash = await digestHex(JSON.stringify([identity.organizationId, identity.storeId, identity.subjectId]));
    if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
    this.namespace = markerPrefix + hash;
    try { this.unknown = this.markerKeys().length > 0; }
    catch { throw new Error("无法保存操作状态，请检查浏览器存储后重试"); }
  }
  private async exclusive<T>(scope: number, work: () => Promise<T>): Promise<T> {
    const identity = getSession();
    if (!identity || scope !== getScopeVersion()) throw new Error("登录身份已变化");
    const name = markerPrefix + await digestHex(JSON.stringify([
      identity.organizationId, identity.storeId, identity.subjectId]));
    if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
    if (!globalThis.navigator?.locks?.request) {
      throw new Error("浏览器不支持跨页面操作锁，请更换浏览器");
    }
    return globalThis.navigator.locks.request(name, { ifAvailable: true }, async lock => {
      if (!lock) throw new Error("另一页面正在处理操作，请回查后再试");
      if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
      return work();
    });
  }
  async run<T extends CaseView>(
    scope: number, path: string, body: CaseCreateInput | CaseCommandInput,
    target: { operation: Operation; caseId: string | null }, baseUrl = "",
  ): Promise<T> {
    if (scope !== getScopeVersion() || this.pending || this.unknown) {
      throw new Error("操作未完成，请先回查记录并联系负责人核实");
    }
    this.pending = true;
    let markerKey = "";
    try {
      return await this.exclusive(scope, async () => {
        await this.hydrate(scope);
        if (this.unknown) throw new Error("操作未完成，请先回查记录并联系负责人核实");
        const identity = getSession();
        if (!identity || scope !== getScopeVersion()) throw new Error("登录身份已变化");
        const digest = await requestDigest({
          scope: [identity.organizationId, identity.storeId], actorId: identity.subjectId,
          operation: target.operation, caseId: target.caseId, input: body,
        });
        if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
        const key = crypto.randomUUID();
        const marker: Marker = { key, digest, case: target.caseId };
        markerKey = this.namespace + "." + key;
        // If the marker cannot be persisted, never send the write.
        try { globalThis.localStorage.setItem(markerKey, JSON.stringify(marker)); }
        catch { markerKey = ""; throw new Error("无法保存操作状态，请检查浏览器存储后重试"); }
        try {
          const result = await authorizedRequest<T>(path, {
            method: "POST", body, baseUrl, headers: { "Idempotency-Key": key },
          });
          if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
          const expectedVersion = target.operation === "create" ? 1 : (body as CaseCommandInput).expected_version + 1;
          if (!result || !receiptMatches(marker, result, identity.subjectId) ||
              result.command_receipt?.operation !== target.operation || result.command_receipt.case_version !== expectedVersion) {
            throw new Error("服务端结果不完整，请回查记录");
          }
          globalThis.localStorage.removeItem(markerKey);
          markerKey = "";
          return result;
        } catch (error) {
          if (markerKey) {
            // Only a definite rejection is clearable while this employee lock
            // is held. 404/timeout/rate-limit/network/5xx retain recovery.
            const uncertain = scope !== getScopeVersion() || !(error instanceof ApiError) ||
              error.status === 0 || error.status === 404 || error.status === 408 || error.status === 429 || error.status >= 500 ||
              error.code === "INVALID_ENVELOPE";
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
  async reconcile(scope: number, baseUrl = ""): Promise<CaseView[]> {
    if (this.pending || scope !== getScopeVersion()) throw new Error("操作进行中或登录身份已变化");
    this.pending = true;
    try {
      return await this.exclusive(scope, async () => {
        await this.hydrate(scope);
        const recovered: CaseView[] = [];
        for (const storageKey of this.markerKeys()) {
          let marker: unknown;
          try { marker = JSON.parse(globalThis.localStorage.getItem(storageKey) ?? "null"); }
          catch { continue; }
          if (!validMarker(marker) || storageKey !== this.namespace + "." + marker.key) continue;
          const identity = getSession();
          if (!identity || scope !== getScopeVersion()) throw new Error("登录身份已变化");
          const status = await authorizedRequest<CaseCommandStatus>(
            base + "/commands/status?key=" + encodeURIComponent(marker.key) +
            "&request_digest=" + marker.digest, { baseUrl });
          if (scope !== getScopeVersion()) throw new Error("登录身份已变化");
          // not_found never proves the write did not land: keep the marker.
          if (status?.status === "found" && status.case &&
              receiptMatches(marker, status.case, identity.subjectId)) {
            globalThis.localStorage.removeItem(storageKey);
            recovered.push(status.case);
          }
        }
        this.unknown = this.markerKeys().length > 0;
        return recovered;
      });
    } finally { this.pending = false; }
  }
}

let gateScope = -1;
let scopeGate = new ExceptionCommandGate();
function gateFor(scope: number): ExceptionCommandGate {
  if (gateScope !== scope) { gateScope = scope; scopeGate = new ExceptionCommandGate(); }
  return scopeGate;
}
export function OFC007Page({ session = getSession(), baseUrl = "", initialItems = [] }: {
  session?: Session | null; baseUrl?: string; initialItems?: CaseSummary[];
}) {
  const scope = getScopeVersion();
  const mounted = useRef(true);
  const queryEpoch = useRef(0);
  const gate = gateFor(scope);
  const [items, setItems] = useState<CaseSummary[]>(session ? initialItems : []);
  const [cursor, setCursor] = useState<string | null>(null);
  const [sourceType, setSourceType] = useState<SourceType>("receiving_discrepancy");
  const [kindFilter, setKindFilter] = useState<CaseKind | "">("");
  const [stateFilter, setStateFilter] = useState<CaseState | "">("");
  const [ownerFilter, setOwnerFilter] = useState<"" | "me" | "unassigned">("");
  const [sourceId, setSourceId] = useState("");
  const [lookupKind, setLookupKind] = useState<CaseKind>(defaultKind("receiving_discrepancy"));
  const [source, setSource] = useState<ExceptionSource | null>(null);
  const [createReason, setCreateReason] = useState("");
  const [selected, setSelected] = useState<CaseView | null>(null);
  const [history, setHistory] = useState<CaseCommandReceipt[]>([]);
  const [historyNext, setHistoryNext] = useState<number | null>(null);
  const [reason, setReason] = useState("");
  const [toOwner, setToOwner] = useState("");
  const [busy, setBusy] = useState(false);
  const [gateReady, setGateReady] = useState(false);
  const [message, setMessage] = useState("");
  const current = () => mounted.current && scope === getScopeVersion();
  const invalidateQueue = () => {
    queryEpoch.current++;
    setItems([]); setCursor(null);
  };
  const showError = (e: unknown) => {
    if (!current()) return;
    if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
      invalidateQueue(); setSelected(null); setHistory([]); setHistoryNext(null); setSource(null);
      setSourceId(""); setCreateReason(""); setReason(""); setToOwner("");
    }
    if (e instanceof ApiError && e.status === 409) {
      setSelected(null); setHistory([]); setHistoryNext(null); setSource(null);
      setReason(""); setToOwner("");
    }
    setMessage(e instanceof ApiError ? e.userMessage : e instanceof Error ? e.message : "请求失败");
  };
  function listPath(next: string | null): string {
    const query = new URLSearchParams({ source_type: sourceType, limit: "50" });
    if (kindFilter) query.set("kind", kindFilter);
    if (stateFilter) query.set("state", stateFilter);
    if (ownerFilter) query.set("owner", ownerFilter);
    if (next) query.set("cursor", next);
    return base + "?" + query.toString();
  }
  async function refresh(next: string | null = null, epoch = ++queryEpoch.current) {
    const page = await authorizedRequest<CaseListPage>(listPath(next), { baseUrl });
    if (!current() || epoch !== queryEpoch.current) return;
    setItems(page.items);
    setCursor(page.next_cursor);
  }
  function refreshFromUi(next: string | null = null) {
    const epoch = ++queryEpoch.current;
    void refresh(next, epoch).catch(e => {
      if (!current()) return;
      const authorizationError = e instanceof ApiError && (e.status === 401 || e.status === 403);
      if (authorizationError || epoch === queryEpoch.current) showError(e);
    });
  }
  async function openCase(id: string) {
    setBusy(true); setMessage("");
    try {
      const view = await authorizedRequest<CaseView>(base + "/" + encodeURIComponent(id), { baseUrl });
      const page = await authorizedRequest<CaseReceiptPage>(
        base + "/" + encodeURIComponent(id) + "/receipts?after_version=0&limit=100", { baseUrl });
      if (!current()) return;
      const receipts = historyRowsForCase(id, page, view.version);
      setSelected(view); setHistory(receipts); setHistoryNext(page.next_after_version); setReason(""); setToOwner("");
    } catch (e) {
      showError(e);
    } finally { if (current()) setBusy(false); }
  }
  async function loadSource() {
    const id = sourceId.trim();
    if (!id) { setMessage("请先录入来源对象 ID"); return; }
    setBusy(true); setMessage("");
    try {
      const query = new URLSearchParams({
        source_type: sourceType, source_id: id, kind: lookupKind,
      });
      const projection = await authorizedRequest<ExceptionSource>(
        base + "/source-context?" + query.toString(), { baseUrl });
      if (current()) { setSource(projection); setMessage("已读取来源当前状态，请核对后再登记异常"); }
    } catch (e) { if (current()) setSource(null); showError(e); }
    finally { if (current()) setBusy(false); }
  }
  async function registerCase() {
    if (!source) { setMessage("请先读取来源"); return; }
    const locator: SourceLocator = { source_type: sourceType, source_id: source.source_id, kind: lookupKind };
    setBusy(true); setMessage("");
    try {
      const body = createInput(locator, source, createReason);
      const view = await gate.run<CaseView>(scope, base, body, { operation: "create", caseId: null }, baseUrl);
      if (!current()) return;
      setSelected(view); setHistory([]); setHistoryNext(null); setCreateReason("");
      setMessage("异常已登记到跟踪队列；来源业务仍需原领域人工处理");
      await refresh(null);
      const page = await authorizedRequest<CaseReceiptPage>(
        base + "/" + encodeURIComponent(view.id) + "/receipts?after_version=0&limit=100", { baseUrl });
      if (current()) { setHistory(historyRowsForCase(view.id, page, view.version)); setHistoryNext(page.next_after_version); }
    } catch (e) { showError(e); }
    finally { if (current()) setBusy(false); }
  }
  async function command(operation: CommandOperation) {
    if (!selected) return;
    setBusy(true); setMessage("");
    try {
      const extra = operation === "propose_transfer" ? { toOwnerId: toOwner.trim() } :
        operation === "resolve" ? { resolutionDigest: selected.source?.resolution_digest ?? "" } : {};
      const body = commandInput(selected, operation, reason, extra);
      const view = await gate.run<CaseView>(scope, base + "/" + encodeURIComponent(selected.id) + "/commands",
        body, { operation, caseId: selected.id }, baseUrl);
      if (!current()) return;
      setSelected(view); setHistory([]); setHistoryNext(null); setReason(""); setToOwner("");
      setMessage("跟踪记录已更新；这不代表来源业务已关闭");
      await refresh(null);
      const page = await authorizedRequest<CaseReceiptPage>(
        base + "/" + encodeURIComponent(view.id) + "/receipts?after_version=0&limit=100", { baseUrl });
      if (current()) { setHistory(historyRowsForCase(view.id, page, view.version)); setHistoryNext(page.next_after_version); }
    } catch (e) {
      if (current() && e instanceof ApiError) {
        if (e.status === 409) setMessage("对象版本或来源已变化：已停下来，请核对最新记录后再由人重新提交");
        if (e.status === 403) { setSelected(null); setHistory([]); setMessage("已失去该异常的查看或处理权限"); }
      }
      showError(e);
    } finally { if (current()) setBusy(false); }
  }
  async function reconcilePending() {
    setBusy(true); setMessage("");
    try {
      const recovered = await gate.reconcile(scope, baseUrl);
      if (!current()) return;
      const currentCase = selected ? recovered.find(view => view.id === selected.id) : recovered.at(-1);
      if (currentCase) {
        setSelected(currentCase); setHistory([]); setHistoryNext(null); setReason(""); setToOwner("");
        const page = await authorizedRequest<CaseReceiptPage>(
          base + "/" + encodeURIComponent(currentCase.id) + "/receipts?after_version=0&limit=100", { baseUrl });
        if (current()) { setHistory(historyRowsForCase(currentCase.id, page, currentCase.version)); setHistoryNext(page.next_after_version); }
      }
      if (current()) setMessage(gate.unknown
        ? "仍有未知结果，请核对原系统并联系负责人，勿重复提交"
        : "回查完成：没有待回查的写入，可以继续");
      if (current()) await refresh(null);
    } catch (e) { showError(e); }
    finally { if (current()) setBusy(false); }
  }
  async function loadHistory(after: number) {
    if (!selected) return;
    setBusy(true); setMessage("");
    try {
      const page = await authorizedRequest<CaseReceiptPage>(
        base + "/" + encodeURIComponent(selected.id) + "/receipts?after_version=" + after + "&limit=100", { baseUrl });
      if (current()) { setHistory(historyRowsForCase(selected.id, page, selected.version)); setHistoryNext(page.next_after_version); }
    } catch (e) { showError(e); }
    finally { if (current()) setBusy(false); }
  }
  useEffect(() => {
    mounted.current = true;
    if (session) {
      setGateReady(false);
      void gate.hydrate(scope).then(() => { if (current()) setGateReady(true); }).catch(showError);
      refreshFromUi(null);
    }
    return () => { mounted.current = false; };
  }, [scope, baseUrl]);
  if (!session) return <p>请先登录员工账号</p>;
  const locked = busy || !gateReady || gate.pending || gate.unknown;
  const kinds = kindsFor(sourceType);
  const availability = selected ? actionAvailability(selected, session.subjectId) : null;
  const resolveBlocked = selected ? resolveBlockedReason(selected) : "尚未选择异常";
  const sourceReady = source !== null && source.source_type === sourceType &&
    source.source_id === sourceId.trim() && source.kind === lookupKind;
  return <section className="office-exceptions" aria-label="异常与交接队列" style={{ overflowWrap: "anywhere" }}>
    <h2>异常与交接</h2>
    <p>少货、多货、错货、身份歧义、部分成功、未知写和逾期都在同一个队列。此处只登记跟踪与交接，不替代原领域关闭，也不重发任何未知写。</p>
    {message && <p role="status">{message}</p>}
    {gate.unknown && <div role="alert">
      <p>上次写入结果未知，已暂停提交。请用只读核对确认，不要把未知当失败重试。</p>
      <button type="button" disabled={busy} onClick={() => void reconcilePending()}>回查已登记的操作</button>
    </div>}
    <section aria-label="队列筛选">
      <label>来源 <select value={sourceType} disabled={busy} onChange={e => {
        const next = e.target.value as SourceType;
        invalidateQueue(); setSelected(null); setHistory([]); setHistoryNext(null); setReason(""); setToOwner("");
        setSourceType(next); setKindFilter(""); setLookupKind(defaultKind(next));
        setSource(null); setSourceId(""); setCursor(null);
      }}>{SOURCE_TYPES.map(x => <option key={x} value={x}>{SOURCE_TYPE_LABELS[x]}</option>)}</select></label>
      <label>异常类型 <select value={kindFilter} disabled={busy} onChange={e => {
        invalidateQueue(); setKindFilter(e.target.value as CaseKind | "");
      }}>
        <option value="">全部（按来源）</option>
        {kinds.map(x => <option key={x} value={x}>{KIND_LABELS[x]}</option>)}
      </select></label>
      <label>状态 <select value={stateFilter} disabled={busy} onChange={e => {
        invalidateQueue(); setStateFilter(e.target.value as CaseState | "");
      }}>
        <option value="">全部</option>{CASE_STATES.map(x => <option key={x} value={x}>{STATE_LABELS[x]}</option>)}
      </select></label>
      <label>负责人 <select value={ownerFilter} disabled={busy} onChange={e => {
        invalidateQueue(); setOwnerFilter(e.target.value as "" | "me" | "unassigned");
      }}>
        <option value="">全部</option><option value="me">我的</option><option value="unassigned">未分配</option>
      </select></label>
      <button type="button" disabled={busy} onClick={() => refreshFromUi(null)}>刷新队列</button>
    </section>
    <ul>{items.map(row => <li key={row.id}>
      <button type="button" disabled={busy} onClick={() => void openCase(row.id)}>
        {KIND_LABELS[row.kind]} · {STATE_LABELS[row.state]} · {row.source_id}
        {row.source_version === null ? " · 无整数版本" : " · 登记时来源版本 " + row.source_version}
        {" · "}{row.owner_id ?? "未分配"}
        {row.owner_id !== null && !row.owner_available ? "（负责人不可用，保留可见）" : ""}
        {row.pending_owner_id ? " · 待转交给 " + row.pending_owner_id : ""}
      </button>
    </li>)}</ul>
    {items.length === 0 && <p>当前没有可查看的异常记录。</p>}
    <button type="button" disabled={busy || !cursor} onClick={() => refreshFromUi(cursor)}>下一页</button>
    <section aria-label="登记异常">
      <h3>先核验来源，再登记异常</h3>
      <label>来源对象 ID <input value={sourceId} maxLength={36} disabled={locked}
        onChange={e => { setSourceId(e.target.value); setSource(null); }} /></label>
      <label>异常类型 <select value={lookupKind} disabled={locked}
        onChange={e => setLookupKind(e.target.value as CaseKind)}>
        {kinds.map(x => <option key={x} value={x}>{KIND_LABELS[x]}</option>)}
      </select></label>
      <button type="button" disabled={locked} onClick={() => void loadSource()}>读取来源</button>
      {source && <div>
        <p>来源 {SOURCE_TYPE_LABELS[source.source_type]} · {LABEL_OR(source.source_verification)}</p>
        <p>状态 {sourceStateLabel(source.source_state)} · {source.source_version === null ? "无整数版本" : "版本 " + source.source_version}</p>
        <details><summary>来源编号与核验摘要</summary><p>来源 {source.source_id}</p><p>摘要 {source.source_digest}</p></details>
        {!source.anomaly_present && <p>当前来源未显示所选异常，无需登记。</p>}
        <p>{source.can_resolve ? "来源已提供关闭依据：" + RESOLUTION_LABELS[source.resolution_kind ?? "verified_resolution"]
          : "来源未提供关闭依据，登记后暂不可关闭"}</p>
        {source.lines.length > 0 && <table>
          <thead><tr><th>行</th><th>状态</th><th>数量</th><th>单位</th><th>收据摘要</th><th>错误码</th></tr></thead>
          <tbody>{lineVerdicts(source.lines).map(row => <tr key={row.line.id}>
            <td><code title={row.line.id}>{shortReference(row.line.id)}</code></td><td>{sourceStateLabel(row.state)}</td>
            <td>{quantityLabel(row.line)}</td>
            <td title={row.line.unit ?? ""}>{unitLabel(row.line.unit)}</td>
            <td><code title={row.receipt}>{shortReference(row.receipt)}</code></td>
            <td>{row.error ?? ""}</td>
          </tr>)}</tbody>
        </table>}
        {source.source_verification === "employee_observation" && <p>数量保留原观察身份；包装换算未经核实时不作换算。</p>}
      </div>}
      <label>登记原因 <input value={createReason} maxLength={1000} disabled={locked}
        onChange={e => setCreateReason(e.target.value)} /></label>
      <button type="button" disabled={locked || !sourceReady || !source?.anomaly_present || !createReason.trim()}
        onClick={() => void registerCase()}>登记异常</button>
    </section>
    {selected && <section aria-label="异常详情">
      <h3>{KIND_LABELS[selected.kind]} · {selected.id}</h3>
      <p>状态 {STATE_LABELS[selected.state]} · 跟踪版本 {selected.version} · 来源 {selected.source_id}</p>
      <p>{selected.owner_id ?? "未分配"} {selected.owner_id && !selected.owner_available ? "（负责人不可用，保留可见）" : ""}
        {selected.pending_owner_id ? " · 待转交 " + selected.pending_owner_id + "，原负责人仍需负责" : ""}</p>
      {selected.state === "owned" && selected.owner_id !== session.subjectId && selected.owner_available &&
        <p>该异常已有负责人：普通转交和关闭由其处理；接管另需明确权限与依据。</p>}
      {selected.state === "transfer_pending" && selected.owner_id !== session.subjectId &&
        selected.pending_owner_id !== session.subjectId &&
        <p>转交待接手人确认，原负责人仍负责；其他员工不能代为确认或关闭。</p>}
      {selected.source_changed && <p role="alert">来源内容已变化，请重新核对后再提交；不要沿用旧摘要。</p>}
      {!selected.source && <p role="alert">来源当前不可读（{selected.blocking_code}），仍保留这条跟踪记录。</p>}
      {selected.source && <div>
        <p>{LABEL_OR(selected.source.source_verification)} · 状态 {sourceStateLabel(selected.source.source_state)}
          {" · "}{selected.source.source_version === null ? "无整数版本" : "版本 " + selected.source.source_version}</p>
        <details><summary>来源编号、证据引用与核验摘要</summary>
          <p>来源 {selected.source.source_id}</p><p>摘要 {selected.source.source_digest}</p>
          <ul>{selected.source.references.map(ref => <li key={ref.kind + ":" + ref.id}>
            {ref.kind} · {ref.id}{ref.version === null ? "" : " · 版本 " + ref.version}{ref.digest ? " · " + ref.digest : ""}
          </li>)}</ul>
        </details>
        {selected.source.anomaly_present && !selected.source.can_resolve &&
          <p>来源仍显示异常且未提供关闭依据：可跟踪，不可关闭。</p>}
        {selected.source.lines.length > 0 && <table>
          <thead><tr><th>行</th><th>状态</th><th>数量</th><th>单位</th><th>收据摘要</th><th>证据摘要</th><th>错误码</th></tr></thead>
          <tbody>{lineVerdicts(selected.source.lines).map(row => <tr key={row.line.id}>
            <td><code title={row.line.id}>{shortReference(row.line.id)}</code></td><td>{sourceStateLabel(row.state)}</td>
            <td>{quantityLabel(row.line)}</td>
            <td title={row.line.unit ?? ""}>{unitLabel(row.line.unit)}</td>
            <td><code title={row.receipt}>{shortReference(row.receipt)}</code></td>
            <td><code title={row.evidence}>{shortReference(row.evidence)}</code></td>
            <td>{row.error ?? ""}</td>
          </tr>)}</tbody>
        </table>}
        {selected.source.source_verification === "employee_observation" && <p>数量保留原观察身份；包装换算未经核实时不作换算。</p>}
      </div>}
      <label>处理依据 <input value={reason} maxLength={1000} disabled={locked}
        onChange={e => setReason(e.target.value)} /></label>
      {availability?.propose_transfer && <label>接手人 ID
        <input value={toOwner} maxLength={36} disabled={locked} onChange={e => setToOwner(e.target.value)} /></label>}
      {COMMAND_OPERATIONS.map(operation => {
        const blocked = resolveBlockedReason(selected);
        const usable = availability ? availability[operation] : false;
        const needsTarget = operation === "propose_transfer" && !toOwner.trim();
        const disabled = locked || !usable || !reason.trim() || needsTarget ||
          (operation === "resolve" && (blocked !== null || !selected.source?.resolution_digest));
        return <button key={operation} type="button" disabled={disabled}
          onClick={() => void command(operation)}>{COMMAND_LABELS[operation]}</button>;
      })}
      {resolveBlocked !== null && <p>{resolveBlocked}；关闭只表示“已核对跟踪”，不代表来源业务已关闭。</p>}
      <p>未知写与部分成功不提供重发按钮；请回原系统人工核对，本页不调用任何原领域写入接口。</p>
      <h4>不可变历史</h4>
      <button type="button" disabled={busy} onClick={() => void loadHistory(0)}>历史首页</button>
      <button type="button" disabled={busy || historyNext === null}
        onClick={() => { if (historyNext !== null) void loadHistory(historyNext); }}>下一页历史</button>
      <table><thead><tr><th>操作</th><th>跟踪版本</th><th>经办</th><th>来源摘要</th><th>时间</th></tr></thead>
        <tbody>{history.map(row => <tr key={row.id}>
          <td>{row.operation === "create" ? "登记" : COMMAND_LABELS[row.operation]}</td><td>{row.case_version}</td><td>{row.actor_id}</td>
          <td><code title={row.source_digest}>{shortReference(row.source_digest)}</code></td>
          <td><time dateTime={row.created_at} title={row.created_at}>{new Date(row.created_at).toLocaleString("zh-CN", { hour12: false })}</time></td>
        </tr>)}</tbody></table>
    </section>}
  </section>;
}

function LABEL_OR(value: SourceVerification): string {
  return SOURCE_LABELS[value] ?? String(value);
}
