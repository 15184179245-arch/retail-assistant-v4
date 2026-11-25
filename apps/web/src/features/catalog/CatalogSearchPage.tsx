import { useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { FormEvent } from "react";
import type { Session } from "../../lib/auth/session";
import { authorizedRequest, getScopeVersion, subscribe } from "../../lib/auth/session";
import { getCapabilityEpoch, hasCapability, subscribeCapabilities, useCapabilities } from "../../lib/auth/capabilities";
import { PageHeader } from "../../components/PageHeader";
import { StatusBadge } from "../../components/StatusBadge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { ErrorPanel } from "../../components/ErrorPanel";
import { EmptyState } from "../../components/EmptyState";

export interface SearchMatch {
  product_id: string;
  external_code: string;
  match_field: "code" | "barcode" | "alias" | "name" | "derived";
  match_type: "exact_code" | "exact_barcode" | "normalized_barcode" | "alias_exact" | "fuzzy_candidate";
  original_value: string;
  normalized_value: string | null;
  name: string | null;
  spec: string | null;
  unit: string | null;
  barcode: string | null;
  source_system: string;
  source_version: string;
  source_updated_at: string | null;
  captured_at: string;
}

export interface SearchResultData {
  query: string;
  exact: SearchMatch[];
  candidates: SearchMatch[];
  ambiguous: boolean;
  message: string;
  exact_total: number;
  candidate_total: number;
  has_more: boolean;
  scan_truncated: boolean;
  truncated: boolean;
}

export interface CatalogSearchPageProps {
  session?: Session;
  baseUrl?: string;
}

const QUICK_SEARCH_EXAMPLES = ["水管", "FSJB", "DEMO-A01", "生料带", "球阀", "螺丝"];

type ProductMatches = { product: SearchMatch; reasons: Map<string, SearchMatch> };

function groupSearchProducts(result: SearchResultData | null) {
  const exact: ProductMatches[] = [];
  const candidates: ProductMatches[] = [];
  const byId = new Map<string, ProductMatches>();
  function collect(matches: SearchMatch[], section: ProductMatches[]) {
    for (const item of matches) {
      let group = byId.get(item.product_id);
      if (!group) {
        group = { product: item, reasons: new Map() };
        byId.set(item.product_id, group);
        section.push(group);
      }
      const reasonKey = JSON.stringify([item.match_field, item.match_type, item.original_value, item.normalized_value]);
      if (!group.reasons.has(reasonKey)) group.reasons.set(reasonKey, item);
    }
  }
  if (result) {
    collect(result.exact, exact);
    collect(result.candidates, candidates);
  }
  return { exact, candidates };
}

const matchFields: Record<SearchMatch["match_field"], string> = {
  code: "编码", barcode: "条码", alias: "副码", name: "名称", derived: "整理线索",
};
const matchTypes: Record<SearchMatch["match_type"], string> = {
  exact_code: "精确命中", exact_barcode: "精确命中", normalized_barcode: "规范化候选", alias_exact: "精确命中", fuzzy_candidate: "模糊候选",
};

export function CatalogSearchPage({ baseUrl = "" }: CatalogSearchPageProps) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const epoch = useSyncExternalStore(subscribeCapabilities, getCapabilityEpoch, getCapabilityEpoch);
  const maySearch = useCapabilities().hasCapability("catalog.read");
  const identity = useMemo(() => ({ scope, baseUrl }), [scope, baseUrl]);
  const context = useMemo(() => ({ identity, epoch }), [identity, epoch]);
  const [input, setInput] = useState({ identity, text: "" });
  const [status, setStatus] = useState<{ context: typeof context; loading: boolean; error: string | null; result: SearchResultData | null } | null>(null);
  const query = input.identity === identity ? input.text : "";
  const visible = maySearch && status?.context === context ? status : null;
  const { loading = false, error = null, result = null } = visible ?? {};
  const activeContext = useRef<typeof context | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const isCurrentContext = () => activeContext.current === context &&
    getScopeVersion() === scope && getCapabilityEpoch() === epoch && hasCapability("catalog.read");

  useLayoutEffect(() => {
    activeContext.current = context;
    setStatus(null);
    return () => {
      activeContext.current = null;
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [context]);

  function changeQuery(text: string) {
    if (isCurrentContext()) setInput({ identity, text });
  }

  async function handleSearch(searchTerm: string) {
    if (!isCurrentContext()) return;
    activeRequest.current?.abort();
    activeRequest.current = null;
    const trimmed = searchTerm.trim();
    if (!trimmed) { setStatus(null); return; }

    const controller = new AbortController();
    activeRequest.current = controller;
    const isCurrent = () => activeRequest.current === controller && !controller.signal.aborted && isCurrentContext();
    setStatus({ context, loading: true, error: null, result: null });
    try {
      const data = await authorizedRequest<SearchResultData>(
        `/api/v1/search?q=${encodeURIComponent(trimmed)}&limit=50`,
        { baseUrl, signal: controller.signal, cache: "no-store" },
      );
      if (isCurrent()) setStatus({ context, loading: false, error: null, result: data });
    } catch (err) {
      if (!isCurrent()) return;
      const error = err instanceof Error ? err.message : "搜索请求失败，请稍后重试";
      setStatus({ context, loading: false, error, result: null });
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void handleSearch(query);
  }

  function onQuickClick(example: string) {
    changeQuery(example);
    void handleSearch(example);
  }

  const { exact, candidates } = useMemo(() => groupSearchProducts(result), [result]);
  const hasSearched = result !== null;
  const totalFound = exact.length + candidates.length;

  return (
    <div className="catalog-search-page" style={{ padding: "0 16px 40px", maxWidth: 1100, margin: "0 auto" }}>
      <PageHeader
        title="商品与库存查询"
        eyebrow="商品目录 · 只读查询"
        source="目录查询服务 · 结果以本次回执为准"
      />

      <aside className="catalog-readonly-notice" role="note">
        <strong>只读商品目录</strong>
        <p>当前查询为商品目录快照，不是实时价格或库存；结果不构成正式报价。实时来源是否可用，以具体工作的来源回执为准。</p>
      </aside>
      <Card style={{ marginBottom: 24, padding: "20px 24px" }}>
        <form onSubmit={onSubmit} style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <input
            type="search"
            value={query}
            onChange={(e) => changeQuery(e.target.value)}
            placeholder="输入商品名称、拼音缩写（如 FSJB）、条码或编码..."
            style={{
              flex: 1,
              padding: "10px 14px",
              fontSize: 15,
              borderRadius: 6,
              border: "1px solid var(--border-subtle, #ccc)",
              outline: "none",
            }}
            autoFocus
          />
          <Button type="submit" variant="primary" disabled={loading || !maySearch}>
            {loading ? "检索中..." : "查询"}
          </Button>
        </form>

        <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "#666" }}>
          <span>示例检索词：</span>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {QUICK_SEARCH_EXAMPLES.map((ex) => (
              <button
                key={ex}
                type="button"
                disabled={!maySearch}
                onClick={() => onQuickClick(ex)}
                style={{
                  background: "#f0f4f2",
                  border: "1px solid #d0ded8",
                  borderRadius: 4,
                  padding: "2px 8px",
                  fontSize: 12,
                  cursor: "pointer",
                  color: "#1e3d34",
                }}
              >
                {ex}
              </button>
            ))}
          </div>
        </div>
      </Card>

      {error && (
        <ErrorPanel
          userMessage={error}
          hint="请检查网络连接或尝试重新输入搜索词。"
          onRetry={() => handleSearch(query)}
          style={{ marginBottom: 20 }}
        />
      )}

      {hasSearched && (
        <div style={{ marginBottom: 16, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ fontSize: 14, color: "#444" }}>
            检索词 <strong data-search-result-query>“{result.query}”</strong>：本次显示 {exact.length} 种精确匹配商品，{candidates.length} 种候选商品
            {result.ambiguous && (
              <span style={{ marginLeft: 8, color: "#b45309", fontWeight: 500 }}>
                （命中多个商品，请确认规格与身份）
              </span>
            )}
          </div>
          {result.message && <div style={{ fontSize: 13, color: "#666" }}>{result.message}</div>}
        </div>
      )}

      {hasSearched && totalFound === 0 && (
        <EmptyState
          type="empty"
          title="未找到匹配商品"
          description={`在当前可查询目录中未检索到与 “${result.query}” 匹配的项目。建议尝试其他关键词、拼音首字母或商品编码。`}
        />
      )}

      {/* 精确匹配区域 */}
      {exact.length > 0 && (
        <div style={{ marginBottom: 32 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <h3 style={{ fontSize: 16, margin: 0, color: "#166534" }}>
              精确匹配结果 ({exact.length})
            </h3>
            <StatusBadge kind="verified" />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 320px), 1fr))", gap: 16 }}>
            {exact.map((group) => (
              <ProductCard key={group.product.product_id} group={group} isExact />
            ))}
          </div>
        </div>
      )}

      {/* 模糊/拼音候选区域 */}
      {candidates.length > 0 && (
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <h3 style={{ fontSize: 16, margin: 0, color: "#1e293b" }}>
              候选匹配结果 ({candidates.length})
            </h3>
            <StatusBadge kind="suggestion" />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 320px), 1fr))", gap: 16 }}>
            {candidates.map((group) => (
              <ProductCard key={group.product.product_id} group={group} isExact={false} />
            ))}
          </div>
        </div>
      )}

      {!hasSearched && (
        <Card style={{ padding: "40px 24px", textAlign: "center", color: "#666" }}>
          <h3 style={{ fontSize: 16, color: "#222", marginBottom: 8 }}>连接到管家婆商品只读真实索引</h3>
          <p style={{ maxWidth: 540, margin: "0 auto", fontSize: 14, lineHeight: 1.6 }}>
            查询当前账号可访问的商品目录。输入商品名称、规格、条形码或拼音首字母开始检索。
          </p>
        </Card>
      )}
    </div>
  );
}

