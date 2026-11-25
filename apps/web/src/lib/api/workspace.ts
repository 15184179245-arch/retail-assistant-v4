import { ApiError } from "./errors";
import { queryClient } from "./query";
import {
  authorizedRequest,
  captureSessionScope,
  getScopeVersion,
  getSession,
  registerScopeController,
} from "../auth/session";
import type {
  ArtifactKind,
  ArtifactOpDto,
  OpenObjectRefDto,
  PendingSuggestionDto,
  ThreadWorkspaceDto,
  UiBlockDto,
  WorkspaceArtifactDto,
} from "./generated";

export interface ThreadMessageDto {
  id: string;
  thread_id: string;
  role: "user" | "assistant" | "system";
  text: string;
  blocks: UiBlockDto[];
  attached_objects?: OpenObjectRefDto[];
  created_at: string;
  degraded_reason?: string | null;
}

export interface ArtifactMutationResult {
  artifact: WorkspaceArtifactDto;
  op: ArtifactOpDto;
  message?: string | null;
  skipped_fields: string[];
  pending_suggestions_added: PendingSuggestionDto[];
  idempotent_replay?: boolean;
}

export interface ArtifactDetailResult {
  artifact: WorkspaceArtifactDto;
  ops: ArtifactOpDto[];
}

export interface PinBlockResult {
  artifact: WorkspaceArtifactDto;
  resolved_block?: Record<string, unknown>;
  op?: ArtifactOpDto;
  object_ref: OpenObjectRefDto;
}

export type SubmitOutcome =
  | {
      status: "verified";
      artifact: WorkspaceArtifactDto;
      receipt_ref: string | null;
      message: string;
    }
  | {
      status: "unverified";
      artifact?: WorkspaceArtifactDto;
      receipt_ref?: string | null;
      message: string;
      error?: string;
    }
  | {
      status: "failed";
      message: string;
      error: string;
    };

export interface WorkspaceEventEnvelope {
  id: string | null;
  event: string;
  data: Record<string, unknown>;
}

export type StreamConnectionState = "connecting" | "open" | "reconnecting" | "closed";

export interface WorkspaceStreamOptions {
  threadId?: string | null;
  initialCursor?: string | null;
  onEvent: (event: WorkspaceEventEnvelope) => void;
  onStateChange?: (state: StreamConnectionState) => void;
  reconnectDelayMs?: number;
}

const activeStreamClosers = new Set<() => void>();
let clientDeviceId: string | null = null;

export function getClientDeviceId(): string {
  if (clientDeviceId) {
    return clientDeviceId;
  }
  const suffix =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  clientDeviceId = `web-${suffix}`;
  return clientDeviceId;
}

export function getActiveWorkspaceStreamCount(): number {
  return activeStreamClosers.size;
}

export function closeAllWorkspaceStreams(): void {
  const closers = Array.from(activeStreamClosers);
  activeStreamClosers.clear();
  for (const close of closers) {
    try {
      close();
    } catch {
      // ignore teardown errors
    }
  }
}

async function workspaceRequest<T>(
  path: string,
  options: {
    method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
    body?: unknown;
    headers?: Record<string, string>;
    signal?: AbortSignal;
    timeoutMs?: number;
    idempotencyKey?: string;
  } = {},
): Promise<T> {
  const session = getSession();
  if (!session) {
    throw new ApiError("登录已失效，请重新登录。", {
      status: 401,
      code: "UNAUTHORIZED",
      userMessage: "登录已失效，请重新登录。",
    });
  }

  return authorizedRequest<T>(path, {
    method: options.method,
    body: options.body,
    signal: options.signal,
    timeoutMs: options.timeoutMs ?? 8000,
    idempotencyKey: options.idempotencyKey,
    headers: {
      "X-Client-Device-Id": getClientDeviceId(),
      ...options.headers,
    },
  });
}

