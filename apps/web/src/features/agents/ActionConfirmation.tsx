import type { PendingAction } from "./AgentChatPage";

export interface ActionConfirmationProps {
  pending: PendingAction;
  disabled?: boolean;
  onDecision: (decision: "approve" | "reject") => void;
}

export function ActionConfirmation({ pending, disabled = false, onDecision }: ActionConfirmationProps) {
  const params = JSON.stringify(pending.arguments, null, 2);
  return (
    <section className="confirm-card" aria-label="等待人工确认">
      <div className="confirm-card__head">
        <span className="confirm-card__icon" aria-hidden="true">
          !
        </span>
        <div>
          <h3>等待人工确认</h3>
          <p className="confirm-card__sub">这一步会写入或影响业务数据，需要你确认后才执行。</p>
        </div>
      </div>
      <dl className="confirm-card__meta">
        <div>
          <dt>工具</dt>
          <dd>
            {pending.tool_key}@{pending.version}
          </dd>
        </div>
        <div>
          <dt>有效期至</dt>
          <dd>{pending.expires_at}</dd>
        </div>
      </dl>
      <details className="confirm-card__args">
        <summary>查看提交参数</summary>
        <pre aria-label="工具参数">{params}</pre>
      </details>
      <div className="confirm-card__actions">
        <button type="button" className="btn btn--ghost" disabled={disabled} onClick={() => onDecision("reject")}>
          拒绝
        </button>
        <button type="button" className="btn btn--primary" disabled={disabled} onClick={() => onDecision("approve")}>
          确认执行
        </button>
      </div>
    </section>
  );
}
