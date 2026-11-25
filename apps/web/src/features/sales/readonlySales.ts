import {
  authorizedRequest, getScopeVersion, getSession, sessionIsExpired,
  subscribe as subscribeSession,
} from "../../lib/auth/session";
import { hasCapability, subscribeCapabilities } from "../../lib/auth/capabilities";

export interface SalesLine {
  id: string; line_key: string; product_id: string; product_name: string;
  quantity: string; unit_price: string; discount_bps: number; line_amount: string;
  quote_id: string | null; confirmed_by: string | null; confirmed_at: string | null;
}
export interface SalesHandoff {
  id: string; handoff_state: "requested" | "sent" | "receipt_unknown" | "confirmed" | "failed";
  external_ref: string | null; created_at: string; updated_at: string;
}
export interface SalesDraft {
  id: string; organization_id: string; store_id: string; creator_id: string;
  quote_id: string | null; stage: "draft" | "ready"; status: "pending" | "paid";
  sync_state: "local_only" | "pending" | "synced" | "conflict";
  version: number; currency: string; total_amount: string;
  voided_at: string | null; expires_at: string | null; created_at: string; updated_at: string;
  lines: SalesLine[]; handoffs: SalesHandoff[];
}
export interface SalesReadonlyState {
  access: "allowed" | "forbidden";
  listStatus: "idle" | "loading" | "ready" | "error";
  drafts: SalesDraft[];
  listError: string | null;
  selectedId: string | null;
  detailStatus: "idle" | "loading" | "ready" | "error";
  detail: SalesDraft | null;
  detailError: string | null;
}

const unavailable = () => new Error("sales_data_unavailable");
function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw unavailable();
  return value as Record<string, unknown>;
}
function string(value: unknown): string {
  if (typeof value !== "string") throw unavailable();
  return value;
}
function nullable(value: unknown): string | null {
  return value === null ? null : string(value);
}
function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) throw unavailable();
  return value;
}
function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  if (typeof value !== "string" || !choices.includes(value as T)) throw unavailable();
  return value as T;
}
function optionalArray<T>(value: unknown, parse: (item: unknown) => T): T[] {
  // These two fields alone have an explicit [] default in DraftOut.
  if (value === undefined) return [];
  if (!Array.isArray(value)) throw unavailable();
  return value.map(parse);
}
function parseLine(value: unknown): SalesLine {
  const v = record(value);
  return {
    id: string(v.id), line_key: string(v.line_key), product_id: string(v.product_id),
    product_name: string(v.product_name), quantity: string(v.quantity),
    unit_price: string(v.unit_price), discount_bps: integer(v.discount_bps),
    line_amount: string(v.line_amount), quote_id: v.quote_id === undefined ? null : nullable(v.quote_id),
    confirmed_by: nullable(v.confirmed_by), confirmed_at: nullable(v.confirmed_at),
  };
}
function parseHandoff(value: unknown): SalesHandoff {
  const v = record(value);
  return {
    id: string(v.id), handoff_state: choice(v.handoff_state, ["requested", "sent", "receipt_unknown", "confirmed", "failed"]),
    external_ref: nullable(v.external_ref), created_at: string(v.created_at), updated_at: string(v.updated_at),
  };
}
function parseDraft(value: unknown, organizationId: string, storeId: string): SalesDraft {
  const v = record(value);
  if (v.organization_id !== organizationId || v.store_id !== storeId || !string(v.id)) throw unavailable();
  return {
    id: string(v.id), organization_id: string(v.organization_id), store_id: string(v.store_id),
    creator_id: string(v.creator_id), quote_id: nullable(v.quote_id),
    stage: choice(v.stage, ["draft", "ready"]), status: choice(v.status, ["pending", "paid"]),
    sync_state: choice(v.sync_state, ["local_only", "pending", "synced", "conflict"]),
    version: integer(v.version), currency: string(v.currency), total_amount: string(v.total_amount),
    voided_at: nullable(v.voided_at), expires_at: nullable(v.expires_at),
    created_at: string(v.created_at), updated_at: string(v.updated_at),
    lines: optionalArray(v.lines, parseLine), handoffs: optionalArray(v.handoffs, parseHandoff),
  };
}

function canRead(): boolean {
  const session = getSession();
  return !!session && !sessionIsExpired(session) && hasCapability("sales.read");
}
function emptyState(): SalesReadonlyState {
  return {
    access: canRead() ? "allowed" : "forbidden", listStatus: "idle", drafts: [], listError: null,
    selectedId: null, detailStatus: "idle", detail: null, detailError: null,
  };
}
function forbidden(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error &&
    ((error as { status: unknown }).status === 403 || (error as { status: unknown }).status === 401);
}

