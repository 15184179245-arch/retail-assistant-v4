import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "../lib/api/client";
import { L1CoeditField, type PendingAiSuggestion } from "../blocks";
import { Button } from "../components/Button";
import { StatusBadge } from "../components/StatusBadge";
import {
  TemplateCEvidenceSidebar,
  type AuditTrailEntry,
  type EvidenceCardItem,
} from "../components/templates";
import {
  type ArtifactDetailResult,
  type ArtifactMutationResult,
  acceptArtifactSuggestion,
  getArtifactDetail,
  ignoreArtifactSuggestion,
  lockArtifactFields,
  patchArtifact,
  undoArtifact,
  unlockArtifactFields,
} from "../lib/api/workspace";
import { captureSessionScope, getScopeVersion, type Session } from "../lib/auth/session";
import { getCapabilityEpoch, getCapabilityStatus, hasCapability, subscribeCapabilities, useCapabilities } from "../lib/auth/capabilities";
import { canWriteArtifact } from "../lib/auth/businessPermissions";
import { acknowledgeDraftInput, createArtifactContextFence, createArtifactMutationQueue } from "./artifactMutationQueue";
import type { PendingSuggestionDto, WorkspaceArtifactDto } from "../lib/api/generated";
import {
  evictArtifactFacts,
  updateArtifactDraftInputs,
  getWorkspaceState,
  pushToast,
  setArtifactOps,
  setTabDirty,
  upsertArtifact,
  useWorkspaceState,
} from "../workspace/store";
import styles from "../styles/workbench.module.css";

export interface ArtifactCoeditWorkspaceProps {
  artifactId: string;
  tabId: string;
  session: Session;
  onOpenSubmitBoundary: () => void;
  onSyncArtifactToThread?: (artifact: WorkspaceArtifactDto) => void;
}

const FIELD_META: Record<string, { label: string; type: "text" | "number" }> = {
  customer_name: { label: "客户名称", type: "text" },
  sku: { label: "商品编码 SKU", type: "text" },
  product_name: { label: "商品名称", type: "text" },
  spec: { label: "规格型号", type: "text" },
  qty: { label: "数量", type: "number" },
  unit_price: { label: "单价 (元)", type: "number" },
  note: { label: "备注说明", type: "text" },
};

const EDITABLE_FIELD_ORDER = [
  "customer_name",
  "sku",
  "product_name",
  "spec",
  "qty",
  "unit_price",
  "note",
];

function readScalarFieldValue(
  payload: Record<string, unknown>,
  fieldKey: string,
): string | number | undefined {
  const direct = payload[fieldKey];
  if (typeof direct === "string" || typeof direct === "number") {
    return direct;
  }
  const firstLine = Array.isArray(payload.lines)
    ? (payload.lines[0] as Record<string, unknown> | undefined)
    : undefined;
  if (fieldKey === "qty" && firstLine && (typeof firstLine.qty === "number" || typeof firstLine.qty === "string")) {
    return firstLine.qty;
  }
  if (fieldKey === "product_name" && firstLine && typeof firstLine.name === "string") {
    return firstLine.name;
  }
  if (fieldKey === "sku" && firstLine && typeof firstLine.ref_id === "string") {
    return firstLine.ref_id;
  }
  if (fieldKey === "customer_name" && typeof payload.customer_label === "string") {
    return payload.customer_label;
  }
  if (fieldKey === "unit_price" && firstLine && (typeof firstLine.unit_price === "number" || typeof firstLine.unit_price === "string")) {
    return firstLine.unit_price;
  }
  return undefined;
}


function assertFieldReceipt(
  result: ArtifactMutationResult,
  expected: { artifactId: string; artifactThreadId: string; baseVersion: number;
    field: string; value: string | number; session: Session; kind: "patch" | "lock" },
): void {
  const { artifact, op } = result;
  const matching = artifact.id === expected.artifactId
    && artifact.thread_id === expected.artifactThreadId
    && artifact.organization_id === expected.session.organizationId
    && artifact.store_id === expected.session.storeId
    && op.artifact_id === expected.artifactId
    && op.actor_subject_id === expected.session.subjectId
    && op.actor_type === "human" && op.op_type === expected.kind
    && op.version_before === expected.baseVersion
    && op.version_after === artifact.version && artifact.version > expected.baseVersion
    && (expected.kind === "lock"
      ? artifact.locked_fields.includes(expected.field)
      : op.applied_fields.includes(expected.field)
        && op.patch[expected.field] === expected.value
        && readScalarFieldValue(artifact.payload, expected.field) === expected.value);
  if (!matching) throw new Error("回执未匹配本次对象、字段或基准版本，人工输入仍保留。");
}

