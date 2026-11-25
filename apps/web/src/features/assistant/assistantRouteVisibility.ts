import { createContext, useContext, useSyncExternalStore } from 'react';
import { getPath, subscribe } from '../../app/router';

export function isAssistantPath(path: string): boolean {
  return path === '/' || path === '/assistant';
}

export type AssistantRouteVisibility = {
  signal: () => AbortSignal;
  subscribe: (listener: () => void) => () => void;
};

export function createAssistantRouteVisibility(
  path: () => string, subscribePath: (listener: () => void) => () => void,
): AssistantRouteVisibility {
  let active = isAssistantPath(path());
  let permit = new AbortController();
  if (!active) permit.abort();
  const listeners = new Set<() => void>();
  subscribePath(() => {
    const next = isAssistantPath(path());
    if (next === active) return;
    active = next;
    if (next) permit = new AbortController();
    else permit.abort();
    for (const listener of listeners) listener();
  });
  return {
    signal: () => permit.signal,
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

/** One app-route epoch, independent of tab visibility and of delivered write receipts. */
export const assistantRouteVisibility = createAssistantRouteVisibility(getPath, subscribe);
export const AssistantRouteContext = createContext<AssistantRouteVisibility | undefined>(undefined);
const subscribeNone = () => () => {};
const noSignal = () => undefined;
export function useAssistantRouteSignal(): AbortSignal | undefined {
  const route = useContext(AssistantRouteContext);
  return useSyncExternalStore(route?.subscribe ?? subscribeNone, route?.signal ?? noSignal, route?.signal ?? noSignal);
}

export class AssistantRouteInterrupted extends Error {}

/** A literal signal combination is kept for Safari versions without AbortSignal.any. */
export function linkAssistantRead(permit?: AbortSignal, external?: AbortSignal) {
  if (!permit || !external || permit === external)
    return { signal: permit ?? external, finish: () => {} };
  const combined = new AbortController();
  const onRoute = () => { combined.abort(permit.reason); };
  const onExternal = () => { combined.abort(external.reason); };
  if (permit.aborted) onRoute();
  else if (external.aborted) onExternal();
  else {
    permit.addEventListener('abort', onRoute, { once: true });
    external.addEventListener('abort', onExternal, { once: true });
  }
  return {
    signal: combined.signal,
    finish() {
      permit.removeEventListener('abort', onRoute);
      external.removeEventListener('abort', onExternal);
    },
  };
}

/** Do not hand data to native clipboard/download after an interrupted read. */
export async function loadAssistantExport(
  read: (signal: AbortSignal) => Promise<string>, signal: AbortSignal, current: () => boolean,
): Promise<string> {
  if (signal.aborted || !current()) throw new AssistantRouteInterrupted();
  const text = await read(signal);
  if (signal.aborted || !current()) throw new AssistantRouteInterrupted();
  return text;
}
