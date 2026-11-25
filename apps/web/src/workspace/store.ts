import { useSyncExternalStore } from "react";
import { captureSessionScope } from "../lib/auth/session";
import {
  getThreadWorkspace,
  subscribeWorkspaceEvents,
  syncThreadWorkspace,
  type StreamConnectionState,
  type ThreadMessageDto,
  type WorkspaceEventEnvelope,
} from "../lib/api/workspace";
import type {
  ArtifactOpDto,
  OpenObjectRefDto,
  UiBlockDto,
  WorkspaceArtifactDto,
} from "../lib/api/generated";

export interface WorkbenchTab {
  id: string;
  kind: "today" | "route" | "artifact" | "object";
  title: string;
  path?: string;
  objectKind?: string;
  objectId?: string;
  artifactId?: string;
  closable: boolean;
  dirty?: boolean;
}

export interface AiFieldModification {
  field: string;
  opId: string;
  previousValue: unknown;
  newValue: unknown;
  label?: string;
  timestamp: string;
}

export interface WorkspaceToast {
  id: string;
  message: string;
  kind: "info" | "warning" | "success";
}

export interface WorkspaceThread {
  id: string;
  title: string;
  messages: ThreadMessageDto[];
  openObjects: OpenObjectRefDto[];
  activeObjectRef: OpenObjectRefDto | null;
  attachedObjects: OpenObjectRefDto[];
  version: number;
  modelStatus: "online" | "offline" | "degraded";
  degradedHint: string | null;
  streamState: StreamConnectionState;
  lastCursor: string | null;
}

export interface DirtyLeavePrompt {
  tabId: string;
  targetDescription: string;
  resolveAction: () => void;
}

export interface WorkspaceState {
  subjectId: string | null;
  leftSection: "today" | "objects" | "threads" | "settings";
  threadCollapsed: boolean;
  mobilePane: "thread" | "workbench";
  openTabs: WorkbenchTab[];
  activeTabId: string;
  threads: WorkspaceThread[];
  activeThreadId: string;
  artifactsById: Record<string, WorkspaceArtifactDto>;
  opsByArtifactId: Record<string, ArtifactOpDto[]>;
  partialUndoByArtifactId: Record<string, { skippedFields: string[]; message: string } | null>;
  aiModifiedFields: Record<string, Record<string, AiFieldModification>>;
  dirtyByTabId: Record<string, boolean>;
  dirtyLeavePrompt: DirtyLeavePrompt | null;
  toasts: WorkspaceToast[];
  syncWarning: string | null;
}

export const TODAY_TAB: WorkbenchTab = {
  id: "today",
  kind: "today",
  title: "今日",
  path: "/",
  closable: false,
};

function createDefaultThread(id = "thread-today", title = "今日工作区会话"): WorkspaceThread {
  return {
    id,
    title,
    messages: [],
    openObjects: [],
    activeObjectRef: null,
    attachedObjects: [],
    version: 1,
    modelStatus: "online",
    degradedHint: null,
    streamState: "closed",
    lastCursor: null,
  };
}

function createInitialState(): WorkspaceState {
  return {
    subjectId: null,
    leftSection: "today",
    threadCollapsed: false,
    mobilePane: "workbench",
    openTabs: [{ ...TODAY_TAB }],
    activeTabId: TODAY_TAB.id,
    threads: [createDefaultThread()],
    activeThreadId: "thread-today",
    artifactsById: {},
    opsByArtifactId: {},
    partialUndoByArtifactId: {},
    aiModifiedFields: {},
    dirtyByTabId: {},
    dirtyLeavePrompt: null,
    toasts: [],
    syncWarning: null,
  };
}

let state: WorkspaceState = createInitialState();
const listeners = new Set<() => void>();
let activeStreamUnsub: (() => void) | null = null;
let urlSyncEnabled = true;

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

