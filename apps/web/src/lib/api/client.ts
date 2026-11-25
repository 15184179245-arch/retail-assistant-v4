import { parseEnvelope } from "./envelope";
import type { ApiEnvelope } from "./envelope";
import { ApiError, createCategorizedApiError } from "./errors";
import { generateIdempotencyKey, IdempotentScope } from "./idempotency";

export { parseEnvelope, ApiError, createCategorizedApiError, generateIdempotencyKey, IdempotentScope };
export type { ApiEnvelope };

export interface LoginResponse {
  access_token: string;
  token_type: "Bearer";
  expires_at: string;
  subject_id: string;
  organization_id: string;
  store_id: string;
}

export type LoginData = LoginResponse;

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  rawBody?: BodyInit;
  cache?: RequestCache;
  redirect?: RequestRedirect;
  readResponse?: (response: Response) => Promise<unknown>;
  signal?: AbortSignal;
  token?: string | null;
  baseUrl?: string;
  headers?: Record<string, string>;
  timeoutMs?: number;
  idempotencyKey?: string;
  onUnauthorized?: () => void;
}

export function authHeaders(token?: string | null): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const {
    method = "GET",
    body,
    rawBody,
    cache,
    redirect,
    readResponse,
    signal,
    token,
    baseUrl = "",
    headers: extraHeaders,
    timeoutMs = 15000,
    idempotencyKey,
    onUnauthorized,
  } = options;

  // Handle timeout cancellation
  let isTimedOut = false;
  const timeoutController = new AbortController();
  let timerId: ReturnType<typeof setTimeout> | undefined;

  if (timeoutMs > 0) {
    timerId = setTimeout(() => {
      isTimedOut = true;
      timeoutController.abort();
    }, timeoutMs);
  }

  // One controller owns fetch and body consumption; remove the caller listener at completion.
  const abortExternal = () => timeoutController.abort(signal?.reason);
  if (signal?.aborted) abortExternal();
  else signal?.addEventListener("abort", abortExternal, { once: true });
  const activeSignal = timeoutController.signal;

  try {
    activeSignal.throwIfAborted();
    const hasRawBody = rawBody !== undefined;
    const isJsonBody = body !== undefined && !hasRawBody;

    const requestHeaders: Record<string, string> = {
      Accept: "application/json",
      ...(isJsonBody ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(token),
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      ...(extraHeaders ?? {}),
    };

    let response: Response;
    try {
      response = await awaitWithSignal(fetch(`${baseUrl}${path}`, {
        method,
        headers: requestHeaders,
        body: hasRawBody ? rawBody : isJsonBody ? JSON.stringify(body) : undefined,
        signal: activeSignal,
        ...(cache===undefined?{}:{cache}),
        ...(redirect===undefined?{}:{redirect}),
      }), activeSignal);
    } catch (error) {
      if (activeSignal.aborted) throw activeSignal.reason;

      throw new ApiError("网络请求失败。", {
        status: 0,
        code: "NETWORK_ERROR",
        userMessage: "无法连接服务，请检查网络后重试。",
        hint: "请检查设备网络信号或确认服务端运行状态。",
        cause: error,
      });
    }

    if (readResponse) {
      if (!response.ok) {
        if (response.status === 401) {
          onUnauthorized?.();
          throw createCategorizedApiError(401, "UNAUTHORIZED", "登录已失效，请重新登录。", null, null);
        }
        throw createCategorizedApiError(
          response.status,
          "HTTP_ERROR",
          `请求失败 (HTTP ${response.status})`,
          null,
          null,
        );
      }
      return await awaitWithSignal(readResponse(response), activeSignal) as T;
    }

    let payload: unknown;
    try {
      payload = await awaitWithSignal(response.json(), activeSignal);
    } catch (jsonError) {
      if (activeSignal.aborted) throw activeSignal.reason;
      if (response.status === 401) {
        onUnauthorized?.();
        throw createCategorizedApiError(401, "UNAUTHORIZED", "登录已失效，请重新登录。", null, null);
      }
      throw new ApiError("服务端响应不是合法的 JSON 格式。", {
        status: response.status,
        code: "INVALID_ENVELOPE",
        userMessage: "服务端返回格式异常，解析失败。",
        hint: "请核对服务端返回数据类型。",
        cause: jsonError,
      });
    }

    let envelope: ApiEnvelope<unknown>;
    try {
      envelope = parseEnvelope(payload);
    } catch (envelopeError) {
      if (response.status === 401) {
        onUnauthorized?.();
        throw createCategorizedApiError(401, "UNAUTHORIZED", "登录已失效，请重新登录。", null, null);
      }
      throw envelopeError;
    }

    if (response.status === 401) {
      onUnauthorized?.();
      throw createCategorizedApiError(
        401,
        envelope.code,
        envelope.user_message,
        envelope.hint,
        envelope.error,
      );
    }

    if (!envelope.success || response.status < 200 || response.status >= 300) {
      throw createCategorizedApiError(
        response.status,
        envelope.code,
        envelope.user_message,
        envelope.hint,
        envelope.error,
      );
    }

    return envelope.data as T;
  } catch (error) {
    if (isTimedOut) {
      throw new ApiError(`请求超时（超过 ${timeoutMs}ms 未完成）。`, {
        status: 0, code: "TIMEOUT", userMessage: "请求超时，服务未在规定时间内响应。",
        hint: "请核对网络连接或稍后重试，写操作请勿重复提交。", cause: error,
      });
    }
    throw error;
  } finally {
    if (timerId !== undefined) clearTimeout(timerId);
    signal?.removeEventListener("abort", abortExternal);
  }
}

/** A response consumer cannot outlive the same request deadline, even if it ignores abort. */
function awaitWithSignal<T>(pending: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason);
    if (signal.aborted) { pending.catch(() => {}); reject(signal.reason); return; }
    signal.addEventListener("abort", abort, { once: true });
    pending.then(
      value => { signal.removeEventListener("abort", abort); resolve(value); },
      error => { signal.removeEventListener("abort", abort); reject(error); },
    );
  });
}