function normalizeMutationResult(
  res: ArtifactMutationResult | WorkspaceArtifactDto,
): ArtifactMutationResult {
  if ("artifact" in res && res.op && typeof res.op.id === "string" && res.op.id.trim()) {
    return {
      ...res,
      skipped_fields: res.skipped_fields ?? [],
      pending_suggestions_added: res.pending_suggestions_added ?? [],
    };
  }
  throw new ApiError("工作区操作缺少完整服务端回执，结果待核实。", {
    status: 502,
    code: "WORKSPACE_RECEIPT_INCOMPLETE",
    userMessage: "未收到操作回执，不能将本地内容视作已保存。请先核查服务端草稿。",
  });
}

export async function listArtifacts(params: {
  threadId?: string;
  q?: string;
} = {}): Promise<WorkspaceArtifactDto[]> {
  const sp = new URLSearchParams();
  if (params.threadId) sp.set("thread_id", params.threadId);
  const qs = sp.toString();
  const res = await workspaceRequest<
    WorkspaceArtifactDto[] | { items: WorkspaceArtifactDto[] }
  >(`/api/v1/workspace/artifacts${qs ? `?${qs}` : ""}`);
  const items = Array.isArray(res) ? res : (res?.items ?? []);
  if (!params.q) {
    return items;
  }
  const needle = params.q.trim().toLowerCase();
  if (!needle) {
    return items;
  }
  return items.filter(
    (item) =>
      item.title.toLowerCase().includes(needle) ||
      item.kind.toLowerCase().includes(needle) ||
      item.id.toLowerCase().includes(needle),
  );
}

export async function createArtifact(input: {
  thread_id: string;
  kind: ArtifactKind;
  title: string;
  payload?: Record<string, unknown>;
  source_refs?: Array<Record<string, unknown>>;
  actor_type?: "human" | "agent";
  idempotency_key?: string;
}): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    "/api/v1/workspace/artifacts",
    {
      method: "POST",
      idempotencyKey: input.idempotency_key,
      body: {
        thread_id: input.thread_id,
        kind: input.kind,
        title: input.title,
        payload: input.payload ?? {},
        source_refs: input.source_refs ?? [],
        actor_type: input.actor_type ?? "human",
        idempotency_key: input.idempotency_key ?? null,
      },
    },
  );
  queryClient.invalidateQueries(["workspace"]);
  return normalizeMutationResult(res);
}

export async function getArtifactDetail(artifactId: string): Promise<ArtifactDetailResult> {
  const res = await workspaceRequest<ArtifactDetailResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}`,
  );
  if ("artifact" in res) {
    return {
      artifact: res.artifact,
      ops: res.ops ?? [],
    };
  }
  return { artifact: res, ops: [] };
}

export async function getArtifact(artifactId: string): Promise<WorkspaceArtifactDto> {
  const detail = await getArtifactDetail(artifactId);
  return detail.artifact;
}

export async function patchArtifact(
  artifactId: string,
  input: {
    base_version: number;
    patch: Record<string, unknown>;
    actor_type?: "human" | "agent";
    reason?: string | null;
    title?: string | null;
    idempotency_key?: string;
  },
): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}`,
    {
      method: "PATCH",
      idempotencyKey: input.idempotency_key,
      body: {
        base_version: input.base_version,
        patch: input.patch,
        actor_type: input.actor_type ?? "human",
        reason: input.reason ?? null,
        title: input.title ?? null,
        idempotency_key: input.idempotency_key ?? null,
      },
    },
  );
  queryClient.invalidateQueries(["workspace", "artifact", artifactId]);
  return normalizeMutationResult(res);
}

export async function lockArtifactFields(
  artifactId: string,
  input: { base_version: number; fields: string[] },
): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}/lock`,
    {
      method: "POST",
      body: {
        base_version: input.base_version,
        fields: input.fields,
      },
    },
  );
  return normalizeMutationResult(res);
}

export async function unlockArtifactFields(
  artifactId: string,
  input: { base_version: number; fields: string[] },
): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}/unlock`,
    {
      method: "POST",
      body: {
        base_version: input.base_version,
        fields: input.fields,
      },
    },
  );
  return normalizeMutationResult(res);
}

