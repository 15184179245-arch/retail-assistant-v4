export interface ApiErrorOptions {
  status?: number;
  code?: string;
  userMessage?: string;
  hint?: string | null;
  cause?: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly userMessage: string;
  readonly hint: string | null;

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = "ApiError";
    this.status = options.status ?? 0;
    this.code = options.code ?? "NETWORK_ERROR";
    this.userMessage = options.userMessage ?? "请求失败，请稍后重试。";
    this.hint = options.hint ?? null;
    if (options.cause) {
      this.cause = options.cause;
    }
  }

  get isUnauthorized(): boolean {
    return this.status === 401 || this.code === "UNAUTHORIZED";
  }

  get isForbidden(): boolean {
    return this.status === 403 || this.code === "FORBIDDEN";
  }

  get isConflict(): boolean {
    return this.status === 409 || this.code === "CONFLICT";
  }

  get isValidationError(): boolean {
    return this.status === 422 || this.code === "VALIDATION_ERROR";
  }

  get isNetworkError(): boolean {
    return this.status === 0 || this.code === "NETWORK_ERROR";
  }

  get isTimeout(): boolean {
    return this.code === "TIMEOUT";
  }

  get isInvalidEnvelope(): boolean {
    return this.code === "INVALID_ENVELOPE";
  }

  get isServerError(): boolean {
    return this.status >= 500 && this.status < 600;
  }
}

/**
 * Creates standardized ApiErrors based on HTTP status codes and envelope values.
 */
export function createCategorizedApiError(
  status: number,
  envelopeCode?: string,
  serverUserMessage?: string,
  serverHint?: string | null,
  rawError?: string | null,
): ApiError {
  let defaultCode = envelopeCode ?? "UNKNOWN";
  let defaultUserMessage = serverUserMessage;
  let defaultHint = serverHint;

  switch (status) {
    case 401:
      defaultCode = envelopeCode ?? "UNAUTHORIZED";
      defaultUserMessage = serverUserMessage ?? "登录已失效，请重新登录。";
      defaultHint = serverHint ?? "请核对员工账号密码后再次登录。";
      break;

    case 403:
      defaultCode = envelopeCode ?? "FORBIDDEN";
      defaultUserMessage = serverUserMessage ?? "当前账号缺少执行此操作的权限。";
      defaultHint = serverHint ?? "请联系系统管理员为您分配相应的功能权限。";
      break;

    case 409:
      defaultCode = envelopeCode ?? "CONFLICT";
      defaultUserMessage = serverUserMessage ?? "操作存在并发版本冲突，数据已被他人更新。";
      defaultHint = serverHint ?? "请刷新页面获取最新版本后再次提交。";
      break;

    case 422:
      defaultCode = envelopeCode ?? "VALIDATION_ERROR";
      defaultUserMessage = serverUserMessage ?? "录入数据格式或业务校验未通过。";
      defaultHint = serverHint ?? "请核对表单标红字段与录入规则。";
      break;

    default:
      if (status >= 500 && status < 600) {
        defaultCode = envelopeCode ?? "SERVER_ERROR";
        defaultUserMessage = serverUserMessage ?? "服务端处理异常，请稍后重试。";
        defaultHint = serverHint ?? "如果问题持续存在，请联系技术人员排查后端服务日志。";
      } else {
        defaultUserMessage = serverUserMessage ?? "请求处理未成功。";
      }
      break;
  }

  const message = rawError || defaultUserMessage || defaultCode;
  return new ApiError(message, {
    status,
    code: defaultCode,
    userMessage: defaultUserMessage,
    hint: defaultHint,
  });
}
