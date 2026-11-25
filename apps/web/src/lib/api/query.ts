import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

export type QueryKey = string | readonly unknown[];

export type QueryStatus = "idle" | "loading" | "success" | "error";

export interface QueryEntry<T = unknown> {
  key: string;
  data: T | undefined;
  error: unknown;
  status: QueryStatus;
  updatedAt: number;
  stale: boolean;
}

type Listener = () => void;

export function serializeQueryKey(key: QueryKey): string {
  if (typeof key === "string") {
    return key;
  }
  return JSON.stringify(key);
}

function keyMatchesPrefix(serializedKey: string, prefix: QueryKey): boolean {
  if (typeof prefix === "string") {
    return serializedKey === prefix || serializedKey.startsWith(prefix);
  }
  try {
    const parsedKey = JSON.parse(serializedKey);
    if (!Array.isArray(parsedKey)) {
      return false;
    }
    if (prefix.length > parsedKey.length) {
      return false;
    }
    for (let i = 0; i < prefix.length; i += 1) {
      if (JSON.stringify(parsedKey[i]) !== JSON.stringify(prefix[i])) {
        return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}

class QueryCacheClient {
  private entries = new Map<string, QueryEntry<unknown>>();
  private inflight = new Map<string, { promise: Promise<unknown>; controller: AbortController; epoch: number }>();
  private listeners = new Set<Listener>();
  private epoch = 0;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private emit(): void {
    for (const listener of this.listeners) {
      listener();
    }
  }

  getEpoch(): number {
    return this.epoch;
  }

  getSize(): number {
    return this.entries.size;
  }

  getEntry<T>(key: QueryKey): QueryEntry<T> | undefined {
    const serialized = serializeQueryKey(key);
    return this.entries.get(serialized) as QueryEntry<T> | undefined;
  }

  getQueryData<T>(key: QueryKey): T | undefined {
    return this.getEntry<T>(key)?.data;
  }

  setQueryData<T>(key: QueryKey, data: T): void {
    const serialized = serializeQueryKey(key);
    this.entries.set(serialized, {
      key: serialized,
      data,
      error: null,
      status: "success",
      updatedAt: Date.now(),
      stale: false,
    });
    this.emit();
  }

  async fetchQuery<T>(options: {
    queryKey: QueryKey;
    queryFn: (signal: AbortSignal) => Promise<T>;
    staleTimeMs?: number;
    force?: boolean;
  }): Promise<T> {
    const { queryKey, queryFn, staleTimeMs = 15_000, force = false } = options;
    const serialized = serializeQueryKey(queryKey);
    const existing = this.entries.get(serialized) as QueryEntry<T> | undefined;

    if (
      !force &&
      existing &&
      existing.status === "success" &&
      !existing.stale &&
      Date.now() - existing.updatedAt < staleTimeMs
    ) {
      return existing.data as T;
    }

    const activeInflight = this.inflight.get(serialized);
    if (!force && activeInflight && activeInflight.epoch === this.epoch) {
      return activeInflight.promise as Promise<T>;
    }

    const requestEpoch = this.epoch;
    const controller = new AbortController();

    this.entries.set(serialized, {
      key: serialized,
      data: existing?.data,
      error: null,
      status: "loading",
      updatedAt: existing?.updatedAt ?? 0,
      stale: existing?.stale ?? false,
    });
    this.emit();

    const promise = (async () => {
      try {
        const result = await queryFn(controller.signal);
        if (this.epoch !== requestEpoch || controller.signal.aborted) throw new Error("QUERY_SCOPE_CHANGED");
        if (this.epoch === requestEpoch && !controller.signal.aborted) {
          this.entries.set(serialized, {
            key: serialized,
            data: result,
            error: null,
            status: "success",
            updatedAt: Date.now(),
            stale: false,
          });
          this.emit();
        }
        return result;
      } catch (err) {
        if (this.epoch === requestEpoch && !controller.signal.aborted) {
          this.entries.set(serialized, {
            key: serialized,
            data: existing?.data,
            error: err,
            status: "error",
            updatedAt: existing?.updatedAt ?? Date.now(),
            stale: true,
          });
          this.emit();
        }
        throw err;
      } finally {
        const tracked = this.inflight.get(serialized);
        if (tracked && tracked.controller === controller) {
          this.inflight.delete(serialized);
        }
      }
    })();

    this.inflight.set(serialized, { promise, controller, epoch: requestEpoch });
    return promise;
  }

  invalidateQueries(prefix?: QueryKey): void {
    let changed = false;
    for (const [key, entry] of this.entries.entries()) {
      if (!prefix || keyMatchesPrefix(key, prefix)) {
        this.entries.set(key, { ...entry, stale: true });
        changed = true;
      }
    }
    if (changed) {
      this.emit();
    }
  }

  clear(): void {
    this.epoch += 1;
    for (const active of this.inflight.values()) {
      active.controller.abort();
    }
    this.inflight.clear();
    this.entries.clear();
    this.emit();
  }
}

export const queryClient = new QueryCacheClient();

export function invalidateQueries(prefix?: QueryKey): void {
  queryClient.invalidateQueries(prefix);
}

export function clearQueryCache(): void {
  queryClient.clear();
}

export function getQueryCacheSize(): number {
  return queryClient.getSize();
}

export interface UseQueryOptions<T> {
  queryKey: QueryKey;
  queryFn: (signal: AbortSignal) => Promise<T>;
  enabled?: boolean;
  staleTimeMs?: number;
  initialData?: T;
}

export interface UseQueryResult<T> {
  data: T | undefined;
  error: unknown;
  status: QueryStatus;
  isLoading: boolean;
  isError: boolean;
  isSuccess: boolean;
  refetch: () => Promise<T | undefined>;
}

export function useQuery<T>(options: UseQueryOptions<T>): UseQueryResult<T> {
  const { queryKey, queryFn, enabled = true, staleTimeMs = 15_000, initialData } = options;
  const serialized = serializeQueryKey(queryKey);
  const queryFnRef = useRef(queryFn);
  queryFnRef.current = queryFn;

  const snapshot = useSyncExternalStore(
    queryClient.subscribe,
    () => queryClient.getEntry<T>(serialized),
    () => queryClient.getEntry<T>(serialized),
  );

  const refetch = useCallback(async (): Promise<T | undefined> => {
    if (!enabled) return undefined;
    return queryClient.fetchQuery({
      queryKey: serialized,
      queryFn: (signal) => queryFnRef.current(signal),
      staleTimeMs,
      force: true,
    });
  }, [enabled, serialized, staleTimeMs]);

  useEffect(() => {
    if (!enabled) return;
    const current = queryClient.getEntry<T>(serialized);
    const shouldFetch =
      !current ||
      current.stale ||
      current.status === "idle" ||
      (current.status === "success" && Date.now() - current.updatedAt >= staleTimeMs);
    if (shouldFetch) {
      void queryClient
        .fetchQuery({
          queryKey: serialized,
          queryFn: (signal) => queryFnRef.current(signal),
          staleTimeMs,
        })
        .catch(() => {
          // Error state is stored in queryClient entry for UI rendering
        });
    }
  }, [enabled, serialized, staleTimeMs, snapshot?.stale]);

  const data = snapshot?.data ?? initialData;
  const status: QueryStatus = snapshot?.status ?? (initialData !== undefined ? "success" : "idle");

  return {
    data,
    error: snapshot?.error ?? null,
    status,
    isLoading: status === "loading",
    isError: status === "error",
    isSuccess: status === "success",
    refetch,
  };
}

export interface UseMutationOptions<TData, TVariables> {
  mutationFn: (variables: TVariables) => Promise<TData>;
  onSuccess?: (data: TData, variables: TVariables) => void;
  onError?: (error: unknown, variables: TVariables) => void;
  invalidateKeys?: QueryKey[];
}

export interface UseMutationResult<TData, TVariables> {
  mutate: (variables: TVariables) => void;
  mutateAsync: (variables: TVariables) => Promise<TData>;
  data: TData | undefined;
  error: unknown;
  status: QueryStatus;
  isPending: boolean;
  isError: boolean;
  isSuccess: boolean;
  reset: () => void;
}

export function useMutation<TData = unknown, TVariables = void>(
  options: UseMutationOptions<TData, TVariables>,
): UseMutationResult<TData, TVariables> {
  const [state, setState] = useState<{
    data: TData | undefined;
    error: unknown;
    status: QueryStatus;
  }>({
    data: undefined,
    error: null,
    status: "idle",
  });

  const inflightRef = useRef(false);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const reset = useCallback(() => {
    setState({ data: undefined, error: null, status: "idle" });
  }, []);

  const mutateAsync = useCallback(async (variables: TVariables): Promise<TData> => {
    if (inflightRef.current) {
      throw new Error("MUTATION_IN_FLIGHT");
    }
    const startEpoch = queryClient.getEpoch();
    inflightRef.current = true;
    setState((prev) => ({ ...prev, error: null, status: "loading" }));
    try {
      const result = await optionsRef.current.mutationFn(variables);
      if (queryClient.getEpoch() !== startEpoch) throw new Error("MUTATION_SCOPE_CHANGED");
      if (queryClient.getEpoch() === startEpoch) {
        setState({ data: result, error: null, status: "success" });
        if (optionsRef.current.invalidateKeys) {
          for (const key of optionsRef.current.invalidateKeys) {
            queryClient.invalidateQueries(key);
          }
        }
        optionsRef.current.onSuccess?.(result, variables);
      }
      return result;
    } catch (error) {
      if (queryClient.getEpoch() === startEpoch) {
        setState((prev) => ({ ...prev, error, status: "error" }));
        optionsRef.current.onError?.(error, variables);
      }
      throw error;
    } finally {
      inflightRef.current = false;
    }
  }, []);

  const mutate = useCallback(
    (variables: TVariables) => {
      void mutateAsync(variables).catch(() => {});
    },
    [mutateAsync],
  );

  return {
    mutate,
    mutateAsync,
    data: state.data,
    error: state.error,
    status: state.status,
    isPending: state.status === "loading",
    isError: state.status === "error",
    isSuccess: state.status === "success",
    reset,
  };
}