export function subscribeWorkspace(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getWorkspaceState(): WorkspaceState {
  return state;
}

export function useWorkspaceState(): WorkspaceState {
  return useSyncExternalStore(
    subscribeWorkspace,
    getWorkspaceState,
    getWorkspaceState,
  );
}

export function setWorkspaceUrlSyncEnabled(enabled: boolean): void {
  urlSyncEnabled = enabled;
}

function syncTabsToUrl(): void {
  if (!urlSyncEnabled || typeof window === "undefined") {
    return;
  }
  try {
    const url = new URL(window.location.href);
    const nonTodayIds = state.openTabs
      .filter((t) => t.id !== TODAY_TAB.id)
      .map((t) => t.id);

    if (state.activeTabId === TODAY_TAB.id && nonTodayIds.length === 0) {
      url.searchParams.delete("tab");
      url.searchParams.delete("tabs");
    } else {
      url.searchParams.set("tab", state.activeTabId);
      if (nonTodayIds.length > 0) {
        url.searchParams.set("tabs", nonTodayIds.join(","));
      } else {
        url.searchParams.delete("tabs");
      }
    }

    const nextUrl = `${url.pathname}${url.search}${url.hash}`;
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (nextUrl !== currentUrl) {
      window.history.replaceState(window.history.state, "", nextUrl);
    }
  } catch {
    // ignore invalid URLs in synthetic/non-browser environments
  }
}

export function resetWorkspaceStore(options: { clearUrl?: boolean } = {}): void {
  if (activeStreamUnsub) {
    activeStreamUnsub();
    activeStreamUnsub = null;
  }
  state = createInitialState();
  if (options.clearUrl) {
    syncTabsToUrl();
  }
  emit();
}

export function bindWorkspaceSubject(subjectId: string | null): void {
  if (state.subjectId !== null && subjectId !== state.subjectId) {
    resetWorkspaceStore();
  }
  state = { ...state, subjectId };
  if (subjectId) {
    hydrateWorkspaceFromUrl();
  }
  emit();
}

export function canLeaveTab(tabId: string = state.activeTabId): boolean {
  return !state.dirtyByTabId[tabId];
}

export function hasAnyDirtyTab(): boolean {
  return Object.values(state.dirtyByTabId).some(Boolean);
}

export function setTabDirty(tabId: string, dirty: boolean): void {
  const prevDirty = Boolean(state.dirtyByTabId[tabId]);
  if (prevDirty === dirty) {
    return;
  }
  const nextDirty = { ...state.dirtyByTabId };
  if (dirty) {
    nextDirty[tabId] = true;
  } else {
    delete nextDirty[tabId];
  }
  state = {
    ...state,
    dirtyByTabId: nextDirty,
    openTabs: state.openTabs.map((t) => (t.id === tabId ? { ...t, dirty } : t)),
  };
  emit();
}

export function dismissDirtyLeavePrompt(): void {
  if (!state.dirtyLeavePrompt) {
    return;
  }
  state = { ...state, dirtyLeavePrompt: null };
  emit();
}

export function confirmDirtyLeavePrompt(): void {
  const prompt = state.dirtyLeavePrompt;
  if (!prompt) {
    return;
  }
  const nextDirty = { ...state.dirtyByTabId };
  delete nextDirty[prompt.tabId];
  state = {
    ...state,
    dirtyByTabId: nextDirty,
    dirtyLeavePrompt: null,
    openTabs: state.openTabs.map((t) =>
      t.id === prompt.tabId ? { ...t, dirty: false } : t,
    ),
  };
  prompt.resolveAction();
  emit();
}

export function setLeftSection(section: WorkspaceState["leftSection"]): void {
  state = { ...state, leftSection: section };
  emit();
}

export function toggleThreadSidebar(collapsed?: boolean): void {
  state = {
    ...state,
    threadCollapsed: collapsed ?? !state.threadCollapsed,
  };
  emit();
}

export function setMobilePane(pane: "thread" | "workbench"): void {
  state = { ...state, mobilePane: pane };
  emit();
}

export function pushToast(
  message: string,
  kind: WorkspaceToast["kind"] = "info",
): string {
  const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  state = {
    ...state,
    toasts: [...state.toasts.slice(-3), { id, message, kind }],
  };
  emit();
  return id;
}

export function dismissToast(id: string): void {
  state = {
    ...state,
    toasts: state.toasts.filter((t) => t.id !== id),
  };
  emit();
}

export function getActiveThread(): WorkspaceThread {
  return (
    state.threads.find((t) => t.id === state.activeThreadId) ??
    state.threads[0] ??
    createDefaultThread()
  );
}

function updateActiveThread(updater: (thread: WorkspaceThread) => WorkspaceThread): void {
  const current = getActiveThread();
  const updated = updater(current);
  const exists = state.threads.some((t) => t.id === current.id);
  const nextThreads = exists
    ? state.threads.map((t) => (t.id === current.id ? updated : t))
    : [...state.threads, updated];
  state = { ...state, threads: nextThreads };
}

function toObjectRef(tab: WorkbenchTab): OpenObjectRefDto | null {
  if (tab.kind === "today") {
    return null;
  }
  return {
    kind: tab.objectKind ?? tab.kind,
    id: tab.objectId ?? tab.id,
    title: tab.title,
    path: tab.path,
    artifact_id: tab.artifactId ?? null,
  };
}

let remoteRequestGeneration = 0;
let streamGeneration = 0;

function captureThreadRequest(threadId: string): () => boolean {
  const isCurrent = captureSessionScope();
  const requestGeneration = ++remoteRequestGeneration;
  return () => isCurrent() && state.activeThreadId === threadId && requestGeneration === remoteRequestGeneration;
}

export function syncThreadToBackend(): void {
  if (!state.subjectId) {
    return;
  }
  const thread = getActiveThread();
  const openObjects = state.openTabs
    .map(toObjectRef)
    .filter((item): item is OpenObjectRefDto => item !== null);
  const activeTab = state.openTabs.find((t) => t.id === state.activeTabId);
  const activeObjectRef = activeTab ? toObjectRef(activeTab) : null;
  const isCurrent = captureThreadRequest(thread.id);

  void syncThreadWorkspace(thread.id, {
    open_objects: openObjects,
    active_object_ref: activeObjectRef,
  })
    .then((dto) => {
      if (!isCurrent()) return;
      updateActiveThread((t) => ({
        ...t,
        version: dto.version,
        openObjects: dto.open_objects,
        activeObjectRef: dto.active_object_ref,
      }));
      if (dto.recent_editor_hint || (dto.recent_other_editor && dto.recent_other_editor.device)) {
        pushToast(dto.recent_editor_hint || "此对象正在另一端编辑", "info");
      }
      emit();
    })
    .catch(() => {
      // keep local multi-tab workspace usable if offline / backend unavailable
    });
}

export function openWorkbenchTab(
  tab: WorkbenchTab,
  options: { activate?: boolean; force?: boolean; syncBackend?: boolean } = {},
): boolean {
  const activate = options.activate ?? true;
  const existing = state.openTabs.find((t) => t.id === tab.id);

  if (
    activate &&
    state.activeTabId !== tab.id &&
    !options.force &&
    !canLeaveTab(state.activeTabId)
  ) {
    state = {
      ...state,
      dirtyLeavePrompt: {
        tabId: state.activeTabId,
        targetDescription: `切换到「${tab.title}」`,
        resolveAction: () => {
          openWorkbenchTab(tab, { ...options, force: true });
        },
      },
    };
    emit();
    return false;
  }

  const nextTabs = existing
    ? state.openTabs.map((t) => (t.id === tab.id ? { ...t, ...tab } : t))
    : [...state.openTabs, tab];

  const activeTabId = activate ? tab.id : state.activeTabId;
  state = {
    ...state,
    openTabs: nextTabs,
    activeTabId,
  };

  const activeTab = nextTabs.find((t) => t.id === activeTabId);
  updateActiveThread((t) => ({
    ...t,
    openObjects: nextTabs
      .map(toObjectRef)
      .filter((item): item is OpenObjectRefDto => item !== null),
    activeObjectRef: activeTab ? toObjectRef(activeTab) : null,
  }));

  syncTabsToUrl();
  emit();
  if (options.syncBackend !== false) {
    syncThreadToBackend();
  }
  return true;
}

export function selectWorkbenchTab(
  tabId: string,
  options: { force?: boolean } = {},
): boolean {
  const target = state.openTabs.find((t) => t.id === tabId);
  if (!target) {
    return false;
  }
  if (tabId === state.activeTabId) {
    return true;
  }
  if (!options.force && !canLeaveTab(state.activeTabId)) {
    state = {
      ...state,
      dirtyLeavePrompt: {
        tabId: state.activeTabId,
        targetDescription: `切换到「${target.title}」`,
        resolveAction: () => {
          selectWorkbenchTab(tabId, { force: true });
        },
      },
    };
    emit();
    return false;
  }
  state = { ...state, activeTabId: tabId };
  updateActiveThread((t) => ({
    ...t,
    activeObjectRef: toObjectRef(target),
  }));
  syncTabsToUrl();
  emit();
  syncThreadToBackend();
  return true;
}

export function closeWorkbenchTab(
  tabId: string,
  options: { force?: boolean } = {},
): boolean {
  const target = state.openTabs.find((t) => t.id === tabId);
  if (!target || !target.closable) {
    return false;
  }
  if (!options.force && !canLeaveTab(tabId)) {
    state = {
      ...state,
      dirtyLeavePrompt: {
        tabId,
        targetDescription: `关闭「${target.title}」`,
        resolveAction: () => {
          closeWorkbenchTab(tabId, { force: true });
        },
      },
    };
    emit();
    return false;
  }

  const nextDirty = { ...state.dirtyByTabId };
  delete nextDirty[tabId];

  const remaining = state.openTabs.filter((t) => t.id !== tabId);
  const nextActiveId =
    state.activeTabId === tabId
      ? (remaining[remaining.length - 1]?.id ?? TODAY_TAB.id)
      : state.activeTabId;

  state = {
    ...state,
    openTabs: remaining,
    activeTabId: nextActiveId,
    dirtyByTabId: nextDirty,
  };

  const activeTab = remaining.find((t) => t.id === nextActiveId);
  updateActiveThread((t) => ({
    ...t,
    openObjects: remaining
      .map(toObjectRef)
      .filter((item): item is OpenObjectRefDto => item !== null),
    activeObjectRef: activeTab ? toObjectRef(activeTab) : null,
  }));

  syncTabsToUrl();
  emit();
  syncThreadToBackend();
  return true;
}

export function upsertArtifact(
  artifact: WorkspaceArtifactDto,
  options: { openTab?: boolean; aiFieldDiffs?: AiFieldModification[] } = {},
): void {
  const nextArtifacts = {
    ...state.artifactsById,
    [artifact.id]: artifact,
  };

  let nextAiFields = state.aiModifiedFields;
  if (options.aiFieldDiffs && options.aiFieldDiffs.length > 0) {
    const existingForArt = { ...(nextAiFields[artifact.id] ?? {}) };
    for (const diff of options.aiFieldDiffs) {
      existingForArt[diff.field] = diff;
    }
    nextAiFields = {
      ...nextAiFields,
      [artifact.id]: existingForArt,
    };
  }

  state = {
    ...state,
    artifactsById: nextArtifacts,
    aiModifiedFields: nextAiFields,
  };

  if (options.openTab) {
    openWorkbenchTab({
      id: `artifact:${artifact.id}`,
      kind: "artifact",
      title: artifact.title,
      objectKind: artifact.kind,
      objectId: artifact.id,
      artifactId: artifact.id,
      closable: true,
    });
  } else {
    emit();
  }
}

export function clearAiModifiedField(artifactId: string, field: string): void {
  const currentForArt = state.aiModifiedFields[artifactId];
  if (!currentForArt || !(field in currentForArt)) {
    return;
  }
  const copy = { ...currentForArt };
  delete copy[field];
  state = {
    ...state,
    aiModifiedFields: {
      ...state.aiModifiedFields,
      [artifactId]: copy,
    },
  };
  emit();
}

export function setArtifactOps(artifactId: string, ops: ArtifactOpDto[]): void {
  state = {
    ...state,
    opsByArtifactId: {
      ...state.opsByArtifactId,
      [artifactId]: ops,
    },
  };
  emit();
}

export function appendArtifactOp(artifactId: string, op: ArtifactOpDto): void {
  const existing = state.opsByArtifactId[artifactId] ?? [];
  if (existing.some((item) => item.id === op.id)) {
    return;
  }
  state = {
    ...state,
    opsByArtifactId: {
      ...state.opsByArtifactId,
      [artifactId]: [op, ...existing],
    },
  };
  emit();
}

export function setPartialUndoNotice(
  artifactId: string,
  notice: { skippedFields: string[]; message: string } | null,
): void {
  state = {
    ...state,
    partialUndoByArtifactId: {
      ...state.partialUndoByArtifactId,
      [artifactId]: notice,
    },
  };
  emit();
}

export function attachObjectToComposer(obj: OpenObjectRefDto): void {
  updateActiveThread((t) => {
    const exists = t.attachedObjects.some(
      (o) => o.kind === obj.kind && o.id === obj.id,
    );
    if (exists) {
      return t;
    }
    return {
      ...t,
      attachedObjects: [...t.attachedObjects, obj],
    };
  });
  emit();
}

export function detachObjectFromComposer(kind: string, id: string): void {
  updateActiveThread((t) => ({
    ...t,
    attachedObjects: t.attachedObjects.filter(
      (o) => !(o.kind === kind && o.id === id),
    ),
  }));
  emit();
}

export function appendThreadMessage(
  msg: Omit<ThreadMessageDto, "id" | "created_at"> & {
    id?: string;
    created_at?: string;
  },
): ThreadMessageDto {
  const full: ThreadMessageDto = {
    id: msg.id ?? `msg-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    thread_id: msg.thread_id,
    role: msg.role,
    text: msg.text,
    blocks: msg.blocks,
    attached_objects: msg.attached_objects,
    created_at: msg.created_at ?? new Date().toISOString(),
    degraded_reason: msg.degraded_reason ?? null,
  };
  updateActiveThread((t) => ({
    ...t,
    messages: [...t.messages, full],
  }));
  emit();
  return full;
}

export function setModelStatus(
  status: WorkspaceThread["modelStatus"],
  degradedHint: string | null = null,
): void {
  updateActiveThread((t) => ({
    ...t,
    modelStatus: status,
    degradedHint,
  }));
  emit();
}

export function selectOrCreateThread(threadId: string, title?: string): void {
  remoteRequestGeneration += 1;
  streamGeneration += 1;
  const exists = state.threads.find((t) => t.id === threadId);
  if (!exists) {
    state = {
      ...state,
      threads: [...state.threads, createDefaultThread(threadId, title ?? threadId)],
      activeThreadId: threadId,
    };
  } else {
    state = { ...state, activeThreadId: threadId };
  }
  emit();
}

export function hydrateWorkspaceFromUrl(searchString?: string): void {
  const raw =
    searchString ?? (typeof window !== "undefined" ? window.location.search : "");
  if (!raw) {
    return;
  }
  const params = new URLSearchParams(raw.startsWith("?") ? raw : `?${raw}`);
  const tabsParam = params.get("tabs");
  const activeParam = params.get("tab");

  if (!tabsParam && !activeParam) {
    return;
  }

  const nextTabs: WorkbenchTab[] = [{ ...TODAY_TAB }];
  const addId = (id: string) => {
    if (!id || id === TODAY_TAB.id || nextTabs.some((t) => t.id === id)) {
      return;
    }
    if (id.startsWith("route:")) {
      const path = id.slice("route:".length);
      nextTabs.push({
        id,
        kind: "route",
        title: path,
        path,
        objectKind: "route",
        objectId: path,
        closable: true,
      });
    } else if (id.startsWith("artifact:")) {
      const artifactId = id.slice("artifact:".length);
      const art = state.artifactsById[artifactId];
      nextTabs.push({
        id,
        kind: "artifact",
        title: art?.title ?? `草稿 ${artifactId.slice(0, 6)}`,
        objectKind: art?.kind ?? "quote_draft",
        objectId: artifactId,
        artifactId,
        closable: true,
      });
    }
  };

  if (tabsParam) {
    for (const part of tabsParam.split(",")) {
      addId(part.trim());
    }
  }
  if (activeParam) {
    addId(activeParam.trim());
  }

  const resolvedActive =
    activeParam && nextTabs.some((t) => t.id === activeParam)
      ? activeParam
      : TODAY_TAB.id;

  state = {
    ...state,
    openTabs: nextTabs,
    activeTabId: resolvedActive,
  };
  syncTabsToUrl();
  emit();
}

export function handleWorkspaceSseEvent(evt: WorkspaceEventEnvelope): void {
  if (evt.id) {
    updateActiveThread((t) => ({ ...t, lastCursor: evt.id }));
  }

  const data = evt.data;
  if (
    evt.event === "workspace.remote_editing" ||
    data.other_editor_active === true
  ) {
    pushToast("此对象正在另一端编辑", "info");
  }

  if (
    (evt.event === "artifact.patched" ||
      evt.event === "artifact.updated" ||
      evt.event === "artifact.submitted") &&
    data.artifact &&
    typeof data.artifact === "object"
  ) {
    const artifact = data.artifact as WorkspaceArtifactDto;
    const diffs = Array.isArray(data.ai_field_diffs)
      ? (data.ai_field_diffs as AiFieldModification[])
      : undefined;
    upsertArtifact(artifact, { aiFieldDiffs: diffs });
  }

  if (evt.event === "thread.block" && data.block && typeof data.block === "object") {
    appendThreadMessage({
      thread_id:
        typeof data.thread_id === "string" ? data.thread_id : state.activeThreadId,
      role: "assistant",
      text: typeof data.text === "string" ? data.text : "",
      blocks: [data.block as UiBlockDto],
    });
  }

  emit();
}

export function startWorkspaceEventStream(): () => void {
  if (activeStreamUnsub) {
    activeStreamUnsub();
    activeStreamUnsub = null;
  }
  const thread = getActiveThread();
  const isCurrent = captureThreadRequest(thread.id);
  const scopeCurrent = captureSessionScope();
  const generation = ++streamGeneration;
  let stopped = false;
  const streamCurrent = () => !stopped && scopeCurrent() && state.activeThreadId === thread.id && generation === streamGeneration;
  void getThreadWorkspace(thread.id)
    .then((dto) => {
      if (!isCurrent()) return;
      updateActiveThread((t) => ({
        ...t,
        version: dto.version,
        openObjects: dto.open_objects,
        activeObjectRef: dto.active_object_ref,
      }));
      if (dto.recent_editor_hint || (dto.recent_other_editor && dto.recent_other_editor.device)) {
        pushToast(dto.recent_editor_hint || "此对象正在另一端编辑", "info");
      }
      emit();
    })
    .catch(() => {
      // backend workspace API might not be running in isolated frontend mode
    });

  const stopThisStream = subscribeWorkspaceEvents({
    threadId: thread.id,
    initialCursor: thread.lastCursor,
    onEvent: (evt) => { if (streamCurrent()) handleWorkspaceSseEvent(evt); },
    onStateChange: (streamState) => {
      if (!streamCurrent()) return;
      updateActiveThread((t) => ({ ...t, streamState }));
      emit();
    },
  });
  const stopOwned = () => {
    stopped = true;
    stopThisStream();
    if (activeStreamUnsub === stopOwned) activeStreamUnsub = null;
  };
  activeStreamUnsub = stopOwned;
  return stopOwned;
}
