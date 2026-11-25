import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { authorizedRequest, getScopeVersion } from "../../lib/auth/session";
import { hasCapability, useCapabilities } from "../../lib/auth/capabilities";
import { businessPermission } from "../../lib/auth/businessPermissions";
import type { Session } from "../../lib/auth/session";
import { readStocktakeObservationPage, type ObservationSource } from "./observations";

export type { ObservationSource } from "./observations";

type Coverage = "partial_area" | "partial_shelf" | "unknown";
export type ReviewDecision = "accepted_partial" | "needs_recount";

export interface StocktakeSessionRow {
  id: string;
  coverage_kind: Coverage;
  coverage_location_id: string | null;
  coverage_note: string;
  status: string;
  version: number;
  opened_by: string;
  created_at: string;
}

export interface StocktakeReviewLine {
  observation_id: string;
  event_seq: number;
  source_observation_ids: string[];
  product_id: string | null;
  unknown_product_label: string | null;
  location_id: string | null;
  unknown_location_label: string | null;
  quantity: string;
  unit: string;
  observed_at: string;
  recorded_at: string;
  normalized_piece_quantity: string | null;
  conversion_receipt_ref: string | null;
  conversion_status: string;
  observed_by: string;
}
interface ConversionReceipt {
  receipt_ref: string;
  product_id: string;
  factor: string;
  evidence_media_id: string;
  status: string;
  session_version: number;
  effective_from: string;
  expires_at: string;
  verified_at: string;
  verified_by: string;
  ledger_write: boolean;
}
interface ReviewReceipt {
  receipt_ref: string;
  session_version: number;
  decision: ReviewDecision;
  reason: string;
  observation_ids: string[];
  reviewer_id: string;
  reviewed_at: string;
  full_store_coverage: boolean;
  stock_adjustment: null;
  ledger_write: boolean;
}
interface Group {
  product_id: string;
  location_id: string;
  normalized_piece_quantity: string | null;
  unit: string | null;
  conversion_complete: boolean;
  observation_ids: string[];
  incomplete_reasons: string[];
}
export interface ReviewPreview {
  session_id: string;
  session_version: number;
  coverage_kind: Coverage;
  coverage_location_id: string | null;
  coverage_note: string;
  full_store_coverage: boolean;
  difference_status: string;
  book_quantity: null;
  book_difference: null;
  inventory_accuracy: null;
  preview_as_of: string;
  observation_count: number;
  observations: StocktakeReviewLine[];
  groups: Group[];
  conversion_receipts: ConversionReceipt[];
  review_receipts: ReviewReceipt[];
  reviewer_id: string | null;
  review_current: boolean;
  disclaimer: string;
}
export interface BlindView {
  blind: boolean;
  session_id: string;
  session_version: number;
  full_store_coverage: boolean;
  book_quantity: null;
  book_difference: null;
}
export function validateReviewSnapshot(
  sessionId: string, preview: ReviewPreview, blind: BlindView,
  session: StocktakeSessionRow, observations: ObservationSource[],
): boolean {
  return preview.session_id === sessionId && blind.session_id === sessionId &&
    session.id === sessionId && session.version === preview.session_version &&
    blind.session_version === preview.session_version &&
    blind.blind === true && blind.full_store_coverage === false &&
    blind.book_quantity === null && blind.book_difference === null &&
    observations.every(source => source.session_id === sessionId) &&
    preview.full_store_coverage === false && preview.book_quantity === null &&
    preview.book_difference === null && preview.inventory_accuracy === null &&
    preview.difference_status === "authoritative_book_balance_unavailable" &&
    (preview.observation_count === 0 ||
     evaluateReviewSafety(preview, observations, true).canRecount);
}
export interface ReviewSafety {
  acceptedPartialAllowed: boolean;
  canRecount: boolean;
  reason: string;
  leafIds: string[];
}

