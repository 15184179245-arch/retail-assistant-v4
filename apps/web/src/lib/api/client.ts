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
  rawResponse?: boolean;
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
    rawResponse,
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

  // Combine external signal and timeout signal
  const activeSignal = signal
    ? combineSignals(signal, timeoutController.signal)
    : timeoutController.signal;

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
    response = await fetch(`${baseUrl}${path}`, {
      method,
      headers: requestHeaders,
      body: hasRawBody ? rawBody : isJsonBody ? JSON.stringify(body) : undefined,
      signal: activeSignal,
    });
  } catch (error) {
    if (timerId) clearTimeout(timerId);

    if (isTimedOut) {
      throw new ApiError(`请求超时（超过 ${timeoutMs}ms 未响应）。`, {
        status: 0,
        code: "TIMEOUT",
        userMessage: "请求超时，服务未在规定时间内响应。",
        hint: "请核对网络连接或稍后重试，写操作请勿重复提交。",
        cause: error,
      });
    }

    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }

    throw new ApiError("网络请求失败。", {
      status: 0,
      code: "NETWORK_ERROR",
      userMessage: "无法连接服务，请检查网络后重试。",
      hint: "请检查设备网络信号或确认服务端运行状态。",
      cause: error,
    });
  } finally {
    if (timerId) clearTimeout(timerId);
  }

  if (rawResponse) {
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
    return response as unknown as T;
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (jsonError) {
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

  if (response.status === 403) {
    throw createCategorizedApiError(
      403,
      envelope.code,
      envelope.user_message,
      envelope.hint,
      envelope.error,
    );
  }

  if (response.status === 409) {
    throw createCategorizedApiError(
      409,
      envelope.code,
      envelope.user_message,
      envelope.hint,
      envelope.error,
    );
  }

  if (response.status === 422) {
    throw createCategorizedApiError(
      422,
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
}

function combineSignals(s1: AbortSignal, s2: AbortSignal): AbortSignal {
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([s1, s2]);
  }

  const controller = new AbortController();
  const onAbort = () => controller.abort();

  if (s1.aborted || s2.aborted) {
    controller.abort();
    return controller.signal;
  }

  s1.addEventListener("abort", onAbort, { once: true });
  s2.addEventListener("abort", onAbort, { once: true });
  return controller.signal;
}
