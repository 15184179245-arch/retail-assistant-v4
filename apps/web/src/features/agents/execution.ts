import type { AgentRunReceipt } from "./AgentChatPage";

export interface AgentExecution {
  protocol_version: 1;
  phase: "queued" | "claimed" | "model_inflight" | "waiting_for_human" |
    "tool_inflight" | "reconciling" | "terminal";
  cancel_requested: boolean;
  cancel_resolution: "not_requested" | "requested" | "cancelled" | "too_late" | "unknown";
  effect_status: "none" | "local_task_committed" | "external_unknown";
}

export class ExecutionProtocolError extends Error {
  constructor() { super("后台状态暂不可识别，请重新读取原运行，勿重复操作。"); }
}

const phases = new Set(["queued", "claimed", "model_inflight", "waiting_for_human",
  "tool_inflight", "reconciling", "terminal"]);
const resolutions = new Set(["not_requested", "requested", "cancelled", "too_late", "unknown"]);
const effects = new Set(["none", "local_task_committed", "external_unknown"]);
const runStates = new Set(["created", "running", "waiting_for_human", "waiting_for_external",
  "completed", "failed", "cancelled"]);
export const terminalRun = (state: string): boolean => ["completed", "failed", "cancelled"].includes(state);

export function parseExecution(value: unknown, state: string): AgentExecution | null {
  if (value == null) return null;
  if (typeof value !== "object" || Array.isArray(value)) throw new ExecutionProtocolError();
  const data = value as Record<string, unknown>;
  const keys = ["protocol_version", "phase", "cancel_requested", "cancel_resolution", "effect_status"];
  if (Object.keys(data).length !== keys.length || keys.some(key => !(key in data)) ||
      data.protocol_version !== 1 || typeof data.cancel_requested !== "boolean" ||
      !phases.has(data.phase as string) || !resolutions.has(data.cancel_resolution as string) ||
      !effects.has(data.effect_status as string)) throw new ExecutionProtocolError();
  if (data.cancel_requested !== (data.cancel_resolution !== "not_requested") ||
      (data.phase === "terminal") !== terminalRun(state) ||
      (data.cancel_resolution === "cancelled" && (data.effect_status !== "none" || state !== "cancelled")) ||
      (data.cancel_resolution === "too_late" && state !== "completed") ||
      (data.phase === "waiting_for_human" && state !== "waiting_for_human") ||
      (["model_inflight", "tool_inflight"].includes(data.phase as string) && state !== "running")) {
    throw new ExecutionProtocolError();
  }
  return data as unknown as AgentExecution;
}

/** A new execution field is validated before any cached answer or action is shown. */
export function checkedReceipt(receipt: AgentRunReceipt, expectedId?: string): AgentRunReceipt {
  if (!receipt || typeof receipt.run_id !== "string" || !runStates.has(receipt.state) ||
      (expectedId != null && receipt.run_id !== expectedId)) throw new ExecutionProtocolError();
  return { ...receipt, execution: parseExecution(receipt.execution, receipt.state) };
}

export function allowsHumanDecision(run: AgentRunReceipt, now = Date.now()): boolean {
  const deadline = run.pending_action ? Date.parse(run.pending_action.expires_at) : NaN;
  return Number.isFinite(deadline) && deadline > now && run.state === "waiting_for_human" && !!run.pending_action &&
    (!run.execution || (run.execution.phase === "waiting_for_human" &&
      !run.execution.cancel_requested && run.execution.effect_status !== "external_unknown"));
}

export function executionNotice(run: AgentRunReceipt): string | null {
  const value = run.execution;
  if (!value) return null;
  if (value.effect_status === "external_unknown" || value.cancel_resolution === "unknown" ||
      value.phase === "reconciling") return "执行结果尚未核实；请读取原运行并核对，勿重复提交或确认。";
  if (value.cancel_resolution === "too_late") return value.effect_status === "local_task_committed"
    ? "任务建议已提交，取消未生效；请查看原任务。" : "运行已经结束，取消未生效；请核对原结果。";
  if (value.cancel_resolution === "cancelled") return "取消已确认，未产生新的业务写入。";
  if (value.cancel_requested) return "取消请求已记录，正在核对执行结果。";
  if (value.phase === "queued") return run.state === "running"
    ? "批准已记录，等待后台执行；尚未生成任务建议。" : "已排队，等待后台处理。";
  if (value.phase === "waiting_for_human") return "等待人工确认；批准后仍需后台执行。";
  if (["claimed", "model_inflight", "tool_inflight"].includes(value.phase)) return "后台正在处理任务。";
  if (value.effect_status === "local_task_committed") return "任务建议已提交，等待负责人审阅。";
  return null;
}
