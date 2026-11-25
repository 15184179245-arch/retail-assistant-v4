import { useState } from "react";
import type { FormEvent } from "react";
import type { Session } from "../../lib/auth/session";
import { authorizedRequest } from "../../lib/auth/session";
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

const QUICK_SEARCH_EXAMPLES = ["水管", "JDD", "DEMO-A01", "生料带", "球阀", "螺丝"];

export function CatalogSearchPage({ baseUrl = "" }: CatalogSearchPageProps) {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SearchResultData | null>(null);

  async function handleSearch(searchTerm: string) {
    const trimmed = searchTerm.trim();
    if (!trimmed) {
      setResult(null);
      setError(null);
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const data = await authorizedRequest<SearchResultData>(
        `/api/v1/search?q=${encodeURIComponent(trimmed)}&limit=50`,
        { baseUrl }
      );
      setResult(data);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "搜索请求失败，请稍后重试";
      setError(msg);
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void handleSearch(query);
  }

  function onQuickClick(example: string) {
    setQuery(example);
    void handleSearch(example);
  }

  const hasSearched = result !== null;
  const totalFound = (result?.exact.length ?? 0) + (result?.candidates.length ?? 0);

  return (
    <div className="catalog-search-page" style={{ padding: "0 16px 40px", maxWidth: 1100, margin: "0 auto" }}>
      <PageHeader
        title="商品与库存查询"
        eyebrow="商品目录 · 只读检索"
        source="公开版不附带商品库"
        statusKind="verified"
      />

      <Card style={{ marginBottom: 24, padding: "20px 24px" }}>
        <form onSubmit={onSubmit} style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入商品名称、拼音缩写（如 JDD）、条码或编码..."
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
          <Button type="submit" variant="primary" disabled={loading}>
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
            检索词 <strong>“{result.query}”</strong>：找到 {result.exact.length} 项精确匹配，{result.candidates.length} 项候选
            {result.ambiguous && (
              <span style={{ marginLeft: 8, color: "#b45309", fontWeight: 500 }}>
                (包含模糊/拼音候选，规格与身份请确认)
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
          description={`在当前可访问的商品目录中未检索到与 “${result.query}” 匹配的项目。建议尝试其他关键词、拼音首字母或商品编码。`}
        />
      )}

      {/* 精确匹配区域 */}
      {result && result.exact.length > 0 && (
        <div style={{ marginBottom: 32 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <h3 style={{ fontSize: 16, margin: 0, color: "#166534" }}>
              精确匹配结果 ({result.exact.length})
            </h3>
            <StatusBadge kind="verified" />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
            {result.exact.map((item) => (
              <ProductCard key={item.product_id} item={item} isExact />
            ))}
          </div>
        </div>
      )}

      {/* 模糊/拼音候选区域 */}
      {result && result.candidates.length > 0 && (
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
            <h3 style={{ fontSize: 16, margin: 0, color: "#1e293b" }}>
              候选匹配结果 ({result.candidates.length})
            </h3>
            <StatusBadge kind="suggestion" />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16 }}>
            {result.candidates.map((item) => (
              <ProductCard key={item.product_id} item={item} isExact={false} />
            ))}
          </div>
        </div>
      )}

      {!hasSearched && (
        <Card style={{ padding: "40px 24px", textAlign: "center", color: "#666" }}>
          <h3 style={{ fontSize: 16, color: "#222", marginBottom: 8 }}>商品目录与只读索引</h3>
          <p style={{ maxWidth: 540, margin: "0 auto", fontSize: 14, lineHeight: 1.6 }}>
            当前公开版不内置业务快照。输入商品名称、规格、条形码或拼音首字母即可毫秒级检索。
          </p>
        </Card>
      )}
    </div>
  );
}

function ProductCard({ item, isExact }: { item: SearchMatch; isExact: boolean }) {
  return (
    <Card
      style={{
        padding: 16,
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

      <div style={{ fontSize: 13, color: "#475569", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 8px" }}>
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
          justifyContent: "space-between",
        }}
      >
        <span>来源: {item.source_system}</span>
        <span>快照: {new Date(item.captured_at).toLocaleDateString()}</span>
      </div>
    </Card>
  );
}
