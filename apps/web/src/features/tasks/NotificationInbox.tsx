import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion } from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";
import { hasCapability, useCapabilities } from "../../lib/auth/capabilities";
import { businessPermission } from "../../lib/auth/businessPermissions";

interface Notice {
  id: string;
  task_id: string;
  kind: "review_requested" | "assigned" | "overdue" | "manual_check" | "failure";
  rule_version: string;
  version: number;
  created_at: string;
  read_at: string | null;
}

const labels: Record<Notice["kind"], string> = {
  review_requested: "任务待人工确认",
  assigned: "新分派任务",
  overdue: "任务已逾期（请人工核对）",
  manual_check: "任务需人工核对",
  failure: "任务建议被拒绝（请人工核对）",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.userMessage : "请求失败，请检查网络后重试。";
}

export function NotificationInbox({ session }: { session: Session }) {
  const canAcknowledge = useCapabilities().hasCapability(businessPermission.notifications.write);
  const [items, setItems] = useState<Notice[]>([]);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const pendingKeys = useRef<Record<string, string>>({});

  async function refresh() {
    const version = getScopeVersion();
    const data = await authorizedRequest<Notice[]>("/api/v1/notifications?limit=50");
    if (version === getScopeVersion()) setItems(data);
  }

  useEffect(() => {
    setItems([]);
    setError("");
    pendingKeys.current = {};
    const version = getScopeVersion();
    authorizedRequest<Notice[]>("/api/v1/notifications?limit=50")
      .then(data => { if (version === getScopeVersion()) setItems(data); })
      .catch(e => { if (version === getScopeVersion()) setError(errorText(e)); });
  }, [session]);

  async function markRead(notice: Notice) {
    if (!hasCapability(businessPermission.notifications.write) || busyId || notice.read_at) return;
    const version = getScopeVersion();
    setBusyId(notice.id);
    setError("");
    const key = pendingKeys.current[notice.id] ?? crypto.randomUUID();
    pendingKeys.current[notice.id] = key;
    try {
      await authorizedRequest("/api/v1/notifications/" + encodeURIComponent(notice.id) + "/read", {
        method: "POST",
        headers: { "Idempotency-Key": key },
        body: { expected_version: notice.version },
      });
      if (version !== getScopeVersion()) return;
      delete pendingKeys.current[notice.id];
      await refresh();
    } catch (e) {
      if (version === getScopeVersion()) setError(errorText(e));
    } finally {
      if (version === getScopeVersion()) setBusyId(null);
    }
  }

  return <section className="welcome-card" style={{ maxWidth: 960 }} aria-labelledby="notification-title">
    <p className="eyebrow">本人 · 当前门店 · 站内通知</p>
    <h2 id="notification-title">站内提醒</h2>
    <p className="welcome-copy">
      只显示当前登录员工在本店仍有权查看的任务提醒。已读不等于审批或完成任务。
      不自动发送邮件、微信或短信；升级责任人需要另行批准规则。
    </p>
    <button type="button" className="link-button" onClick={() => void refresh().catch(e => setError(errorText(e)))}>
      刷新收件箱
    </button>
    {error ? <p className="login-error" role="alert">{error}</p> : null}
    {items.length === 0 ? <p>暂无可见通知（或当前账号未获通知权限）。</p> : null}
    <ul style={{ display: "grid", gap: 12, paddingLeft: 22 }}>
      {items.map(notice => <li key={notice.id} style={{ borderBottom: "1px solid #e5e9e7", paddingBottom: 12 }}>
        <strong>{labels[notice.kind] ?? "任务提醒"}</strong>
        <p>任务编号：{notice.task_id} · {new Date(notice.created_at).toLocaleString()}</p>
        <small>规则：{notice.rule_version} · {notice.read_at ? "已读" : "未读"}</small>
        {canAcknowledge && !notice.read_at ? <div>
          <button type="button" className="login-button" disabled={busyId !== null}
            onClick={() => void markRead(notice)}>
            {busyId === notice.id ? "保存中…" : "标记已读（不执行任务）"}
          </button>
        </div> : null}
      </li>)}
    </ul>
  </section>;
}
