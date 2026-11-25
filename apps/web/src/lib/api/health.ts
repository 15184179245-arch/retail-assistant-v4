import { useEffect, useState } from "react";

export type ConnectionStatus =
  | "online"
  | "backend_unreachable"
  | "model_offline"
  | "offline";

export interface SystemHealth {
  backendReachable: boolean;
  modelAvailable: boolean;
  lastCheckedTimestamp: number;
  databaseOk?: boolean;
  outboxDegraded?: boolean;
  errorMessage?: string;
}

type HealthListener = (status: ConnectionStatus, health: SystemHealth) => void;

let currentHealth: SystemHealth = {
  backendReachable: true,
  modelAvailable: true,
  lastCheckedTimestamp: Date.now(),
  databaseOk: true,
};

let currentStatus: ConnectionStatus = "online";
const listeners = new Set<HealthListener>();

/**
 * Check whether human manual operations are permitted.
 * In accordance with business boundary 8:
 * Offline status and AI model unavailability NEVER block barcode scanning,
 * inventory search, or manual sales draft creation.
 */
export function isManualPermitted(_status: ConnectionStatus): boolean {
  return true;
}

/**
 * Check whether AI agent operations are permitted.
 * Requires full online connectivity and active model availability.
 */
export function isAgentPermitted(status: ConnectionStatus): boolean {
  return status === "online";
}

export function getHealthState(): {
  status: ConnectionStatus;
  health: SystemHealth;
} {
  return {
    status: currentStatus,
    health: currentHealth,
  };
}

export function subscribeHealth(listener: HealthListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function notify(): void {
  for (const listener of listeners) {
    listener(currentStatus, currentHealth);
  }
}

export function setHealthState(
  updates: Partial<SystemHealth> & { status?: ConnectionStatus }
): void {
  const { status, ...healthUpdates } = updates;
  currentHealth = {
    ...currentHealth,
    ...healthUpdates,
    lastCheckedTimestamp: Date.now(),
  };

  if (status) {
    currentStatus = status;
  } else if (!currentHealth.backendReachable) {
    currentStatus = "backend_unreachable";
  } else if (!currentHealth.modelAvailable) {
    currentStatus = "model_offline";
  } else {
    currentStatus = "online";
  }

  notify();
}

/**
 * Perform a real health evaluation against the backend health endpoints.
 */
export async function checkHealth(endpoint = "/health/ready"): Promise<SystemHealth> {
  if (typeof navigator !== "undefined" && !navigator.onLine) {
    setHealthState({
      backendReachable: false,
      modelAvailable: false,
      status: "offline",
      errorMessage: "网络连接已断开",
    });
    return currentHealth;
  }

  try {
    const res = await fetch(endpoint, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(5000),
    });

    if (!res.ok) {
      setHealthState({
        backendReachable: false,
        modelAvailable: false,
        status: "backend_unreachable",
        errorMessage: `服务端状态异常 (HTTP ${res.status})`,
      });
      return currentHealth;
    }

    const payload = (await res.json()) as {
      success?: boolean;
      data?: {
        status?: string;
        model?: string;
        database?: string;
      };
    };

    const isDbOk = payload.data?.database === "ok" || payload.data?.status === "ready" || payload.data?.status === "ok";
    const isModelOffline = payload.data?.model === "offline" || payload.data?.model === "unavailable";

    setHealthState({
      backendReachable: true,
      databaseOk: isDbOk,
      modelAvailable: !isModelOffline,
      status: isModelOffline ? "model_offline" : "online",
      errorMessage: undefined,
    });
    return currentHealth;
  } catch (err) {
    const isOffline = typeof navigator !== "undefined" && !navigator.onLine;
    setHealthState({
      backendReachable: false,
      modelAvailable: false,
      status: isOffline ? "offline" : "backend_unreachable",
      errorMessage: err instanceof Error ? err.message : "连接失败",
    });
    return currentHealth;
  }
}

/**
 * Start background health observation with polling and window network event listeners.
 */
export function startHealthPolling(intervalMs = 30000): () => void {
  const handleOnline = () => {
    void checkHealth();
  };
  const handleOffline = () => {
    setHealthState({
      backendReachable: false,
      modelAvailable: false,
      status: "offline",
    });
  };

  if (typeof window !== "undefined") {
    window.addEventListener("online", handleOnline);
    window.addEventListener("offline", handleOffline);
  }

  const intervalId = setInterval(() => {
    void checkHealth();
  }, intervalMs);

  return () => {
    clearInterval(intervalId);
    if (typeof window !== "undefined") {
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("offline", handleOffline);
    }
  };
}

/**
 * React hook for consuming reactive connection and health status.
 */
export function useConnectionStatus() {
  const [state, setState] = useState(() => getHealthState());

  useEffect(() => {
    return subscribeHealth((status, health) => {
      setState({ status, health });
    });
  }, []);

  return {
    status: state.status,
    health: state.health,
    isManualPermitted: isManualPermitted(state.status),
    isAgentPermitted: isAgentPermitted(state.status),
    checkHealth,
  };
}
