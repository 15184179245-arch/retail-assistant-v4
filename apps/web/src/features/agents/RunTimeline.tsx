import type { AgentStep } from "./AgentChatPage";
import { describeAgentError, isKnownErrorCode } from "./errorCopy";

export interface RunTimelineProps {
  steps: AgentStep[];
  runState: string;
  errorCode?: string | null;
}

const STATE_LABELS: Record<string, string> = {
  pending: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  unknown: "结果未知",
  cancelled: "已取消",
  created: "已创建",
  waiting_for_human: "等待人工确认",
  waiting_for_external: "等待外部系统",
  completed: "已完成",
};

const KIND_LABELS: Record<string, string> = {
  model: "模型",
  tool: "工具",
  checkpoint: "检查点",
};

export function stepStateLabel(state: string): string {
  return STATE_LABELS[state] ?? state;
}

export function RunTimeline({ steps, runState, errorCode }: RunTimelineProps) {
  const collapsed = ["completed", "cancelled"].includes(runState);
  const failed = runState === "failed";
  return (
    <section className="run-steps" aria-label="Agent 步骤轨迹" data-run-state={runState}>
      <details className="run-steps__details" open={!collapsed}>
        <summary className="run-steps__summary">
          <span>本轮步骤</span>
          <span className="run-steps__count">{steps.length}</span>
        </summary>
        {steps.length === 0 ? (
          <p className="run-steps__empty">尚未产生步骤。</p>
        ) : (
          <ol className="run-steps__list">
            {steps.map((step) => (
              <li key={step.id} data-step-state={step.state} className={`run-step is-${step.state}`}>
                <span className="run-step__dot" aria-hidden="true" />
                <div className="run-step__body">
                  <div className="run-step__head">
                    <span className="run-step__kind">{KIND_LABELS[step.kind] ?? step.kind}</span>
                    <span className="run-step__state">{stepStateLabel(step.state)}</span>
                    <span className="run-step__seq">#{step.sequence_no}</span>
                  </div>
                  {step.tool_key ? (
                    <p className="run-step__source">
                      来源：{step.tool_key}@{step.tool_version ?? "?"}
                    </p>
                  ) : null}
                  {step.input_tokens !== null || step.output_tokens !== null ? (
                    <p className="run-step__usage">
                      用量：{step.input_tokens ?? 0} / {step.output_tokens ?? 0}
                    </p>
                  ) : null}
                  {step.error_code ? (
                    <p className="run-step__error" role="alert">
                      {describeAgentError(step.error_code)?.title ?? "步骤执行失败。"}
                      {isKnownErrorCode(step.error_code) ? null : (
                        <span className="run-step__code">（{step.error_code}）</span>
                      )}
                    </p>
                  ) : null}
                </div>
              </li>
            ))}
          </ol>
        )}
      </details>
      <p className="run-steps__state">运行状态：{stepStateLabel(runState)}</p>
      {errorCode ? (
        <p className="run-steps__alert" role="alert">
          {failed ? "运行失败：" : "运行提示："}
          {errorCode}
        </p>
      ) : null}
    </section>
  );
}
