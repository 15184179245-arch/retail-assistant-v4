import { useSyncExternalStore } from "react";
import { apiRequest } from "../api/client";
import { getSession } from "./session";

export const ALL_KNOWN_CAPABILITIES = [
  "catalog.read",
  "agent.run",
  "knowledge.read",
  "purchasing.read",
  "inventory.stocktake",
  "tasks.read",
  "office.materials.read",
  "office.documents.read",
  "office.exceptions.read",
  "analytics.read",
  "receiving.read",
];

export interface IdentityContextData {
  subject_id: string;
  organization_id: string;
  store_id: string;
  auth_expires_at: string;
  capabilities: string[];
  capability_version: string;
}

type Listener = () => void;

let currentCapabilities: string[] = [];
let currentVersion = "v0";
let isFetching = false;
const listeners = new Set<Listener>();

export function getCapabilities(): string[] {
  return currentCapabilities;
}

export function getCapabilityVersion(): string {
  return currentVersion;
}

export function hasCapability(capability: string): boolean {
  if (capability === "public" || capability === "authenticated") {
    return true;
  }
  if (capability === "agent.run") {
    return currentCapabilities.includes("agent.run") || currentCapabilities.includes("agents.run");
  }
  if (capability === "receiving.read") {
    return currentCapabilities.includes("receiving.read") || currentCapabilities.includes("erp.receiving.read");
  }
  return currentCapabilities.includes(capability);
}

export function subscribeCapabilities(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

export function setCapabilities(capabilities: string[], version = "v1"): void {
  currentCapabilities = [...capabilities];
  currentVersion = version;
  emit();
}

export function clearCapabilities(): void {
  currentCapabilities = [];
  currentVersion = "v0";
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

  if (isFetching) {
    return currentCapabilities;
  }

  isFetching = true;
  try {
    const data = await apiRequest<IdentityContextData>("/api/v1/identity/context", {
      token: session.accessToken,
    });
    currentCapabilities = data.capabilities || [];
    currentVersion = data.capability_version || "v1";
    emit();
    return currentCapabilities;
  } catch (error) {
    // If forbidden or unauthorized, wipe capabilities
    if (typeof error === "object" && error !== null && "status" in error) {
      const status = (error as { status: number }).status;
      if (status === 401 || status === 403) {
        clearCapabilities();
      }
    }
    return currentCapabilities;
  } finally {
    isFetching = false;
  }
}

export function useCapabilities(): {
  capabilities: string[];
  version: string;
  hasCapability: (cap: string) => boolean;
} {
  const caps = useSyncExternalStore(subscribeCapabilities, getCapabilities, getCapabilities);
  const version = useSyncExternalStore(subscribeCapabilities, getCapabilityVersion, getCapabilityVersion);

  return {
    capabilities: caps,
    version,
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