const enqueueArtifactOp = createArtifactMutationQueue();

export function ArtifactCoeditWorkspace({
  artifactId,
  tabId,
  session,
  onOpenSubmitBoundary,
}: ArtifactCoeditWorkspaceProps) {
  const ws = useWorkspaceState();
  const { hasCapability: hasGrant, status: capabilityStatus } = useCapabilities();
  const scope = getScopeVersion();
  const epoch = useSyncExternalStore(subscribeCapabilities, getCapabilityEpoch, getCapabilityEpoch);
  const contextKey = JSON.stringify([scope, epoch, ws.activeThreadId, artifactId, tabId]);
  type Proof = { key: string; detail: ArtifactDetailResult };
  const [readProof, setReadProof] = useState<Proof | null>(null);
  const readProofRef = useRef<Proof | null>(null);
  const [readIssue, setReadIssue] = useState<{ key: string; message: string } | null>(null);
  const readJob = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const versionFloor = useRef<{ key: string; version: number } | null>(null);
  const [writeDenied, setWriteDenied] = useState(false);
  const writeDeniedRef = useRef(false);
  const [undoNotice, setUndoNotice] = useState<{key: string; skippedFields: string[]; message: string} | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const artifact = capabilityStatus === "ready" && readProof?.key === contextKey ? readProof.detail.artifact : undefined;
  const canEdit = !!artifact && !writeDenied && artifact.status !== "submitted" && canWriteArtifact(artifact.kind, hasGrant);
  const ops = artifact && readProof ? readProof.detail.ops : [];
  const partialUndo = artifact && undoNotice?.key === contextKey ? undoNotice : null;
  const draftInputs = ws.draftInputsByThread[ws.activeThreadId]?.[tabId] ?? {};
  const setDraftInputs = (update: (previous: Record<string, string>) => Record<string, string>) =>
    updateArtifactDraftInputs(ws.activeThreadId, tabId, update);
  const contextFence = useRef(createArtifactContextFence());
  const fieldRevisions = useRef<Record<string, number>>({});
  contextFence.current.enter(contextKey);
  const captureEditingContext = () => {
    const scopeCurrent = captureSessionScope();
    const generationCurrent = contextFence.current.capture(contextKey);
    const threadId = ws.activeThreadId;
    return () => scopeCurrent() && generationCurrent() && getCapabilityStatus() === "ready"
      && getCapabilityEpoch() === epoch
      && getWorkspaceState().activeThreadId === threadId
      && getWorkspaceState().activeTabId === tabId;
  };
  const currentProof = () => readProofRef.current?.key === contextKey ? readProofRef.current.detail.artifact : undefined;
  const allowsMutation = () => {
    const proof = currentProof();
    return !!proof && getCapabilityStatus() === "ready" && getCapabilityEpoch() === epoch
      && !writeDeniedRef.current && proof.status !== "submitted" && canWriteArtifact(proof.kind, hasCapability);
  };
  const replaceProof = (next: Proof | null) => {
    readProofRef.current = next;
    setReadProof(next);
  };

  const enqueueCurrentArtifactOp = (task: (isCurrent: () => boolean) => Promise<void>) => {
    // Capture before queueing. A later task may not borrow another object/context.
    if (!allowsMutation()) return;
    const contextCurrent = captureEditingContext();
    return enqueueArtifactOp(artifactId, async (scopeCurrent) => {
      const isCurrent = () => scopeCurrent() && contextCurrent();
      if (!isCurrent() || !allowsMutation()) return;
      await task(isCurrent);
    });
  };

  async function readArtifact(notify = false, acknowledgeAtLeast = 0): Promise<boolean> {
    if (!mounted.current || capabilityStatus !== "ready") return false;
    contextFence.current.activate();
    const contextCurrent = captureEditingContext();
    if (!contextCurrent()) return false;
    readJob.current?.abort();
    const request = new AbortController();
    readJob.current = request;
    const isCurrent = () => readJob.current === request && !request.signal.aborted && contextCurrent();
    const previous = versionFloor.current?.key === contextKey ? versionFloor.current.version : 0;
    const minimumVersion = Math.max(previous, acknowledgeAtLeast);
    versionFloor.current = { key: contextKey, version: minimumVersion };
    replaceProof(null);
    setReadIssue(null);
    try {
      const detail = await getArtifactDetail(artifactId, request.signal);
      if (!isCurrent()) return false;
      const facts = detail.artifact;
      if (facts.id !== artifactId || facts.organization_id !== session.organizationId || facts.store_id !== session.storeId
          || facts.owner_subject_id !== session.subjectId || !Number.isSafeInteger(facts.version)
          || typeof facts.title !== "string" || !Array.isArray(facts.locked_fields)
          || facts.version < Math.max(1, minimumVersion) || !Array.isArray(detail.ops)
          || detail.ops.some(op => op.artifact_id !== artifactId)) {
        throw Error("读取回执没有匹配当前草稿和版本");
      }
      const ordered = { artifact: facts, ops: [...detail.ops].sort((a, b) => b.version_after - a.version_after) };
      versionFloor.current = { key: contextKey, version: facts.version };
      replaceProof({ key: contextKey, detail: ordered });
      setStatusError(writeDeniedRef.current ? "写入未获授权。草稿已核实，当前只读；未保存输入不会自动发送。" : null);
      upsertArtifact(facts, { verifiedRead: { scope, epoch } });
      setArtifactOps(artifactId, ordered.ops);
      if (notify) pushToast("已重新查询服务端真实状态。", "info");
      return true;
    } catch (error) {
      if (!isCurrent()) return false;
      if (error instanceof ApiError && [403, 404].includes(error.status)) {
        setBusy(false);
        contextFence.current.invalidate();
        evictArtifactFacts(artifactId);
      }
      setReadIssue({ key: contextKey, message: "未能核实当前身份下的草稿。旧正文、证据及历史未显示；未保存的输入不会自动发送。" });
      return false;
    } finally {
      if (readJob.current === request) readJob.current = null;
    }
  }

  function recheckAfterWriteDenial(error: unknown): boolean {
    if (!(error instanceof ApiError) || ![403, 404].includes(error.status)) return false;
    writeDeniedRef.current = true;
    setWriteDenied(true);
    setBusy(false);
    // A write refusal neither grants a read nor proves denial. Only a fresh
    // GET can publish facts in this scope, and it never replays an edit.
    void readArtifact();
    return true;
  }

  useEffect(() => {
    mounted.current = true;
    writeDeniedRef.current = false;
    setWriteDenied(false);
    setUndoNotice(null);
    fieldRevisions.current = {};
    setBusy(false);
    contextFence.current.activate();
    void readArtifact();
    return () => { mounted.current = false; readJob.current?.abort(); contextFence.current.invalidate(); };
  }, [contextKey]);

  if (!artifact) {
    const reason = readIssue?.key === contextKey ? readIssue.message : null;
    return (
      <div className={styles.tabMainCard} data-artifact-loading={artifactId}>
        <p role={reason ? "alert" : "status"}>{reason ?? "正在核实当前身份下的工作成果。"}</p>
        {capabilityStatus === "ready" && <Button onClick={() => void readArtifact()}>重新核查权限与草稿</Button>}
      </div>
    );
  }

  const commitFieldEdit = async (fieldKey: string, rawVal: string, fieldRevision: number) => {
    const meta = FIELD_META[fieldKey];
    const parsedVal: string | number =
      meta?.type === "number" && rawVal.trim() !== "" && !Number.isNaN(Number(rawVal))
        ? Number(rawVal)
        : rawVal;

    // Edited input remains local until an acknowledged server response.
    setTabDirty(tabId, true);
    setStatusError(null);

    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = currentProof() ?? artifact;
        const patchRes = await patchArtifact(artifactId, {
          base_version: current.version,
          patch: { [fieldKey]: parsedVal },
          actor_type: "human",
          reason: `人工编辑 ${meta?.label ?? fieldKey}`,
        });
        if (!isCurrent()) return;
        assertFieldReceipt(patchRes, { artifactId, artifactThreadId: current.thread_id, baseVersion: current.version,
          field: fieldKey, value: parsedVal, session, kind: "patch" });
        let latest = patchRes.artifact;
        if (!latest.locked_fields.includes(fieldKey)) {
          const lockRes = await lockArtifactFields(artifactId, {
            base_version: latest.version,
            fields: [fieldKey],
          });
          if (!isCurrent()) return;
          assertFieldReceipt(lockRes, { artifactId, artifactThreadId: latest.thread_id, baseVersion: latest.version,
            field: fieldKey, value: parsedVal, session, kind: "lock" });
          latest = lockRes.artifact;
        }
        if (!await readArtifact(false, latest.version)) return;
        setDraftInputs((prev) => isCurrent() && fieldRevisions.current[fieldKey] === fieldRevision
          ? acknowledgeDraftInput(prev, fieldKey, rawVal) : prev);
      } catch (err) {
        if (!isCurrent() || recheckAfterWriteDenial(err)) return;
        setTabDirty(tabId, true);
        setStatusError(
          err instanceof Error
            ? `服务端同步未确认（输入仍保留，未冒充已保存或已锁定；请先核查）：${err.message}`
            : "服务端同步失败",
        );
      }
    });
  };

  const handleFieldInput = (fieldKey: string, nextValue: string) => {
    if (!captureEditingContext()() || !allowsMutation()) return;
    const revision = (fieldRevisions.current[fieldKey] ?? 0) + 1;
    fieldRevisions.current[fieldKey] = revision;
    setDraftInputs((prev) => ({ ...prev, [fieldKey]: nextValue }));
    setTabDirty(tabId, true);
    void commitFieldEdit(fieldKey, nextValue, revision);
  };

  const handleLock = async (fieldKey: string) => {
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = currentProof() ?? artifact;
        const res = await lockArtifactFields(artifactId, {
          base_version: current.version,
          fields: [fieldKey],
        });
        if (!isCurrent()) return;
        await readArtifact(false, res.artifact.version);
      } catch (error) {
        if (!isCurrent() || recheckAfterWriteDenial(error)) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleUnlock = async (fieldKey: string) => {
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = currentProof() ?? artifact;
        const res = await unlockArtifactFields(artifactId, {
          base_version: current.version,
          fields: [fieldKey],
        });
        if (!isCurrent()) return;
        await readArtifact(false, res.artifact.version);
      } catch (error) {
        if (!isCurrent() || recheckAfterWriteDenial(error)) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleAcceptSuggestion = async (
    fieldKey: string,
    suggestedValue: string | number,
    opId?: string,
  ) => {
    if (!opId) { setStatusError("缺少服务端建议记录，未采纳。"); return; }
    void suggestedValue;
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = currentProof() ?? artifact;
        const res = await acceptArtifactSuggestion(artifactId, opId, {
          base_version: current.version,
          field: fieldKey,
        });
        if (!isCurrent()) return;
        await readArtifact(false, res.artifact.version);
      } catch (error) {
        if (!isCurrent() || recheckAfterWriteDenial(error)) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleIgnoreSuggestion = async (fieldKey: string, opId?: string) => {
    if (!opId) { setStatusError("缺少服务端建议记录，未忽略。"); return; }
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = currentProof() ?? artifact;
        const res = await ignoreArtifactSuggestion(artifactId, opId, {
          base_version: current.version,
          field: fieldKey,
        });
        if (!isCurrent()) return;
        await readArtifact(false, res.artifact.version);
      } catch (error) {
        if (!isCurrent() || recheckAfterWriteDenial(error)) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleUndoOp = async (opId: string) => {
    if (!allowsMutation()) return;
    setBusy(true);
    setStatusError(null);
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = currentProof() ?? artifact;
        const res = await undoArtifact(artifactId, {
          base_version: current.version,
          target_op_id: opId,
        });
        if (!isCurrent()) return;
        await readArtifact(false, res.artifact.version);
        if (!isCurrent() || !currentProof()) return;
        const skippedFields = res.skipped_fields.filter(field => Object.hasOwn(currentProof()!.payload, field));
        setUndoNotice(skippedFields.length ? { key: contextKey, skippedFields,
          message: `${skippedFields.length} 个字段已被人工修改，未撤销` } : null);
      } catch (error) {
        if (!isCurrent() || recheckAfterWriteDenial(error)) return;
        setStatusError("撤销结果未确认。未在本地伪造回滚，请先核查服务端草稿。");
      } finally {
        if (isCurrent()) setBusy(false);
      }
    });
  };

  const latestAgentOp = ops.find((op) => op.actor_type === "agent" && op.op_type === "patch");

  const evidenceItems: EvidenceCardItem[] = artifact.source_refs.map(
    (ref: Record<string, unknown>, idx: number) => ({
      id: String(ref.id ?? `ref-${idx}`),
      title: String(ref.name ?? ref.label ?? ref.kind ?? "服务端源引用"),
      detail: JSON.stringify(ref),
      statusKind: "fact",
    }),
  );

  if (artifact.submitted_receipt_ref) {
    evidenceItems.unshift({
      id: artifact.submitted_receipt_ref,
      title: `草稿保存凭据：${artifact.submitted_receipt_ref}`,
      detail: "仅代表工作区内的保存或核对记录；正式业务单据未提交，也未核验 ERP、库存或财务结果。",
      statusKind: artifact.status === "unverified" ? "unverified" : "fact",
    });
  }

  const auditTrail: AuditTrailEntry[] = ops.map((op) => ({
    id: op.id,
    actorType: op.actor_type,
    actorName: op.actor_type === "agent" ? "AI 助手 (L1)" : op.actor_subject_id,
    summary: `${op.op_type} · 修改字段：${Object.keys(op.patch).join("、") || "元信息"}`,
    timestamp: op.created_at,
    undoable: canEdit && op.actor_type === "agent" && op.op_type === "patch",
    onUndo: () => void handleUndoOp(op.id),
  }));

  const scalarFields = EDITABLE_FIELD_ORDER.filter(
    (k) =>
      readScalarFieldValue(artifact.payload, k) !== undefined ||
      k === "qty" ||
      k === "unit_price" ||
      k === "note",
  );
  const IGNORED_META_KEYS = new Set([
    "lines",
    "items",
    "stage",
    "status",
    "pinned_from_block_type",
    "status_kind",
    "customer_label",
  ]);
  for (const extraKey of Object.keys(artifact.payload)) {
    if (
      !scalarFields.includes(extraKey) &&
      !IGNORED_META_KEYS.has(extraKey) &&
      (typeof artifact.payload[extraKey] === "string" ||
        typeof artifact.payload[extraKey] === "number")
    ) {
      scalarFields.push(extraKey);
    }
  }

  const provenanceStatus =
    artifact.status === "submitted"
      ? "fact"
      : artifact.status === "unverified"
        ? "unverified"
        : "fact";

  return (
    <div data-artifact-workspace={artifact.id}>
      <TemplateCEvidenceSidebar
        title={artifact.title}
        eyebrow={`L1 工作成果共编 · ${artifact.kind} · v${artifact.version}`}
        provenance={{
          source: "workspace_artifact",
          observedAt: artifact.updated_at,
          verifiedBy: null,
          statusKind: provenanceStatus,
          version: artifact.version,
          receiptRef: null, // Workspace save identifiers are not formal business receipts.
        }}
        evidenceItems={evidenceItems}
        auditTrail={auditTrail}
        onUndoLatest={
          canEdit && latestAgentOp ? () => void handleUndoOp(latestAgentOp.id) : undefined
        }
        undoConflictNotice={partialUndo?.message ?? null}
        onDismissConflictNotice={() => setUndoNotice(null)}
        actions={
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", alignItems: "center" }}>
            <StatusBadge
              kind={provenanceStatus}
              source={
                artifact.status === "submitted"
                  ? "工作区已保存核对结果；正式单据未提交"
                  : artifact.status === "unverified"
                    ? "待核实"
                    : `草稿 v${artifact.version}`
              }
            />
            {artifact.status === "unverified" ? (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => void readArtifact(true)}
              >
                先查实际状态
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="primary"
              disabled={busy || artifact.status === "submitted"}
              onClick={onOpenSubmitBoundary}
              data-open-l2-submit="true"
            >
              {artifact.status === "submitted" ? "工作区草稿已保存" : "查看正式提交边界"}
            </Button>
          </div>
        }
      >
        {statusError ? (
          <p role="alert" style={{ margin: "0 0 12px", color: "var(--danger-ink)", fontSize: "13px" }}>
            {statusError}
          </p>
        ) : null}

        {!canEdit && <p>草稿当前为只读；查看、核对或记录本地输入不代表有权写入。</p>}

        {artifact.kind === "quote_draft" ? (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "8px",
              marginBottom: "12px",
              fontSize: "12px",
              color: "var(--muted)",
            }}
          >
            <span>
              stage: <code>{String(artifact.payload.stage ?? "draft")}</code>
            </span>
            <span>
              status: <code>{String(artifact.payload.status ?? "pending")}</code>
            </span>
            <span>锁定字段数：{artifact.locked_fields.length}</span>
          </div>
        ) : null}

        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          {scalarFields.map((fieldKey) => {
            const rawScalar = readScalarFieldValue(artifact.payload, fieldKey);
            const meta = FIELD_META[fieldKey] ?? {
              label: fieldKey,
              type: typeof rawScalar === "number" ? "number" : "text",
            };
            const currentValue = (canEdit ? draftInputs[fieldKey] : undefined) ??
              (rawScalar === undefined ? "" : String(rawScalar));
            const latestAuthor = ops.find(op => ["patch", "undo", "accept_suggestion"].includes(op.op_type) &&
              op.applied_fields.includes(fieldKey) && Object.hasOwn(op.patch, fieldKey));
            const aiDiff = latestAuthor?.op_type === "patch" && latestAuthor.actor_type === "agent" ? latestAuthor : undefined;
            const suggestionRaw = artifact.pending_suggestions.find(
              (s: PendingSuggestionDto) => s.field === fieldKey,
            );
            const pendingSuggestion: PendingAiSuggestion | null = suggestionRaw
              ? {
                  opId: suggestionRaw.op_id,
                  value:
                    typeof suggestionRaw.suggested_value === "number" ||
                    typeof suggestionRaw.suggested_value === "string"
                      ? suggestionRaw.suggested_value
                      : String(suggestionRaw.suggested_value ?? ""),
                  reason: suggestionRaw.reason ?? null,
                }
              : null;

            return (
              <L1CoeditField
                key={fieldKey}
                fieldKey={fieldKey}
                label={meta.label}
                type={meta.type}
                value={currentValue}
                readOnly={!canEdit}
                aiEdited={Boolean(aiDiff)}
                aiOpId={aiDiff?.id}
                locked={artifact.locked_fields.includes(fieldKey)}
                pendingSuggestion={pendingSuggestion}
                partialUndoConflictCount={
                  partialUndo && partialUndo.skippedFields.includes(fieldKey) ? 1 : 0
                }
                partialUndoSkippedFields={
                  partialUndo && partialUndo.skippedFields.includes(fieldKey)
                    ? partialUndo.skippedFields
                    : []
                }
                onDismissPartialUndoNotice={() => setUndoNotice(null)}
                onChange={canEdit ? (nextVal) => handleFieldInput(fieldKey, nextVal) : undefined}
                onLock={canEdit ? (fk) => void handleLock(fk) : undefined}
                onUnlock={canEdit ? (fk) => void handleUnlock(fk) : undefined}
                onAcceptSuggestion={canEdit ? (fk, val) =>
                  void handleAcceptSuggestion(fk, val, suggestionRaw?.op_id) : undefined
                }
                onIgnoreSuggestion={canEdit ? (fk) =>
                  void handleIgnoreSuggestion(fk, suggestionRaw?.op_id) : undefined
                }
                onUndoWholeOp={canEdit ? (opId) => void handleUndoOp(opId) : undefined}
              />
            );
          })}
        </div>
      </TemplateCEvidenceSidebar>
    </div>
  );
}
