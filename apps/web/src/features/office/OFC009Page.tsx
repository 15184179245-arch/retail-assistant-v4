import { useState } from "react";
import type { Session } from "../../lib/auth/session";

export interface ReviewItem {
  id: string;
  organization_id: string;
  store_id: string;
  target_kind: string;
  target_id: string;
  target_version: number;
  title: string;
  summary: string;
  risk_level: "low" | "medium" | "high" | "critical";
  diff_payload: Record<string, any>;
  state: "pending" | "claimed" | "approved" | "rejected" | "modified" | "escalated";
  claimed_by: string | null;
  claimed_at: string | null;
  lock_token: string | null;
  lock_expires_at: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_decision: string | null;
  review_notes: string | null;
  modifications: Record<string, any> | null;
  receipt_digest: string | null;
  created_at: string;
  updated_at: string;
}

export function bindItemVersion(item: ReviewItem): { item_id: string; expected_target_version: number } {
  return {
    item_id: item.id,
    expected_target_version: item.target_version,
  };
}

export function isLockedByOther(item: ReviewItem, currentSubjectId: string): boolean {
  if (item.state !== "claimed" || !item.claimed_by) return false;
  if (item.claimed_by === currentSubjectId) return false;
  if (!item.lock_expires_at) return false;
  return new Date(item.lock_expires_at).getTime() > Date.now();
}

export function calculateBatchRisks(items: ReviewItem[]): {
  total: number;
  criticalCount: number;
  highCount: number;
  allowBatch: boolean;
} {
  const criticalCount = items.filter((i) => i.risk_level === "critical").length;
  const highCount = items.filter((i) => i.risk_level === "high").length;
  // Critical risk items require individual review, batch not permitted
  return {
    total: items.length,
    criticalCount,
    highCount,
    allowBatch: criticalCount === 0,
  };
}

interface OFC009PageProps {
  session: Session | null;
  initialItems?: ReviewItem[];
}