export async function acceptArtifactSuggestion(
  artifactId: string,
  opId: string,
  input: { base_version: number; field?: string | null },
): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}/suggestions/${encodeURIComponent(opId)}/accept`,
    {
      method: "POST",
      body: {
        base_version: input.base_version,
        field: input.field ?? null,
      },
    },
  );
  return normalizeMutationResult(res);
}

export async function ignoreArtifactSuggestion(
  artifactId: string,
  opId: string,
  input: { base_version: number; field?: string | null },
): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}/suggestions/${encodeURIComponent(opId)}/ignore`,
    {
      method: "POST",
      body: {
        base_version: input.base_version,
        field: input.field ?? null,
      },
    },
  );
  return normalizeMutationResult(res);
}

export async function undoArtifact(
  artifactId: string,
  input: { base_version: number; target_op_id: string; idempotency_key?: string },
): Promise<ArtifactMutationResult> {
  const res = await workspaceRequest<ArtifactMutationResult | WorkspaceArtifactDto>(
    `/api/v1/workspace/artifacts/${encodeURIComponent(artifactId)}/undo`,
    {
      method: "POST",
      idempotencyKey: input.idempotency_key,
      body: {
        base_version: input.base_version,
        target_op_id: input.target_op_id,
        idempotency_key: input.idempotency_key ?? null,
      },
    },
  );
  queryClient.invalidateQueries(["workspace", "artifact", artifactId]);
  return normalizeMutationResult(res);
}

/**
 * Compatibility export is deliberately fail-closed until a real business adapter
 * and original-operation verification exist. It performs no HTTP request.
 */
export async function submitArtifact(
  artifactId: string,
  input: {
    base_version?: number;
    idempotency_key: string;
    note?: string;
    confirmed_by_human?: true;
  },
): Promise<SubmitOutcome> {
  void artifactId;
  void input;
  return {
    status: "failed",
    error: "WORKSPACE_SUBMIT_NOT_ENABLED",
    message: "正式业务提交尚未接入。工作区草稿不等于正式单据，本入口未发送请求。",
  };
}

export async function getThreadWorkspace(threadId: string): Promise<ThreadWorkspaceDto> {
  return workspaceRequest<ThreadWorkspaceDto>(
    `/api/v1/workspace/threads/${encodeURIComponent(threadId)}`,
  );
}

export async function syncThreadWorkspace(
  threadId: string,
  input: {
    open_objects: OpenObjectRefDto[];
    active_object_ref: OpenObjectRefDto | null;
    base_version?: number;
  },
): Promise<ThreadWorkspaceDto> {
  return workspaceRequest<ThreadWorkspaceDto>(
    `/api/v1/workspace/threads/${encodeURIComponent(threadId)}`,
    {
      method: "PUT",
      body: {
        base_version: input.base_version ?? null,
        open_objects: input.open_objects,
        active_object_ref: input.active_object_ref,
        device: getClientDeviceId(),
      },
    },
  );
}

function toContractBlock(block: UiBlockDto | Record<string, unknown>): Record<string, unknown> {
  const b = block as Record<string, unknown>;
  return {
    type: b.type,
    version: b.version,
    props: b.props && typeof b.props === "object" ? b.props : {},
    refs: Array.isArray(b.refs) ? b.refs : [],
  };
}

export async function resolveBlocks(input: {
  blocks?: Array<Record<string, unknown>>;
  block?: Record<string, unknown>;
  thread_id?: string | null;
}): Promise<{ block: Record<string, unknown>; blocks: Array<Record<string, unknown>> }> {
  const payload: Record<string, unknown> = {
    thread_id: input.thread_id ?? null,
  };
  if (input.block) {
    payload.block = toContractBlock(input.block);
  }
  if (input.blocks) {
    payload.blocks = input.blocks.map(toContractBlock);
  }
  return workspaceRequest<{ block: Record<string, unknown>; blocks: Array<Record<string, unknown>> }>(
    "/api/v1/workspace/blocks/resolve",
    {
      method: "POST",
      body: payload,
    },
  );
}

