import { useEffect, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion } from "../../lib/auth/session";
import type { Session } from "../../lib/auth/session";

type Confidence =
  | "partial_reviewed"
  | "partial_unreviewed"
  | "unknown_coverage"
  | "insufficient_evidence";

interface SourceRef {
  id: string;
  event_seq: number;
  source_observation_ids: string[];
  recorded_at: string;
  conversion_receipt_ref: string | null;
  conversion_status: string;
  source_url: string;
}

interface EvidenceMetric {
  metric_id: string;
  metric_version: string;
  state: "candidate_only";
  source_verified: false;
  publishable: false;
  scope_kind: "partial_area" | "partial_shelf" | "unknown";
  observation_leaf_count: number;
  unknown_identity_count: number;
  unverified_conversion_count: number;
  missing_evidence_count: number;
  missing_evidence_rate: string | null;
  missing_evidence_rate_reason: string | null;
  manual_review_current: boolean;
  full_store_coverage: false;
  inventory_accuracy: null;
  book_quantity: null;
  confidence: Confidence;
  warnings: string[];
}

export interface AnalyticsSnapshotItem {
  id: string;
  version: number;
  metric_id: string;
  metric_version: string;
  formula_version: string;
  formula_digest: string;
  source_digest: string;
  result_digest: string;
  organization_id: string;
  store_id: string;
  session_id: string;
  snapshot_at: string;
  source_last_recorded_at: string | null;
  source_session_version: number;
  fresh: boolean;
  stale_reasons: string[];
  confidence: Confidence;
  coverage_kind: "partial_area" | "partial_shelf" | "unknown";
  warnings: string[];
  metric: EvidenceMetric;
  sources: SourceRef[];
  publishable: false;
  source_verified: false;
  book_quantity: null;
  inventory_accuracy: null;
  full_store_coverage: false;
  action: "manual_review_only";
}

interface Page {
  items: AnalyticsSnapshotItem[];
  count: number;
}

const warningLabels: Record<string, string> = {
  partial_coverage_not_whole_store: "仅覆盖部分区域/货架，不能代表全店库存",
  coverage_unknown: "盘点覆盖范围未知",
  no_population: "没有足够的观察来源",
  unknown_product_or_location: "部分商品或库位未能确认",
  conversion_not_verified: "部分包装换算缺乏核验凭证",
  manual_review_not_current: "人工复核未完成或已失效",
  source_changed: "源观察或人工复核已变化，需重算",
  snapshot_expired: "快照超过24小时，已过期",
};
const confidenceLabels: Record<Confidence, string> = {
  partial_reviewed: "局部来源已复核（不是全店库存）",
  partial_unreviewed: "局部证据待复核",
  unknown_coverage: "覆盖范围未知",
  insufficient_evidence: "来源不足",
};

export function AnalyticsPage({
  session,
  initialItems,
}: {
  session: Session;
  initialItems?: AnalyticsSnapshotItem[];
}) {
  const [items, setItems] = useState<AnalyticsSnapshotItem[]>(initialItems ?? []);
  const [loading, setLoading] = useState(initialItems === undefined);
  const [message, setMessage] = useState<string | null>(null);

  const reload = () => {
    const version = getScopeVersion();
    setLoading(true);
    setMessage(null);
    void authorizedRequest<Page>("/api/v1/analytics/snapshots?limit=20")
      .then((response) => {
        if (getScopeVersion() !== version) return;
        if (!response || !Array.isArray(response.items)) {
          throw new Error("快照响应格式无效");
        }
        setItems(response.items.filter((row) =>
          row.organization_id === session.organizationId &&
          row.store_id === session.storeId));
      })
      .catch((error: unknown) => {
        if (getScopeVersion() !== version) return;
        setItems([]);
        setMessage(error instanceof ApiError
          ? error.userMessage : "无法读取可信度快照，请检查权限与来源。");
      })
      .finally(() => {
        if (getScopeVersion() === version) setLoading(false);
      });
  };

  useEffect(() => {
    if (initialItems === undefined) {
      reload();
    }
    // The auth store aborts outstanding requests on identity change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.subjectId, session.storeId, session.organizationId]);

  return (
    <section aria-labelledby="analytics-heading" style={{ paddingBottom: 24 }}>
      <h2 id="analytics-heading">库存证据可信度与指标快照</h2>
      <p>
        这里只展示局部盘点的候选快照和来源。无账面库存数据，
        不能据此自动采购、调库存、改价或判断全店准确率。
      </p>
      <button type="button" onClick={reload} disabled={loading}>
        重新读取快照
      </button>
      {loading ? <p role="status">正在读取已保存的快照…</p> : null}
      {message ? <p role="alert">{message}</p> : null}
      {!loading && !message && items.length === 0 ?
        <p>当前门店暂无已保存的指标快照；没有数据不代表库存为零。</p> : null}
      {items.map((item) => (
        <article key={item.id} style={{
          marginTop: 18, padding: 18, border: "1px solid #dbe5e0",
          borderRadius: 12, background: "white",
        }}>
          <h3>{confidenceLabels[item.confidence]}</h3>
          <p>
            {item.coverage_kind === "partial_shelf" ? "局部货架" :
              item.coverage_kind === "partial_area" ? "局部区域" : "覆盖未知"}
            {" · "}
            {item.fresh ? "来源未检测到变化" : "快照过期或来源已变化"}
          </p>
          <p>快照：{item.snapshot_at} · 源最近记录：{
            item.source_last_recorded_at ?? "未知"}</p>
          <p>
            观察叶数量：{item.metric.observation_leaf_count} ·
            来源缺失数量：{item.metric.missing_evidence_count} ·
            缺失率：{item.metric.missing_evidence_rate === null ?
              "不可计算" : item.metric.missing_evidence_rate}
          </p>
          <p>
            账面数量：未提供 · 全店覆盖：未确认 · 库存准确率：不可计算
          </p>
          {item.warnings.length > 0 ? (
            <div aria-label="证据警告">
              <strong>需要人工复核：</strong>
              <ul>{item.warnings.map((w) =>
                <li key={w}>{warningLabels[w] ?? "存在未核验来源"}</li>)}</ul>
            </div>
          ) : null}
          <details>
            <summary>来源、口径和版本（可复核）</summary>
            <p>指标：{item.metric_id} / {item.metric_version}</p>
            <p>公式：{item.formula_version}（{item.formula_digest}）</p>
            <p>来源摘要：{item.source_digest}</p>
            <p>结果摘要：{item.result_digest}</p>
            <p>盘点会话：{item.session_id}（第 {item.source_session_version} 版）</p>
            <ul>{item.sources.map((ref) => (
              <li key={ref.id}>
                观察事件 {ref.id}（序号 {ref.event_seq}）
                {" · "}{ref.conversion_status}
                {" · "}<a href={ref.source_url}>查看原始盘点证据（需独立授权）</a>
              </li>
            ))}</ul>
          </details>
          <p style={{ fontSize: 12, color: "#67766e" }}>
            本页不会发送任何正式库存、采购或价格写操作。
          </p>
        </article>
      ))}
    </section>
  );
}
