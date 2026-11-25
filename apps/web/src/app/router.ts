import { useSyncExternalStore } from "react";

type Listener = () => void;
const listeners = new Set<Listener>();

let memoryPath = "/";
let memorySearch = "";

function notify(): void {
  for (const listener of listeners) {
    listener();
  }
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getPath(): string {
  if (typeof window !== "undefined" && window.location) {
    return window.location.pathname || "/";
  }
  return memoryPath;
}

export function getSearch(): string {
  if (typeof window !== "undefined" && window.location) {
    return window.location.search || "";
  }
  return memorySearch;
}

export function usePath(): string {
  return useSyncExternalStore(subscribe, getPath, getPath);
}

export function useSearch(): string {
  return useSyncExternalStore(subscribe, getSearch, getSearch);
}

export function useSearchParams(): URLSearchParams {
  const search = useSearch();
  return new URLSearchParams(search);
}

export function navigate(to: string, options: { replace?: boolean } = {}): void {
  const [path, search = ""] = to.split("?");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const fullSearch = search ? `?${search}` : "";

  if (typeof window !== "undefined" && window.history) {
    const fullUrl = `${normalizedPath}${fullSearch}`;
    if (options.replace) {
      window.history.replaceState(null, "", fullUrl);
    } else {
      window.history.pushState(null, "", fullUrl);
    }
  } else {
    memoryPath = normalizedPath;
    memorySearch = fullSearch;
  }
  notify();
}

/** Set memory location for isolated tests without window.location */
export function setMemoryLocation(path: string, search = ""): void {
  memoryPath = path.startsWith("/") ? path : `/${path}`;
  memorySearch = search ? (search.startsWith("?") ? search : `?${search}`) : "";
  notify();
}

/** Reset memory location to default root */
export function resetRouter(): void {
  memoryPath = "/";
  memorySearch = "";
  notify();
}

/** Validate whether a redirect target is a safe internal path (prevents open redirects). */
export function isSafeInternalPath(target: string | null | undefined): boolean {
  if (!target || typeof target !== "string") {
    return false;
  }
  // Must start with '/' and not '//' (prevents protocol-relative URL //evil.com)
  if (!target.startsWith("/") || target.startsWith("//")) {
    return false;
  }
  // Must not contain scheme delimiter '://'
  if (target.includes("://")) {
    return false;
  }
  // Must not contain backslashes, control characters, or javascript scheme
  if (/[\\]|[\x00-\x1F\x7F]|javascript:/i.test(target)) {
    return false;
  }
  return true;
}

if (typeof window !== "undefined") {
  window.addEventListener("popstate", () => {
    notify();
  });
}
