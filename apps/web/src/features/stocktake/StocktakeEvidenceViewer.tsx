import { useEffect, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { getCapabilityEpoch, hasCapability } from "../../lib/auth/capabilities";
import { businessPermission } from "../../lib/auth/businessPermissions";
import { getScopeVersion } from "../../lib/auth/session";
import { readStocktakeObservation, type ObservationSource } from "./observations";

type Props = { scope: number; epoch: number; sessionId: string; observationId: string; sequence: number };
type View = { kind: "loading" | "missing" | "denied" | "error" } | { kind: "ready"; row: ObservationSource };

/** This panel is mounted only by an explicit click on a snapshot's source. */
export function StocktakeEvidenceViewer({ scope, epoch, sessionId, observationId, sequence }: Props) {
  const [view, setView] = useState<View>({ kind: "loading" });
  const canRead = () => getScopeVersion() === scope && getCapabilityEpoch() === epoch &&
    hasCapability("analytics.read") && hasCapability(businessPermission.stocktake.read);

  useEffect(() => {
    if (!canRead()) return;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && canRead();
    void readStocktakeObservation(sessionId, observationId, sequence, controller.signal)
      .then(row => {
        if (current()) setView(row ? { kind: "ready", row } : { kind: "missing" });
      })
      .catch((error: unknown) => {
        if (!current()) return;
        const status = error instanceof ApiError ? error.status : 0;
        setView({ kind: status === 401 || status === 403 ? "denied" : status === 404 ? "missing" : "error" });
      });
    return () => controller.abort();
  }, [scope, epoch, sessionId, observationId, sequence]);

  if (!canRead() || view.kind === "denied") return <p role="status">当前未获盘点证据读取权限。</p>;
  if (view.kind === "loading") return <p role="status">正在核对指定的原始盘点证据…</p>;
  if (view.kind === "missing") return <p role="status">未找到与该快照的会话、证据 ID 和序号都匹配的原始观察。这不代表库存为零。</p>;
  if (view.kind !== "ready") return <p role="alert">无法核对指定原始观察。可以收起后重新打开。</p>;
  const row = view.row;
  return <section aria-label="原始盘点证据">
    <p>观察 {row.id} · 序号 {row.event_seq} · {row.kind === "correction" ? "更正记录" : "现场观察"}</p>
    <p>原始现场数量：{row.quantity} {row.unit}（不是账面库存）</p>
    <p>观察于：{row.observed_at} · 记录于：{row.recorded_at}</p>
  </section>;
}
