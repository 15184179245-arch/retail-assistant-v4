import { ApiError } from "./errors";

export const REQUIRED_ENVELOPE_FIELDS = [
  "success",
  "data",
  "code",
  "error",
  "user_message",
  "hint",
] as const;

export type RequiredEnvelopeField = (typeof REQUIRED_ENVELOPE_FIELDS)[number];

export interface ApiEnvelope<T = unknown> {
  success: boolean;
  data: T | null;
  code: string;
  error: string | null;
  user_message: string;
  hint: string | null;
}

/**
 * Validates that an arbitrary payload strictly adheres to the standard 6-field envelope contract.
 * Rejects missing fields, malformed types, or null/non-object payloads.
 */
export function parseEnvelope<T = unknown>(payload: unknown): ApiEnvelope<T> {
  if (typeof payload !== "object" || payload === null) {
    throw new ApiError("服务端返回了无法识别的非对象响应。", {
      status: 0,
      code: "INVALID_ENVELOPE",
      userMessage: "服务端返回格式异常，未收到标准信封响应。",
      hint: "请核对网络代理、反向代理网关或后端服务运行状态。",
    });
  }

  const record = payload as Record<string, unknown>;

  // Strict check: every required field must be present as a key in the object
  for (const field of REQUIRED_ENVELOPE_FIELDS) {
    if (!(field in record)) {
      throw new ApiError(`服务端响应缺少必需字段 '${field}'。`, {
        status: 0,
        code: "INVALID_ENVELOPE",
        userMessage: "服务端数据协议不完整，缺少标准状态字段。",
        hint: `请核对接口是否返回了完整的 6 字段统一信封（缺失: ${field}）。`,
      });
    }
  }

  if (typeof record.success !== "boolean") {
    throw new ApiError("服务端响应字段 'success' 必须为布尔类型。", {
      status: 0,
      code: "INVALID_ENVELOPE",
      userMessage: "服务端返回格式异常，success 标志不正确。",
      hint: "请核对后端返回格式。",
    });
  }

  if (typeof record.code !== "string" || record.code.trim().length === 0) {
    throw new ApiError("服务端响应字段 'code' 必须为非空字符串。", {
      status: 0,
      code: "INVALID_ENVELOPE",
      userMessage: "服务端返回格式异常，状态码未定义。",
      hint: "请核对后端返回格式。",
    });
  }

  if (typeof record.user_message !== "string") {
    throw new ApiError("服务端响应字段 'user_message' 必须为字符串类型。", {
      status: 0,
      code: "INVALID_ENVELOPE",
      userMessage: "服务端返回格式异常，缺少中文用户提示。",
      hint: "请核对后端返回格式。",
    });
  }

  return record as unknown as ApiEnvelope<T>;
}