/** Read-only page state. Sequence checks discard obsolete results even if fetch ignores abort. */
export function createSalesReadonlyController() {
  let state = emptyState();
  let active = false;
  let lifecycle = 0;
  let generation = 0;
  let listSequence = 0;
  let detailSequence = 0;
  let unsubscribeSession: (() => void) | undefined;
  let unsubscribeCapabilities: (() => void) | undefined;
  const listeners = new Set<() => void>();
  const publish = (next: SalesReadonlyState) => {
    state = next;
    for (const listener of listeners) listener();
  };
  const reset = () => {
    generation += 1;
    listSequence += 1;
    detailSequence += 1;
    publish(emptyState());
  };
  const deny = () => {
    reset();
    publish({ ...state, access: "forbidden", listError: "当前身份无权读取销售草稿，请核对权限后重试。" });
  };
  const current = (epoch: number, scope: number) => {
    if (!active || generation !== epoch || getScopeVersion() !== scope) return false;
    if (!canRead()) { deny(); return false; }
    return true;
  };

  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    start() {
      if (active) return;
      active = true;
      lifecycle += 1;
      unsubscribeSession = subscribeSession(reset);
      unsubscribeCapabilities = subscribeCapabilities(reset);
      reset();
    },
    stop() {
      active = false;
      unsubscribeSession?.();
      unsubscribeCapabilities?.();
      unsubscribeSession = undefined;
      unsubscribeCapabilities = undefined;
      reset();
    },
    closeDetail() {
      detailSequence += 1;
      publish({ ...state, selectedId: null, detailStatus: "idle", detail: null, detailError: null });
    },
    async refresh(): Promise<void> {
      if (!active) return;
      if (!canRead()) { deny(); return; }
      reset(); // Refresh invalidates old results, but not same-session authorization failures.
      const epoch = generation;
      const requestLifecycle = lifecycle;
      const scope = getScopeVersion();
      const sequence = ++listSequence;
      const session = getSession()!;
      publish({ ...state, access: "allowed", listStatus: "loading" });
      try {
        const data = await authorizedRequest<unknown>("/api/v1/sales/drafts", { method: "GET" });
        if (!current(epoch, scope) || sequence !== listSequence) return;
        if (!Array.isArray(data)) throw unavailable(); // null is unavailable, never an empty list.
        const drafts = data.map((item) => parseDraft(item, session.organizationId, session.storeId));
        if (new Set(drafts.map((draft) => draft.id)).size !== drafts.length) throw unavailable();
        publish({ ...state, listStatus: "ready", drafts, listError: null });
      } catch (error) {
        // Query age does not supersede an authorization denial in this mounted session.
        if (forbidden(error)) {
          if (active && lifecycle === requestLifecycle && getScopeVersion() === scope) deny();
          return;
        }
        if (!current(epoch, scope) || sequence !== listSequence) return;
        publish({ ...state, listStatus: "error", drafts: [], listError: "销售草稿列表不可用，请重试。" });
      }
    },
    async selectDraft(id: string): Promise<void> {
      if (!active) return;
      if (!canRead()) { deny(); return; }
      if (state.access !== "allowed" || state.listStatus !== "ready" || !state.drafts.some((draft) => draft.id === id)) return;
      const epoch = generation;
      const requestLifecycle = lifecycle;
      const scope = getScopeVersion();
      const sequence = ++detailSequence;
      const session = getSession()!;
      publish({ ...state, selectedId: id, detailStatus: "loading", detail: null, detailError: null });
      try {
        const data = await authorizedRequest<unknown>(`/api/v1/sales/drafts/${encodeURIComponent(id)}`, { method: "GET" });
        if (!current(epoch, scope) || sequence !== detailSequence) return;
        const detail = parseDraft(data, session.organizationId, session.storeId);
        if (detail.id !== id) throw unavailable();
        publish({ ...state, detailStatus: "ready", detail, detailError: null });
      } catch (error) {
        // Query age does not supersede an authorization denial in this mounted session.
        if (forbidden(error)) {
          if (active && lifecycle === requestLifecycle && getScopeVersion() === scope) deny();
          return;
        }
        if (!current(epoch, scope) || sequence !== detailSequence) return;
        // Includes the backend's current 400 not-found contract; never manufacture a 404.
        publish({ ...state, detailStatus: "error", detail: null, detailError: "草稿详情不可用，请刷新列表后重新选择。" });
      }
    },
  };
}
