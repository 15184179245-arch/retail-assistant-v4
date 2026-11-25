import { clearDraftCheckpoint, reconcileDraftCheckpointVersion } from './assistantDraftCheckpoint';
import { useSyncExternalStore } from "react";
import { authorizedRequest, captureSessionScope, getScopeVersion, getSession } from "./session";

export interface IdentityContextData {
  subject_id: string;
  organization_id: string;
  store_id: string;
  auth_expires_at: string;
  capabilities: string[];
  capability_version: string;
}

type Listener = () => void;

export type CapabilityStatus = "unknown" | "loading" | "ready" | "unavailable";
const EMPTY_CAPABILITIES: string[] = [];
let context: { status: Exclude<CapabilityStatus, "loading">; capabilities: string[]; version: string } =
  { status: "unknown", capabilities: EMPTY_CAPABILITIES, version: "v0" };
let inflight: { scope: number; promise: Promise<string[]> } | null = null;
const listeners = new Set<Listener>();
let authorityEpoch = 0;

/** Count authority notifications, not reusable server versions or capability tuples. */
export function getCapabilityEpoch(): number {
  return authorityEpoch;
}

export function getCapabilities(): string[] {
  return getCapabilityStatus() === "ready" ? context.capabilities : EMPTY_CAPABILITIES;
}

export function getCapabilityVersion(): string {
  return context.version;
}

export function getCapabilityStatus(): CapabilityStatus {
  return inflight?.scope === getScopeVersion() ? "loading" : context.status;
}

export function hasCapability(capability: string): boolean {
  if (capability === "public" || capability === "authenticated") {
    return true;
  }
  if (capability === "agent.run") {
    return getCapabilities().includes("agent.run") || getCapabilities().includes("agents.run");
  }
  if (capability === "receiving.read") {
    return getCapabilities().includes("receiving.read") || getCapabilities().includes("erp.receiving.read");
  }
  return getCapabilities().includes(capability);
}

export function subscribeCapabilities(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  authorityEpoch += 1;
  for (const listener of listeners) {
    listener();
  }
}

export function setCapabilities(capabilities: string[], version = "v1"): void {
  context = { status: "ready", capabilities: [...capabilities], version };
  emit();
}

export function clearCapabilities(): void {
  context = { status: "unknown", capabilities: EMPTY_CAPABILITIES, version: "v0" };
  emit();
}

/**
 * Fetches the effective capabilities from the backend identity context endpoint.
 * Requirements: FE-003, R-034.
 */
export async function refreshCapabilities(): Promise<string[]> {
  const session = getSession();
  if (!session || !session.accessToken) {
    clearCapabilities();
    return [];
  }

  const scope = getScopeVersion();
  if (inflight?.scope === scope) return inflight.promise;
  const isCurrent = captureSessionScope();
  const promise = (async () => {
    try {
      const data = await authorizedRequest<IdentityContextData>("/api/v1/identity/context");
      if (isCurrent() && data.subject_id === session.subjectId &&
          data.organization_id === session.organizationId && data.store_id === session.storeId &&
          Array.isArray(data.capabilities) && data.capabilities.every(cap => typeof cap === "string")) {
        const version=data.capability_version || "v1";
        if ((data.capabilities.includes('agent.run') || data.capabilities.includes('agents.run')) &&
            data.capabilities.includes('tasks.read_self') && data.capabilities.includes('tasks.manage')) {
          reconcileDraftCheckpointVersion(session,version);
        } else {
          clearDraftCheckpoint(session);
        }
        setCapabilities(data.capabilities,version);
      } else if (isCurrent()) {
        context = { status: "unavailable", capabilities: EMPTY_CAPABILITIES, version: "v0" };
      }
    } catch {
      if (isCurrent()) context = { status: "unavailable", capabilities: EMPTY_CAPABILITIES, version: "v0" };
    } finally {
      if (inflight?.scope === scope) { inflight = null; emit(); }
    }
    return getCapabilities();
  })();
  inflight = { scope, promise };
  emit();
  return promise;
}

export function useCapabilities(): {
  capabilities: string[];
  version: string;
  status: CapabilityStatus;
  hasCapability: (cap: string) => boolean;
} {
  const caps = useSyncExternalStore(subscribeCapabilities, getCapabilities, getCapabilities);
  const status = useSyncExternalStore(subscribeCapabilities, getCapabilityStatus, getCapabilityStatus);
  const version = useSyncExternalStore(subscribeCapabilities, getCapabilityVersion, getCapabilityVersion);

  return {
    capabilities: caps,
    version,
    status,
    hasCapability: (cap: string) => {
      if (cap === "public" || cap === "authenticated") return true;
      if (cap === "agent.run") {
        return caps.includes("agent.run") || caps.includes("agents.run");
      }
      if (cap === "receiving.read") {
        return caps.includes("receiving.read") || caps.includes("erp.receiving.read");
      }
      return caps.includes(cap);
    },
  };
}
