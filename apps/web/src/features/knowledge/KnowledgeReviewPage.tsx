import { useEffect, useState } from "react";
import { ApiError } from "../../lib/api/client";
import type { Session } from "../../lib/auth/session";
import { authorizedRequest } from "../../lib/auth/session";

export interface KnowledgeCandidate {
  id: string;
  version: number;
  revisionId: string;
  title: string;
  excerpt: string;
  status: "pending_review" | "approved" | "rejected" | "withdrawn";
  sourceType: string;
  sourceRef: string | null;
  updatedAt: string;
}

interface QueueItem {
  id: string;
  version: number;
  status: KnowledgeCandidate["status"];
  submitted_at: string;
  revision: { id: string; title: string; body: string; source_type: string; source_ref: string | null } | null;
}

export interface KnowledgeReviewPageProps {
  entries?: KnowledgeCandidate[];
  session?: Session;
  baseUrl?: string;
  onDecision?: (id: string, action: "approve" | "reject" | "withdraw") => void;
}

const STATUS_LABELS: Record<KnowledgeCandidate["status"], string> = {
  pending_review: "待审核",
  approved: "已通过",
  rejected: "已驳回",
  withdrawn: "已撤回",
};

function toCandidate(item: QueueItem): KnowledgeCandidate {
  return {
    id: item.id,
    version: item.version,
    revisionId: item.revision?.id ?? "",
    title: item.revision?.title ?? "（无标题）",
    excerpt: item.revision?.body ?? "",
    status: item.status,
    sourceType: item.revision?.source_type ?? "unknown",
    sourceRef: item.revision?.source_ref ?? null,
    updatedAt: item.submitted_at,
  };
}

export function buildKnowledgeDecision(entry: KnowledgeCandidate,
  action: "approve" | "reject" | "withdraw") {
  return action === "withdraw"
    ? { reason: "复核页撤回", expected_version: entry.version }
    : { decision: action, reason: "复核页审核", expected_version: entry.version,
        revision_id: entry.revisionId };
}

export function KnowledgeReviewPage({
  entries = [],
  session,
  baseUrl = "",
  onDecision,
}: KnowledgeReviewPageProps) {
  const [items, setItems] = useState<KnowledgeCandidate[]>(entries);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh(): Promise<void> {
    if (!session) {
      return;
    }
    const data = await authorizedRequest<{ items: QueueItem[] }>(
      "/api/v1/knowledge/review-queue",
      { baseUrl },
    );
    setItems((data.items ?? []).map(toCandidate));
  }

  useEffect(() => {
    if (!session) {
      setItems(entries);
      return;
    }
    setBusy(true);
    refresh()
      .catch((caught) => {
        setError(caught instanceof ApiError ? caught.userMessage : "复核队列读取失败。");
      })
      .finally(() => setBusy(false));
  }, [session?.subjectId, session?.organizationId, session?.storeId]);

  async function decide(id: string, action: "approve" | "reject" | "withdraw"): Promise<void> {
    if (busy) {
      return;
    }
    const entry = items.find((item) => item.id === id);
    if (!entry) return;
    const body = buildKnowledgeDecision(entry, action);
    // Stable across a lost response and a user retry of this exact action/version.
    const key = `knowledge:${id}:${entry.version}:${action}`;
    setBusy(true);
    setError(null);
    try {
      if (session) {
        if (action === "withdraw") {
          await authorizedRequest(`/api/v1/knowledge/${id}/withdraw`, {
            method: "POST", baseUrl, headers: { "Idempotency-Key": key }, body,
          });
        } else {
          await authorizedRequest(`/api/v1/knowledge/${id}/review`, {
            method: "POST", baseUrl, headers: { "Idempotency-Key": key }, body,
          });
        }
        await refresh();
      }
      onDecision?.(id, action);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.userMessage : "复核操作失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="welcome-card" aria-labelledby="knowledge-title">
      <p className="eyebrow">管理端 · 知识复核</p>
      <h2 id="knowledge-title">知识候选与来源</h2>
      <p className="welcome-copy">
        只展示真实提交的来源与版本；通过后才进入检索，撤回后不再作为依据。
      </p>
      {error ? <p className="login-error" role="alert">{error}</p> : null}
      {items.length === 0 ? <p>{busy ? "正在读取复核队列…" : "暂无知识候选。"}</p> : (
        <ul className="knowledge-list">
          {items.map((entry) => (
            <li key={entry.id} data-status={entry.status}>
              <h3>{entry.title}</h3>
              <p>{entry.excerpt}</p>
              <p className="knowledge-source">
                来源：{entry.sourceType}
                {entry.sourceRef ? ` · ${entry.sourceRef}` : ""} · {STATUS_LABELS[entry.status]}
              </p>
              <div className="agent-actions">
                {entry.status === "pending_review" ? (
                  <>
                    <button type="button" disabled={busy} onClick={() => void decide(entry.id, "approve")}>通过</button>
                    <button type="button" disabled={busy} onClick={() => void decide(entry.id, "reject")}>驳回</button>
                  </>
                ) : null}
                {entry.status === "approved" ? (
                  <button type="button" disabled={busy} onClick={() => void decide(entry.id, "withdraw")}>撤回</button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
