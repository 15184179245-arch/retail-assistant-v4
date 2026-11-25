import { useEffect, useState, useSyncExternalStore } from "react";
import type { Session } from "../../lib/auth/session";
import { createSalesReadonlyController } from "./readonlySales";
import type { SalesReadonlyState } from "./readonlySales";

interface ViewProps {
  session: Session;
  state: SalesReadonlyState;
  onRefresh: () => void;
  onSelect: (id: string) => void;
  onClose: () => void;
}

/** The same read-only view is consumed by the mounted page and its rendering test. */
export function SalesWorkbenchView({ session, state, onRefresh, onSelect, onClose }: ViewProps) {
  const detail = state.detail;
  return (
    <section aria-labelledby="sales-review-title">
      <h2 id="sales-review-title">销售草稿与交接复核（只读）</h2>
      <p>组织：{session.organizationId} · 门店：{session.storeId}</p>
      <p>本页只读取服务端草稿，不修改、交接或确认收款。交接确认不等于已收款，外部引用不代表回执已核验。</p>
      <button type="button" onClick={onRefresh} disabled={state.listStatus === "loading"}>刷新列表</button>
      {state.access === "forbidden" ? (
        <p role="alert">当前身份不可读取销售草稿（需要 sales.read）。旧数据已清除。</p>
      ) : (
        <>
          <p>最新至多 100 条；不代表全部草稿，无总数或分页完整性保证。</p>
          {state.listStatus === "idle" && <p>尚未加载，请刷新列表。</p>}
          {state.listStatus === "loading" && <p role="status">正在读取草稿…</p>}
          {state.listError && <p role="alert">{state.listError}</p>}
          {state.listStatus === "ready" && (
            <>
              <p>本次返回 {state.drafts.length} 条。</p>
              {state.drafts.length === 0 && <p>本次返回列表为空。</p>}
              <ul aria-label="销售草稿列表">
                {state.drafts.map((draft) => (
                  <li key={draft.id}>
                    <button type="button" onClick={() => onSelect(draft.id)} aria-pressed={state.selectedId === draft.id}>
                      草稿 {draft.id} · 版本 {draft.version}
                    </button>
                    <span> · 阶段 {draft.stage} · 业务状态 {draft.status} · 同步 {draft.sync_state}</span>
                    <span> · 服务端金额 {draft.total_amount} {draft.currency} · 更新 {draft.updated_at}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {state.selectedId !== null && (
            <section aria-labelledby="sales-detail-title">
              <h3 id="sales-detail-title">草稿详情：{state.selectedId}</h3>
              <button type="button" onClick={onClose}>关闭详情</button>
              {state.detailStatus === "loading" && <p role="status">正在读取详情…</p>}
              {state.detailError && <p role="alert">{state.detailError}</p>}
              {detail && state.detailStatus === "ready" && (
                <>
                  <dl>
                    <dt>草稿 ID / 版本</dt><dd>{detail.id} / {detail.version}</dd>
                    <dt>阶段 / 业务状态 / 同步状态</dt><dd>{detail.stage} / {detail.status} / {detail.sync_state}</dd>
                    <dt>服务端金额（未重算）</dt><dd>{detail.total_amount} {detail.currency}</dd>
                    <dt>报价 ID</dt><dd>{detail.quote_id ?? "未提供"}</dd>
                    <dt>作废时间</dt><dd>{detail.voided_at ?? "未提供"}</dd>
                    <dt>有效期</dt><dd>{detail.expires_at ?? "未提供"}</dd>
                    <dt>创建 / 更新</dt><dd>{detail.created_at} / {detail.updated_at}</dd>
                  </dl>
                  <h4>商品行（数量与金额按服务端原字符串显示）</h4>
                  {detail.lines.length === 0 && <p>返回数据未列商品行。</p>}
                  <ul>
                    {detail.lines.map((line) => (
                      <li key={line.id}>
                        <p>{line.product_name} · 商品 {line.product_id} · 行 {line.id} / {line.line_key}</p>
                        <p>数量 {line.quantity} · 单价 {line.unit_price} · 折扣基点 {line.discount_bps} · 行金额 {line.line_amount}</p>
                        <p>报价 {line.quote_id ?? "未提供"} · 确认人 {line.confirmed_by ?? "未提供"} · 确认时间 {line.confirmed_at ?? "未提供"}</p>
                      </li>
                    ))}
                  </ul>
                  <h4>交接记录（不等于付款或外部回执核验）</h4>
                  {detail.handoffs.length === 0 && <p>返回数据未列交接记录。</p>}
                  <ul>
                    {detail.handoffs.map((handoff) => (
                      <li key={handoff.id}>
                        <p>交接 {handoff.id} · 交接状态 {handoff.handoff_state}</p>
                        <p>外部引用（未核验）{handoff.external_ref ?? "未提供"}</p>
                        <p>创建 {handoff.created_at} · 更新 {handoff.updated_at}</p>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}

export function SalesWorkbenchPage({ session }: { session: Session }) {
  const [controller] = useState(createSalesReadonlyController);
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  useEffect(() => {
    controller.start();
    void controller.refresh();
    return () => controller.stop();
  }, [controller]);
  return <SalesWorkbenchView session={session} state={state}
    onRefresh={() => { void controller.refresh(); }}
    onSelect={(id) => { void controller.selectDraft(id); }}
    onClose={controller.closeDetail} />;
}
