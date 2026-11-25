import { useSyncExternalStore } from "react";

export type ShellMode = "ai" | "legacy";

let overrideMode: ShellMode | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) {
    listener();
  }
}

export function subscribeShellMode(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getShellMode(searchString?: string): ShellMode {
  if (overrideMode !== null) {
    return overrideMode;
  }
  const raw =
    searchString ?? (typeof window !== "undefined" ? window.location.search : "");
  if (raw) {
    const params = new URLSearchParams(raw.startsWith("?") ? raw : `?${raw}`);
    const param = params.get("shell");
    if (param === "legacy" || param === "ai") {
      return param;
    }
  }
  return "ai";
}

export function setShellMode(mode: ShellMode): void {
  overrideMode = mode;
  if (typeof window !== "undefined" && window.history) {
    try {
      const url = new URL(window.location.href);
      if (mode === "legacy") {
        url.searchParams.set("shell", "legacy");
      } else {
        url.searchParams.delete("shell");
      }
      window.history.replaceState(
        window.history.state,
        "",
        `${url.pathname}${url.search}${url.hash}`,
      );
    } catch {
      // ignore invalid location
    }
  }
  emit();
}

export function resetShellMode(): void {
  overrideMode = null;
  emit();
}

export function useShellMode(): ShellMode {
  return useSyncExternalStore(
    subscribeShellMode,
    () => getShellMode(),
    () => getShellMode(),
  );
}