export function evaluateReviewSafety(
  preview: ReviewPreview,
  sources: ObservationSource[],
  sourcesComplete: boolean,
): ReviewSafety {
  const leaves = preview.observations.map(item => item.observation_id);
  const sourceMap = new Map(sources.map(item => [item.id, item]));
  const unique = new Set(leaves);
  const chainLoaded = preview.observations.every(item =>
    item.source_observation_ids.length > 0 &&
    item.source_observation_ids.every(id => sourceMap.has(id))
  );
  const validPreview = preview.full_store_coverage === false &&
    preview.book_quantity === null && preview.book_difference === null &&
    preview.inventory_accuracy === null &&
    preview.difference_status === "authoritative_book_balance_unavailable" &&
    preview.observation_count === leaves.length &&
    unique.size === leaves.length &&
    Number.isInteger(preview.session_version) && preview.session_version > 0 &&
    preview.conversion_receipts.every(item => item.ledger_write === false) &&
    preview.review_receipts.every(item =>
      item.ledger_write === false && item.stock_adjustment === null &&
      item.full_store_coverage === false);
  if (!validPreview || !sourcesComplete || !chainLoaded) {
    return { acceptedPartialAllowed: false, canRecount: false,
      reason: "证据未完整加载、版本无效或预览不符合局部盘点契约。", leafIds: [] };
  }
  if (leaves.length === 0) {
    return { acceptedPartialAllowed: false, canRecount: false,
      reason: "还没有观察记录，不可提交复核。", leafIds: [] };
  }
  const conversionMissing = preview.observations.some(
    item => item.normalized_piece_quantity === null);
  const acceptedPartialAllowed = !conversionMissing &&
    preview.coverage_kind !== "unknown" &&
    preview.groups.every(group => group.conversion_complete);
  return {
    acceptedPartialAllowed,
    canRecount: true,
    reason: acceptedPartialAllowed ? "仅确认本次局部观察，不代表账面准确。" :
      "覆盖范围未知或换算证据不足：必须先补证，当前仅能要求重新盘点。",
    leafIds: leaves,
  };
}

export function buildStocktakeDecision(
  preview: ReviewPreview,
  sources: ObservationSource[],
  sourcesComplete: boolean,
  decision: ReviewDecision,
  selected: string[],
  reason: string,
) {
  const safety = evaluateReviewSafety(preview, sources, sourcesComplete);
  const ids = [...new Set(selected)].sort();
  if (!safety.canRecount || !reason.trim() || reason.trim().length < 3 ||
      reason.trim().length > 2000 || !ids.length ||
      ids.some(id => !safety.leafIds.includes(id)) ||
      (decision === "accepted_partial" &&
       (!safety.acceptedPartialAllowed || ids.length !== safety.leafIds.length))) {
    throw new Error("review_incomplete_evidence_or_decision");
  }
  return {
    expected_session_version: preview.session_version,
    decision,
    reason: reason.trim(),
    observation_ids: ids,
  };
}

function errorText(e: unknown): string {
  return e instanceof ApiError ? e.userMessage : "盘点读取或复核失败，请检查网络及权限后重试。";
}

async function loadAllObservations(id: string): Promise<ObservationSource[]> {
  const result: ObservationSource[] = [];
  let after = 0;
  // Bound pagination; too many sources must fail closed, not silently truncate.
  for (let page = 0; page < 100; page++) {
    const batch = await readStocktakeObservationPage(id, after, 100);
    if (batch.length === 0) return result;
    after = batch[batch.length - 1].event_seq;
    result.push(...batch);
    if (batch.length < 100) return result;
  }
  throw new Error("stocktake_observation_pagination_incomplete");
}

