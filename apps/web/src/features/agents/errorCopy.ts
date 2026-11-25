/** 统一的 AI/Agent 错误码中文文案：界面不再直接暴露 provider_* 英文码。 */

export interface AgentErrorCopy {
  title: string;
  hint: string | null;
}

const PROVIDER_HINT = "现场扫码、开单、收货等作业不受影响，可继续手工办理。";

const COPY: Record<string, AgentErrorCopy> = {
  agent_input_invalid: { title: "问题不能为空或过长。", hint: null },
  agent_conflict: { title: "该运行状态已变化，请刷新。", hint: null },
  agent_not_found: { title: "未找到该运行，或无权访问。", hint: null },
  agent_login_required: { title: "登录已失效，请重新登录。", hint: null },
  agent_recovery_pending: { title: "运行已超时，等待恢复处理。", hint: null },
  agent_confirmation_conflict: { title: "待确认动作已变化，请刷新后再试。", hint: null },
  agent_approval_fenced: { title: "这个动作已被处理或已过期。", hint: null },
  agent_budget_exceeded: { title: "本次 AI 用量已达到上限。", hint: PROVIDER_HINT },

  provider_unavailable: { title: "AI 模型通道暂时不可用。", hint: PROVIDER_HINT },
  provider_http_error: { title: "AI 模型返回了异常响应。", hint: PROVIDER_HINT },
  provider_network_error: { title: "连接 AI 模型失败。", hint: PROVIDER_HINT },
  provider_timeout: { title: "AI 模型响应超时。", hint: PROVIDER_HINT },
  provider_rate_limited: { title: "AI 模型请求过多，请稍后再试。", hint: PROVIDER_HINT },
  provider_key: { title: "AI 模型凭据不可用。", hint: PROVIDER_HINT },
  provider_authentication_failed: { title: "AI 模型鉴权失败。", hint: PROVIDER_HINT },
  provider_invalid_request: { title: "发给 AI 模型的请求不合规。", hint: PROVIDER_HINT },
  provider_invalid_response: { title: "AI 模型返回的内容无法解析。", hint: PROVIDER_HINT },
  provider_configuration_invalid: { title: "AI 模型配置不完整。", hint: PROVIDER_HINT },
  provider_budget_exceeded: { title: "本次 AI 用量已超出预算。", hint: PROVIDER_HINT },
  provider_response_too_large: { title: "AI 回答超过长度上限。", hint: PROVIDER_HINT },
  provider_usage_invalid: { title: "AI 用量计量异常。", hint: PROVIDER_HINT },
  agent_provider_error: { title: "AI 模型调用失败。", hint: PROVIDER_HINT },
  cost_price_unconfigured: { title: "模型计价尚未配置。", hint: PROVIDER_HINT },
  cost_budget_unconfigured: { title: "预算策略尚未配置。", hint: PROVIDER_HINT },
};

/** 已知错误码返回中文说明；未知错误码给通用安全文案，原始码只留在 details 里。 */
export function describeAgentError(code: string | null | undefined): AgentErrorCopy | null {
  if (!code) return null;
  return COPY[code] ?? { title: "AI 服务暂时不可用，请稍后重试。", hint: PROVIDER_HINT };
}

export function isKnownErrorCode(code: string | null | undefined): boolean {
  return !!code && code in COPY;
}