export function OFC009Page({ session, initialItems = [] }: OFC009PageProps) {
  const [items, setItems] = useState<ReviewItem[]>(initialItems);
  const [selectedId, setSelectedId] = useState<string | null>(initialItems[0]?.id ?? null);
  const [reason, setReason] = useState("");
  const [modificationText, setModificationText] = useState("");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  if (!session) {
    return (
      <div className="p-6 text-center text-gray-500">
        <h2 className="text-xl font-semibold mb-2">需要登录</h2>
        <p>请登录后访问人工审核与办公交付工作台。</p>
      </div>
    );
  }

  const selectedItem = items.find((i) => i.id === selectedId);

  const handleClaim = (item: ReviewItem) => {
    if (isLockedByOther(item, session.subjectId)) {
      setErrorMessage(`该项目已被审核人 ${item.claimed_by} 锁定`);
      return;
    }
    const updated = items.map((i) =>
      i.id === item.id
        ? {
            ...i,
            state: "claimed" as const,
            claimed_by: session.subjectId,
            claimed_at: new Date().toISOString(),
            lock_token: "local-token-" + item.id,
            lock_expires_at: new Date(Date.now() + 300000).toISOString(),
          }
        : i
    );
    setItems(updated);
    setStatusMessage(`已成功认领审核项: ${item.title}`);
    setErrorMessage(null);
  };

  const handleDecide = (decision: "approved" | "rejected" | "modified" | "escalated") => {
    if (!selectedItem) return;
    if (isLockedByOther(selectedItem, session.subjectId)) {
      setErrorMessage("该项目被他人锁定，无法提交决策");
      return;
    }
    if (!reason.trim()) {
      setErrorMessage("请填写审核意见或理由");
      return;
    }

    let parsedModifications: Record<string, any> | null = null;
    if (decision === "modified") {
      try {
        parsedModifications = modificationText ? JSON.parse(modificationText) : {};
      } catch {
        setErrorMessage("修正内容格式错误，需为合法的 JSON 对象");
        return;
      }
    }

    const updated = items.map((i) =>
      i.id === selectedItem.id
        ? {
            ...i,
            state: decision,
            reviewed_by: session.subjectId,
            reviewed_at: new Date().toISOString(),
            review_decision: decision,
            review_notes: reason,
            modifications: parsedModifications,
            claimed_by: null,
            lock_token: null,
            lock_expires_at: null,
          }
        : i
    );
    setItems(updated);
    setStatusMessage(`决策已提交: ${decision}`);
    setErrorMessage(null);
    setReason("");
    setModificationText("");
  };

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6">
      <div className="flex justify-between items-center border-b pb-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">人工审核与办公交付工作台 (OFC-009)</h1>
          <p className="text-sm text-gray-500">统一文职审核待办、前后差异核对、字段人工修正与不可变回执</p>
        </div>
        <div className="text-sm text-gray-600">
          <span>登录员工: {session.username}</span> | <span>门店: {session.storeId}</span>
        </div>
      </div>

      {statusMessage && (
        <div role="status" className="p-4 bg-green-50 border border-green-200 text-green-800 rounded">
          {statusMessage}
        </div>
      )}

      {errorMessage && (
        <div role="alert" className="p-4 bg-red-50 border border-red-200 text-red-800 rounded">
          {errorMessage}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Backlog Items List */}
        <div className="border rounded-lg p-4 bg-white shadow-sm space-y-3">
          <div className="flex justify-between items-center mb-2">
            <h2 className="font-semibold text-lg">待审队列 ({items.length})</h2>
            <button
              onClick={() => setStatusMessage("导出任务已发起")}
              className="text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 px-2 py-1 rounded border"
            >
              导出报表
            </button>
          </div>

          <div className="space-y-2 max-h-[600px] overflow-y-auto">
            {items.map((item) => {
              const locked = isLockedByOther(item, session.subjectId);
              return (
                <div
                  key={item.id}
                  onClick={() => setSelectedId(item.id)}
                  className={`p-3 rounded border cursor-pointer transition ${
                    selectedId === item.id ? "border-blue-500 bg-blue-50" : "hover:bg-gray-50"
                  }`}
                >
                  <div className="flex justify-between items-start">
                    <span className="font-medium text-sm text-gray-900">{item.title}</span>
                    <span
                      className={`text-xs px-2 py-0.5 rounded ${
                        item.risk_level === "critical"
                          ? "bg-red-100 text-red-800"
                          : item.risk_level === "high"
                          ? "bg-orange-100 text-orange-800"
                          : "bg-gray-100 text-gray-700"
                      }`}
                    >
                      {item.risk_level}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 mt-1 line-clamp-2">{item.summary}</p>
                  <div className="flex justify-between items-center mt-2 text-xs">
                    <span className="text-gray-400">版本: v{item.target_version}</span>
                    <span
                      className={`font-semibold ${
                        item.state === "approved"
                          ? "text-green-600"
                          : item.state === "claimed"
                          ? "text-blue-600"
                          : "text-gray-600"
                      }`}
                    >
                      {item.state}
                    </span>
                  </div>
                  {locked && (
                    <div className="text-xs text-red-600 mt-1 font-medium">已被 {item.claimed_by} 锁定</div>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Selected Item Detail & Diff Viewer */}
        <div className="md:col-span-2 border rounded-lg p-6 bg-white shadow-sm space-y-6">
          {selectedItem ? (
            <>
              <div className="border-b pb-4 flex justify-between items-start">
                <div>
                  <h2 className="text-xl font-bold text-gray-900">{selectedItem.title}</h2>
                  <p className="text-sm text-gray-500 mt-1">{selectedItem.summary}</p>
                  <div className="flex gap-4 mt-2 text-xs text-gray-500">
                    <span>目标类型: {selectedItem.target_kind}</span>
                    <span>目标ID: {selectedItem.target_id}</span>
                    <span>目标版本: v{selectedItem.target_version}</span>
                  </div>
                </div>
                {selectedItem.state === "pending" && (
                  <button
                    onClick={() => handleClaim(selectedItem)}
                    className="bg-blue-600 hover:bg-blue-700 text-white text-sm px-4 py-2 rounded shadow-sm"
                  >
                    认领审核
                  </button>
                )}
              </div>

              {/* Side-by-side / Diff section */}
              <div>
                <h3 className="font-semibold text-md mb-2">前后差异与置信度</h3>
                <div className="bg-gray-50 p-4 rounded border font-mono text-xs overflow-x-auto">
                  <pre>{JSON.stringify(selectedItem.diff_payload, null, 2)}</pre>
                </div>
              </div>

              {/* Human field modification (takeover) */}
              <div className="space-y-2">
                <label className="block text-sm font-medium text-gray-700">
                  人工字段修正 (JSON 格式，仅在选择“修正批准”时生效)
                </label>
                <textarea
                  value={modificationText}
                  onChange={(e) => setModificationText(e.target.value)}
                  placeholder='{"field_name": "修正后的值"}'
                  rows={3}
                  className="w-full border rounded p-2 text-xs font-mono"
                />
              </div>

              {/* Review notes and action bar */}
              <div className="space-y-3">
                <label className="block text-sm font-medium text-gray-700">审核意见 / 决策理由</label>
                <input
                  type="text"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="请输入批准或拒绝的具体依据..."
                  className="w-full border rounded p-2 text-sm"
                />

                <div className="flex gap-3 pt-2">
                  <button
                    onClick={() => handleDecide("approved")}
                    className="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded text-sm font-medium"
                  >
                    批准
                  </button>
                  <button
                    onClick={() => handleDecide("modified")}
                    className="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded text-sm font-medium"
                  >
                    修正并批准
                  </button>
                  <button
                    onClick={() => handleDecide("rejected")}
                    className="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded text-sm font-medium"
                  >
                    拒绝
                  </button>
                  <button
                    onClick={() => handleDecide("escalated")}
                    className="bg-amber-600 hover:bg-amber-700 text-white px-4 py-2 rounded text-sm font-medium"
                  >
                    升级呈报
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="text-center py-20 text-gray-400">请选择左侧列表中的待审项目</div>
          )}
        </div>
      </div>
    </div>
  );
}