export function StocktakeReviewPage({
  session, initialPreview, initialSources = [],
}: {
  session: Session;
  /** Synthetic snapshot for static rendering in tests; never a production grant. */
  initialPreview?: ReviewPreview;
  initialSources?: ObservationSource[];
}) {
  const [reviewRejected, setReviewRejected] = useState(false);
  const canReview = useCapabilities().hasCapability(businessPermission.stocktake.write) && !reviewRejected;
  const [sessions, setSessions] = useState<StocktakeSessionRow[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [preview, setPreview] = useState<ReviewPreview | null>(initialPreview ?? null);
  const [sources, setSources] = useState<ObservationSource[]>(initialSources);
  const [sourceComplete, setSourceComplete] = useState(Boolean(initialPreview));
  const [blind, setBlind] = useState<BlindView | null>(null);
  const [decision, setDecision] = useState<ReviewDecision>("needs_recount");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [accessDenied, setAccessDenied] = useState(false);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const serial = useRef(0);
  const pendingKey = useRef<{ fingerprint: string; key: string } | null>(null);

  const safety = preview ? evaluateReviewSafety(preview, sources, sourceComplete) : null;
  const identityKey = [session.organizationId, session.storeId, session.subjectId].join(":");

  function clearDetail() {
    setPreview(null);
    setSources([]);
    setSourceComplete(false);
    setBlind(null);
    setSelectedIds([]);
    setReason("");
    setDecision("needs_recount");
    pendingKey.current = null;
  }
  function deny(caught: unknown): boolean {
    if (caught instanceof ApiError && (caught.status === 401 || caught.status === 403)) {
      serial.current++;
      setAccessDenied(true);
      setSessions([]);
      setSelectedId(null);
      clearDetail();
      setError("会话或盘点权限已失效，无法查看/提交本店复核。");
      return true;
    }
    return false;
  }
  async function refreshSessions() {
    const ticket = ++serial.current;
    const scope = getScopeVersion();
    setLoading(true);
    setAccessDenied(false);
    try {
      const rows = await authorizedRequest<StocktakeSessionRow[]>(
        "/api/v1/stocktake/sessions?limit=100");
      if (ticket !== serial.current || scope !== getScopeVersion()) return;
      if (!Array.isArray(rows)) throw new Error("invalid_session_response");
      setSessions(rows);
      setError("");
    } catch (e) {
      if (ticket !== serial.current || scope !== getScopeVersion()) return;
      if (!deny(e)) { setSessions([]); setError(errorText(e)); }
    } finally {
      if (ticket === serial.current && scope === getScopeVersion()) setLoading(false);
    }
  }
  async function open(id: string) {
    const ticket = ++serial.current;
    const scope = getScopeVersion();
    setSelectedId(id);
    clearDetail();
    setLoading(true);
    setError("");
    try {
      const root = "/api/v1/stocktake/sessions/" + encodeURIComponent(id);
      const current = await authorizedRequest<ReviewPreview>(root + "/review-preview");
      const blindView = await authorizedRequest<BlindView>(root + "/blind");
      const observations = await loadAllObservations(id);
      const verified = await authorizedRequest<StocktakeSessionRow>(root);
      if (ticket !== serial.current || scope !== getScopeVersion()) return;
      if (!validateReviewSnapshot(id, current, blindView, verified, observations)) {
        throw new Error("stocktake_source_version_or_evidence_changed");
      }
      setPreview(current);
      setSources(observations);
      setBlind(blindView);
      setSourceComplete(true);
    } catch (e) {
      if (ticket !== serial.current || scope !== getScopeVersion()) return;
      clearDetail();
      if (!deny(e)) setError(errorText(e));
    } finally {
      if (ticket === serial.current && scope === getScopeVersion()) setLoading(false);
    }
  }
  useEffect(() => {
    setSessions([]); setSelectedId(null); clearDetail(); setError("");
    void refreshSessions();
    return () => { serial.current++; };
  }, [identityKey, session.accessToken]);

  function changeDecision(next: ReviewDecision) {
    pendingKey.current = null;
    setDecision(next);
  }
  function changeSelected(id: string, checked: boolean) {
    pendingKey.current = null;
    setSelectedIds(previous => checked
      ? [...new Set([...previous, id])] : previous.filter(x => x !== id));
  }
  async function submit() {
    if (reviewRejected || !hasCapability(businessPermission.stocktake.write) || !preview || !sourceComplete || submitting || loading || accessDenied ||
        !selectedId || !blind) return;
    let body: ReturnType<typeof buildStocktakeDecision>;
    try {
      body = buildStocktakeDecision(preview, sources, sourceComplete,
                                   decision, selectedIds, reason);
    } catch {
      setError("请先选取实际观察、填写复核原因并补齐所需证据。");
      return;
    }
    const fingerprint = JSON.stringify({ id: selectedId, ...body });
    if (pendingKey.current?.fingerprint !== fingerprint)
      pendingKey.current = { fingerprint, key: crypto.randomUUID() };
    const key = pendingKey.current.key;
    const scope = getScopeVersion();
    setSubmitting(true);
    setError("");
    try {
      await authorizedRequest(
        "/api/v1/stocktake/sessions/" + encodeURIComponent(selectedId) + "/reviews", {
        method: "POST", headers: { "Idempotency-Key": key }, body,
      });
      if (scope !== getScopeVersion()) return;
      pendingKey.current = null;
      await open(selectedId);
    } catch (e) {
      if (scope !== getScopeVersion()) return;
      if (e instanceof ApiError && e.status === 401 && deny(e)) return;
      if (e instanceof ApiError && e.status === 403) {
        setReviewRejected(true);
        await open(selectedId);
        if (scope === getScopeVersion()) setError("提交复核未获授权；保留可重新核实的只读证据。");
        return;
      }
      if (e instanceof ApiError && e.status === 409) {
        const id = selectedId;
        clearDetail();
        await open(id);
        if (scope === getScopeVersion()) {
          setError("所见盘点版本已变化或来源冲突；请重选观察和原因，不会自动重试。");
        }
      } else {
        setError(errorText(e));
      }
    } finally {
      if (scope === getScopeVersion()) setSubmitting(false);
    }
  }

  return <section className="welcome-card" style={{ maxWidth: 1060 }} aria-labelledby="stocktake-title">
    <p className="eyebrow">管理端 · STK-007 盘点证据复核</p>
    <h2 id="stocktake-title">局部盘点、换算与人工裁决</h2>
    <p className="welcome-copy">仅查看当前门店现场观察，不等于账面库存。任何复核只生成审计收据，绝不调整库存或写入管家婆。</p>
    <p role="status">覆盖范围：局部 / 未知；正式账面库存、真实差异及全店准确率均不可计算。</p>
    {!canReview && <p>当前身份只能读盘点证据，不能提交复核。</p>}
    {error ? <p className="login-error" role="alert">{error}</p> : null}
    {accessDenied ? <p role="alert">当前账号无盘点读取或复核权限；敏感数据已隐藏。</p> : <>
      <button type="button" onClick={() => void refreshSessions()} disabled={loading || submitting}>刷新本店盘点会话</button>
      <h3>会话列表（最多 100 条）</h3>
      {sessions.length === 100 ? <p>列表已达上限，后续会话未展示，不能认定全店覆盖。</p> : null}
      <ul>{sessions.map(item => <li key={item.id}>
        <button type="button" disabled={loading || submitting} onClick={() => void open(item.id)}>
          查看 {item.id} · {item.coverage_kind === "unknown" ? "范围未知" : "局部范围"} · 版本 {item.version}
        </button>
        <span> {item.coverage_note}</span>
      </li>)}</ul>
      {loading ? <p role="status">正在读取盘点证据并核验版本…</p> : null}
      {preview ? <>
        <h3>所见会话 {preview.session_id} · 版本 {preview.session_version}</h3>
        <p>覆盖：{preview.coverage_kind === "unknown" ? "未知范围" : "仅局部覆盖"}
          {preview.coverage_location_id ? " · 位置 " + preview.coverage_location_id : ""}
          {" · "}{preview.coverage_note}</p>
        <p>盲盘：{blind?.blind ? "开启（不显示账面数量）" : "未核验"} · 来源快照时间：{preview.preview_as_of}</p>
        <p>正式账面数量：不可用 · 与账面差异：不可计算 · 全店覆盖：否</p>
        <p>{preview.disclaimer}</p>
        <p>{safety?.reason}</p>
        <h3>现场观察与更正链（{preview.observation_count} 条有效叶子）</h3>
        {preview.observations.length === 0 ? <p>暂无可复核观察。</p> : <ul>
          {preview.observations.map(line => {
            const chain = line.source_observation_ids
              .map(id => sources.find(source => source.id === id)).filter(
                (x): x is ObservationSource => Boolean(x));
            const mediaIds = chain.map(source => source.media_id).filter(
              (id): id is string => Boolean(id));
            return <li key={line.observation_id}>
              <label><input type="checkbox" checked={selectedIds.includes(line.observation_id)}
                onChange={e => changeSelected(line.observation_id, e.target.checked)}
                disabled={!canReview || !sourceComplete || loading || submitting} />
                观察 {line.observation_id} · 序号 {line.event_seq}</label>
              <p>商品：{line.product_id ?? "待识别：" + (line.unknown_product_label ?? "未知")} ·
                库位：{line.location_id ?? "待识别：" + (line.unknown_location_label ?? "未知")}</p>
              <p>原始现场数量：{line.quantity} {line.unit} ·
                归一件数：{line.normalized_piece_quantity ?? "未知，禁止合计"}</p>
              <p>换算：{line.conversion_status} ·
                凭证：{line.conversion_receipt_ref ?? "尚无有效人工核验凭证"}</p>
              <p>记录人：{line.observed_by} · 观察于：{line.observed_at} · 入库记录于：{line.recorded_at}</p>
              <p>原观察/修正链：{line.source_observation_ids.join(" → ")}</p>
              <p>照片证据：{mediaIds.length ? mediaIds.join("、") : "未附照片（保留原观察）"}</p>
              <details><summary>查看原始观察证据详情</summary>
                <ul>{chain.map(source => <li key={source.id}>
                  {source.kind} / {source.id} · {source.quantity} {source.unit} ·
                  观察于 {source.observed_at} · 记录于 {source.recorded_at} ·
                  {source.media_id ?? "未附照片"}
                </li>)}</ul>
              </details>
            </li>;
          })}
        </ul>}
        <h3>按商品与库位的局部归一汇总</h3>
        <ul>{preview.groups.map(group => <li key={group.product_id + ":" + group.location_id}>
          {group.product_id} / {group.location_id}：
          {group.conversion_complete ? group.normalized_piece_quantity + " " + group.unit :
            "换算未完成，数量不可计算"}
          {!group.conversion_complete ? " · 原因：" + group.incomplete_reasons.join("、") : ""}
        </li>)}</ul>
        <h3>包装换算依据</h3>
        {preview.conversion_receipts.length === 0 ? <p>尚无已记录的换算核验收据。</p> : <ul>
          {preview.conversion_receipts.map(receipt => <li key={receipt.receipt_ref}>
            {receipt.product_id} · 系数 {receipt.factor} · {receipt.status} ·
            照片 {receipt.evidence_media_id} · 有效期 {receipt.effective_from} 至 {receipt.expires_at} ·
            核验人 {receipt.verified_by} · 收据 {receipt.receipt_ref} · 版本 {receipt.session_version}
          </li>)}</ul>}
        <h3>已保存的人工裁决（仅证明局部复核）</h3>
        <p>最新裁决：{preview.review_current ? "当前版本已留人审收据（仍是局部盘点）" :
          "当前版本待复核或旧裁决已过期"}</p>
        {preview.review_receipts.length === 0 ? <p>尚无复核收据。</p> : <ul>
          {preview.review_receipts.map(receipt => <li key={receipt.receipt_ref}>
            {receipt.decision} · 版本 {receipt.session_version} · 审核人 {receipt.reviewer_id} ·
            {receipt.reviewed_at} · 原因：{receipt.reason} · 来源 {receipt.observation_ids.join("、")} ·
            账面写入：否
          </li>)}</ul>}
        <h3>仅人工复核，不执行库存变更</h3>
        <fieldset disabled={!canReview || !safety?.canRecount || !blind || loading || submitting}>
          <legend>复核的是所见会话版本 {preview.session_version}</legend>
          <label>裁决类别 <select value={decision} onChange={e => changeDecision(e.target.value as ReviewDecision)}>
            <option value="needs_recount">需重新盘点 / 补证</option>
            <option value="accepted_partial" disabled={!safety?.acceptedPartialAllowed}>
              接受本次局部观察（非账面准确）
            </option>
          </select></label>
          <label style={{ display: "block", marginTop: 12 }}>人工复核原因
            <textarea value={reason} maxLength={2000} rows={3}
              onChange={e => { pendingKey.current = null; setReason(e.target.value); }} />
          </label>
          <button type="button" onClick={() => {
            pendingKey.current = null;
            setSelectedIds(safety?.leafIds ?? []);
          }}>勾选本次全部有效观察</button>
        </fieldset>
        <button className="login-button" type="button"
          disabled={!canReview || !safety?.canRecount || (decision === "accepted_partial" &&
            !safety?.acceptedPartialAllowed) || loading || submitting || !blind ||
            selectedIds.length === 0 || reason.trim().length < 3}
          onClick={() => void submit()}>
          {submitting ? "正在提交人工裁决…" : "提交所见版本人工复核收据（不调库存）"}
        </button>
      </> : selectedId && !loading ? <p>当前会话无法展示完整证据，请重新读取。</p> : null}
    </>}
  </section>;
}