export async function pinBlockToArtifact(input: {
  thread_id: string;
  block: UiBlockDto | Record<string, unknown>;
  title?: string;
  kind?: ArtifactKind;
  idempotency_key?: string;
}): Promise<PinBlockResult> {
  const result = await workspaceRequest<
    {
      artifact: WorkspaceArtifactDto;
      resolved_block?: Record<string, unknown>;
      op?: ArtifactOpDto;
    } | WorkspaceArtifactDto
  >("/api/v1/workspace/blocks/pin", {
    method: "POST",
    idempotencyKey: input.idempotency_key,
    body: {
      thread_id: input.thread_id,
      block: toContractBlock(input.block),
      kind: input.kind ?? null,
      title: input.title ?? null,
      idempotency_key: input.idempotency_key ?? null,
    },
  });
  queryClient.invalidateQueries(["workspace"]);
  if ("artifact" in result) {
    return {
      artifact: result.artifact,
      resolved_block: result.resolved_block,
      op: result.op,
      object_ref: {
        kind: "artifact",
        id: result.artifact.id,
        title: result.artifact.title,
        artifact_id: result.artifact.id,
      },
    };
  }
  return {
    artifact: result,
    object_ref: {
      kind: "artifact",
      id: result.id,
      title: result.title,
      artifact_id: result.id,
    },
  };
}

export interface CatalogSearchItemDto {
  id: string;
  sku: string;
  name: string;
  spec: string | null;
  unit: string | null;
  source_system?: string;
  captured_at?: string;
}

export interface CommandSearchResult {
  products: CatalogSearchItemDto[];
  artifacts: WorkspaceArtifactDto[];
  degraded: boolean;
}

export async function searchCommandObjects(query: string): Promise<CommandSearchResult> {
  const isCurrent = captureSessionScope();
  const trimmed = query.trim();
  const qParam = trimmed ? `?q=${encodeURIComponent(trimmed)}&limit=20` : "?limit=20";
  const [productsRes, artifactsRes] = await Promise.allSettled([
    trimmed
      ? workspaceRequest<{
          exact?: Array<{
            product_id: string;
            external_code: string;
            name: string | null;
            spec?: string | null;
            unit?: string | null;
            source_system?: string;
            captured_at?: string;
          }>;
          candidates?: Array<{
            product_id: string;
            external_code: string;
            name: string | null;
            spec?: string | null;
            unit?: string | null;
            source_system?: string;
            captured_at?: string;
          }>;
        }>(`/api/v1/search${qParam}`, { timeoutMs: 4000 })
      : Promise.resolve({ exact: [], candidates: [] }),
    listArtifacts({ q: trimmed || undefined }),
  ]);

  if (!isCurrent()) throw new ApiError("身份已改变，旧搜索结果已丢弃。", { status: 0, code: "STALE_SCOPE", userMessage: "请在当前身份下重新搜索。" });
  const seenIds = new Set<string>();
  const products: CatalogSearchItemDto[] = [];
  if (productsRes.status === "fulfilled") {
    const val = productsRes.value as {
      exact?: Array<Record<string, unknown>>;
      candidates?: Array<Record<string, unknown>>;
      products?: Array<Record<string, unknown>>;
    };
    const rawMatches = [
      ...(val.exact ?? []),
      ...(val.candidates ?? []),
      ...(val.products ?? []),
    ];
    for (const item of rawMatches) {
      const pid = String(item.product_id ?? item.id ?? item.sku ?? "");
      if (!pid || seenIds.has(pid)) continue;
      seenIds.add(pid);
      const sku = String(item.external_code ?? item.sku ?? pid);
      products.push({
        id: pid,
        sku,
        name: typeof item.name === "string" && item.name ? item.name : sku,
        spec: typeof item.spec === "string" ? item.spec : null,
        unit: typeof item.unit === "string" ? item.unit : null,
        source_system:
          typeof item.source_system === "string" ? item.source_system : undefined,
        captured_at:
          typeof item.captured_at === "string" ? item.captured_at : undefined,
      });
    }
  }
  const artifacts = artifactsRes.status === "fulfilled" ? artifactsRes.value : [];
  const degraded = productsRes.status === "rejected" && artifactsRes.status === "rejected";

  return { products, artifacts, degraded };
}

/**
 * Subscribe to backend thread workspace SSE stream with cursor replay recovery.
 * Immediately terminates when `closeAllWorkspaceStreams()` is invoked (logout / user switch).
 */