function ProductCard({ group: { product: item, reasons }, isExact }: { group: ProductMatches; isExact: boolean }) {
  return (
    <Card
      style={{
        padding: 16,
        minWidth: 0,
        overflowWrap: "anywhere",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        border: isExact ? "1px solid #86efac" : "1px solid var(--border-subtle, #e2e8f0)",
        backgroundColor: isExact ? "#f0fdf4" : "#ffffff",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
        <div style={{ fontWeight: 600, fontSize: 15, color: "#0f172a" }}>
          {item.name || "（未命名商品）"}
        </div>
        <span
          style={{
            fontSize: 11,
            padding: "2px 6px",
            borderRadius: 4,
            backgroundColor: isExact ? "#bbf7d0" : "#e2e8f0",
            color: isExact ? "#15803d" : "#475569",
            whiteSpace: "nowrap",
          }}
        >
          {isExact ? "精确" : "候选"}
        </span>
      </div>

      <div style={{ fontSize: 13, color: "#475569", display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 130px), 1fr))", gap: "4px 8px" }}>
        <div>
          <span style={{ color: "#94a3b8" }}>规格：</span>
          {item.spec || "—"}
        </div>
        <div>
          <span style={{ color: "#94a3b8" }}>单位：</span>
          {item.unit || "—"}
        </div>
        <div>
          <span style={{ color: "#94a3b8" }}>编码：</span>
          <code>{item.external_code}</code>
        </div>
        <div>
          <span style={{ color: "#94a3b8" }}>条码：</span>
          {item.barcode ? <code>{item.barcode}</code> : "—"}
        </div>
      </div>

      <div
        style={{
          marginTop: 6,
          paddingTop: 8,
          borderTop: "1px dashed #e2e8f0",
          fontSize: 11,
          color: "#94a3b8",
          display: "flex",
          flexDirection: "column",
          gap: 4,
        }}
      >
        <ul aria-label="匹配依据" style={{ margin: 0, paddingInlineStart: 16 }}>
          {[...reasons].map(([key, reason]) => (
            <li key={key}>
              {matchFields[reason.match_field]} · {matchTypes[reason.match_type]}：<code>{reason.original_value}</code>
              {reason.normalized_value && reason.normalized_value !== reason.original_value ? (
                <> → <code>{reason.normalized_value}</code></>
              ) : null}
            </li>
          ))}
        </ul>
        <span>目录来源：{item.source_system || "未提供"}</span>
        <span>来源版本：{item.source_version || "未提供"}</span>
        <span>采集时点：{formatSourceTime(item.captured_at)}</span>
        <span>源更新时间：{formatSourceTime(item.source_updated_at)}</span>
      </div>
    </Card>
  );
}


function formatSourceTime(value: string | null): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "未提供";
  return new Date(value).toLocaleString(undefined, { hour12: false });
}
