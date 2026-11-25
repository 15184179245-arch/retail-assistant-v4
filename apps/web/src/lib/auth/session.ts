import { ApiError, apiRequest } from "../api/client";
import type { RequestOptions } from "../api/client";
import { clearQueryCache } from "../api/query";
import { closeAllWorkspaceStreams } from "../api/workspace";
import { bindWorkspaceSubject, resetWorkspaceStore } from "../../workspace/store";
import {
  ALL_KNOWN_CAPABILITIES,
  clearCapabilities,
  setCapabilities,
} from "./capabilities";

export interface Session {
  accessToken: string;
  tokenType: "Bearer";
  expiresAt: string;
  subjectId: string;
  organizationId: string;
  storeId: string;
  username: string;
  capabilities?: string[];
}

type Listener = () => void;

let current: Session | null = null;
let scopeVersion = 0;
const listeners = new Set<Listener>();
const inflight = new Set<AbortController>();

export function getSession(): Session | null {
  return current;
}

export function getScopeVersion(): number {
  return scopeVersion;
}

/** Capture the initiating identity for every async callback/queued mutation. */
export function captureSessionScope(): () => boolean {
  const version = scopeVersion;
  const session = current;
  return () => session !== null && current === session && scopeVersion === version;
}

function staleScopeError(): ApiError {
  return new ApiError("身份已改变，旧请求结果已丢弃。", {
    status: 0, code: "STALE_SCOPE", userMessage: "身份已改变，旧请求结果不会进入当前工作区。",
  });
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

function abortInflight(): void {
  for (const controller of inflight) {
    controller.abort();
  }
  inflight.clear();
}

export function setSession(next: Session): void {
  const switching = Boolean(current);
  if (switching) {
    // Switching identity must not let the previous request scope, cache, tabs, or SSE streams survive.
    abortInflight();
    clearCapabilities();
  }
  clearQueryCache();
  closeAllWorkspaceStreams();
  resetWorkspaceStore({ clearUrl: switching });
  current = next;
  scopeVersion += 1;
  bindWorkspaceSubject(next.subjectId);
  if (next.capabilities) {
    setCapabilities(next.capabilities);
  } else {
    setCapabilities(ALL_KNOWN_CAPABILITIES, "v0-bootstrap");
  }
  emit();
}

export function clearSession(): void {
  abortInflight();
  current = null;
  scopeVersion += 1;
  clearCapabilities();
  clearQueryCache();
  closeAllWorkspaceStreams();
  resetWorkspaceStore({ clearUrl: true });
  emit();
}

export function registerScopeController(controller: AbortController): () => void {
  inflight.add(controller);
  return () => {
    inflight.delete(controller);
  };
}

export interface ScopedRequest {
  signal: AbortSignal;
  version: number;
  finish: () => void;
  isCurrent: () => boolean;
}

export function beginScopedRequest(): ScopedRequest {
  const controller = new AbortController();
  inflight.add(controller);
  const version = scopeVersion;
  return {
    signal: controller.signal,
    version,
    finish: () => {
      inflight.delete(controller);
    },
    isCurrent: () => version === scopeVersion,
  };
}

export function sessionIsExpired(session: Session, now = Date.now()): boolean {
  const expires = Date.parse(session.expiresAt);
  return Number.isNaN(expires) || expires <= now;
}

export function authorizedRequest<T>(
  path: string,
  options: Omit<RequestOptions, "token" | "onUnauthorized"> = {},
): Promise<T> {
  const session = getSession();
  const scoped = beginScopedRequest();
  let invalidatedByThisRequest = false;
  return apiRequest<T>(path, {
    ...options,
    token: session?.accessToken ?? null,
    signal: scoped.signal,
    onUnauthorized: () => {
      // A late 401 from the previous identity must never clear the new one.
      if (scoped.isCurrent()) {
        invalidatedByThisRequest = true;
        clearSession();
      }
    },
  }).then(
    (value) => { if (!scoped.isCurrent()) throw staleScopeError(); return value; },
    (error: unknown) => { if (!scoped.isCurrent() && !invalidatedByThisRequest) throw staleScopeError(); throw error; },
  ).finally(scoped.finish);
}


/** Local logout must complete before any network wait, including a hung revoke. */
export async function logoutSession(baseUrl = ""): Promise<void> {
  const token = current?.accessToken;
  clearSession();
  if (!token) return;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    await apiRequest("/api/v1/auth/logout", {
      method: "POST", token, baseUrl, signal: controller.signal,
    });
  } catch {
    // Best-effort revocation never restores the old identity.
  } finally {
    clearTimeout(timeout);
  }
}