export function subscribeWorkspaceEvents(options: WorkspaceStreamOptions): () => void {
  let closed = false;
  let cursor: string | null = options.initialCursor ?? null;
  let activeController: AbortController | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  const scopeAtSubscribe = getScopeVersion();
  const baseDelay = options.reconnectDelayMs ?? 1200;
  const threadId = options.threadId ?? "thread-today";
  let attempt = 0;
  // 短流正常结束（服务端发完增量就关闭）不等于连接故障，不该显示成重连中。
  let endedCleanly = false;

  const stop = () => {
    if (closed) {
      return;
    }
    closed = true;
    activeStreamClosers.delete(stop);
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (activeController) {
      activeController.abort();
      activeController = null;
    }
    if (scopeAtSubscribe === getScopeVersion()) options.onStateChange?.("closed");
  };

  activeStreamClosers.add(stop);

  const connect = async () => {
    if (closed || scopeAtSubscribe !== getScopeVersion()) {
      stop();
      return;
    }
    const session = getSession();
    if (!session) {
      stop();
      return;
    }

    if (!endedCleanly) {
      options.onStateChange?.(attempt === 0 ? "connecting" : "reconnecting");
    }
    const controller = new AbortController();
    activeController = controller;
    const release = registerScopeController(controller);

    const sp = new URLSearchParams();
    sp.set("cursor", cursor && /^\d+$/.test(cursor) ? cursor : "0");
    sp.set("stream", "true");
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
      Authorization: `Bearer ${session.accessToken}`,
      "X-Client-Device-Id": getClientDeviceId(),
    };
    if (cursor) {
      headers["Last-Event-ID"] = cursor;
    }

    try {
      const response = await fetch(
        `/api/v1/workspace/threads/${encodeURIComponent(threadId)}/events?${sp.toString()}`,
        {
          method: "GET",
          headers,
          signal: controller.signal,
        },
      );

      if (closed || scopeAtSubscribe !== getScopeVersion()) { stop(); return; }
      if ([401, 403, 404].includes(response.status)) { stop(); return; }
      if (!response.ok || !response.body) {
        throw new Error(`SSE status ${response.status}`);
      }

      attempt = 0;
      options.onStateChange?.("open");
      endedCleanly = false;

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let currentId: string | null = null;
      let currentEvent = "message";
      let dataLines: string[] = [];

      const flushFrame = () => {
        if (closed || scopeAtSubscribe !== getScopeVersion()) return;
        if (dataLines.length === 0) {
          currentId = null;
          currentEvent = "message";
          return;
        }
        const rawData = dataLines.join("\n");
        if (currentId !== null) {
          cursor = currentId;
        }
        let parsed: Record<string, unknown>;
        try {
          parsed = JSON.parse(rawData) as Record<string, unknown>;
        } catch {
          parsed = { raw: rawData };
        }
        if (typeof parsed.seq === "number" || typeof parsed.seq === "string") {
          cursor = String(parsed.seq);
        } else if (typeof parsed.cursor === "string" && parsed.cursor) {
          cursor = parsed.cursor;
        }
        options.onEvent({
          id: currentId,
          event: currentEvent,
          data: parsed,
        });
        currentId = null;
        currentEvent = "message";
        dataLines = [];
      };

      while (!closed) {
        const { value, done } = await reader.read();
        if (closed || scopeAtSubscribe !== getScopeVersion()) { stop(); return; }
        if (done) {
          flushFrame();
          endedCleanly = true;
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (line === "") {
            flushFrame();
          } else if (line.startsWith("id:")) {
            currentId = line.slice(3).trim();
          } else if (line.startsWith("event:")) {
            currentEvent = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).trim());
          }
        }
      }
    } catch {
      // Fall through to backoff reconnect unless closed or scope switched
    } finally {
      release();
      if (activeController === controller) {
        activeController = null;
      }
    }

    if (!closed && scopeAtSubscribe === getScopeVersion() && getSession()) {
      attempt += 1;
      if (!endedCleanly) {
        options.onStateChange?.("reconnecting");
      }
      const delay = Math.min(baseDelay * Math.min(attempt, 5), 10000);
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    } else {
      stop();
    }
  };

  void connect();
  return stop;
}
