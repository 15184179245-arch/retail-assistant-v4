import { useEffect, useRef, useState } from "react";
import { L1CoeditField, type PendingAiSuggestion } from "../blocks";
import { Button } from "../components/Button";
import { StatusBadge } from "../components/StatusBadge";
import {
  TemplateCEvidenceSidebar,
  type AuditTrailEntry,
  type EvidenceCardItem,
} from "../components/templates";
import {
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
import { acknowledgeDraftInput, createArtifactContextFence, createArtifactMutationQueue } from "./artifactMutationQueue";
import type { PendingSuggestionDto, WorkspaceArtifactDto } from "../lib/api/generated";
import {
  appendArtifactOp,
  clearAiModifiedField,
  getWorkspaceState,
  pushToast,
  setArtifactOps,
  setPartialUndoNotice,
  setTabDirty,
  upsertArtifact,
  useWorkspaceState,
} from "../workspace/store";
import styles from "../styles/workbench.module.css";

export interface ArtifactCoeditWorkspaceProps {
  artifactId: string;
  tabId: string;
  session: Session;
  onRequestL2Submit: (artifact: WorkspaceArtifactDto) => void;
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
  onRequestL2Submit,
}: ArtifactCoeditWorkspaceProps) {
  const ws = useWorkspaceState();
  const artifact = ws.artifactsById[artifactId];
  const ops = ws.opsByArtifactId[artifactId] ?? [];
  const aiFields = ws.aiModifiedFields[artifactId] ?? {};
  const partialUndo = ws.partialUndoByArtifactId[artifactId];
  const [draftInputs, setDraftInputs] = useState<Record<string, string>>({});
  const [statusError, setStatusError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const contextKey = JSON.stringify([getScopeVersion(), ws.activeThreadId, artifactId, tabId]);
  const contextFence = useRef(createArtifactContextFence());
  const fieldRevisions = useRef<Record<string, number>>({});
  contextFence.current.enter(contextKey);
  const captureEditingContext = () => {
    const scopeCurrent = captureSessionScope();
    const generationCurrent = contextFence.current.capture(contextKey);
    const threadId = ws.activeThreadId;
    return () => scopeCurrent() && generationCurrent()
      && getWorkspaceState().activeThreadId === threadId
      && getWorkspaceState().activeTabId === tabId;
  };
  useEffect(() => {
    contextFence.current.activate();
    return () => { contextFence.current.invalidate(); };
  }, [contextKey]);

  const enqueueCurrentArtifactOp = (task: (isCurrent: () => boolean) => Promise<void>) => {
    // Capture before queueing. A later task may not borrow another object/context.
    const contextCurrent = captureEditingContext();
    return enqueueArtifactOp(artifactId, async (scopeCurrent) => {
      const isCurrent = () => scopeCurrent() && contextCurrent();
      if (!isCurrent()) return;
      await task(isCurrent);
    });
  };

  useEffect(() => {
    const contextCurrent = captureEditingContext();
    let cancelled = false;
    const isCurrent = () => !cancelled && contextCurrent();
    void getArtifactDetail(artifactId)
      .then((detail) => {
        if (!isCurrent()) return;
        upsertArtifact(detail.artifact);
        setArtifactOps(artifactId, detail.ops);
      })
      .catch(() => { if (isCurrent()) setStatusError("尚未核实服务端当前草稿。下方缓存不代表最新状态，请先核查。"); });
    return () => { cancelled = true; };
  }, [contextKey]);

  useEffect(() => {
    setDraftInputs({});
    fieldRevisions.current = {};
  }, [contextKey]);

  useEffect(() => {
    if (captureEditingContext()()) setTabDirty(tabId, Object.keys(draftInputs).length > 0);
  }, [draftInputs, tabId, contextKey]);

  if (!artifact) {
    return (
      <div className={styles.tabMainCard} data-artifact-loading={artifactId}>
        <p role={statusError ? "alert" : "status"}>{statusError ?? `正在加载工作成果 ${artifactId}...`}</p>
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
        const current = getWorkspaceState().artifactsById[artifactId] ?? artifact;
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
        upsertArtifact(latest);
        clearAiModifiedField(artifactId, fieldKey);
        appendArtifactOp(artifactId, patchRes.op);
        if (!latest.locked_fields.includes(fieldKey)) {
          const lockRes = await lockArtifactFields(artifactId, {
            base_version: latest.version,
            fields: [fieldKey],
          });
          if (!isCurrent()) return;
          assertFieldReceipt(lockRes, { artifactId, artifactThreadId: latest.thread_id, baseVersion: latest.version,
            field: fieldKey, value: parsedVal, session, kind: "lock" });
          latest = lockRes.artifact;
          appendArtifactOp(artifactId, lockRes.op);
        }
        upsertArtifact(latest);
        setDraftInputs((prev) => isCurrent() && fieldRevisions.current[fieldKey] === fieldRevision
          ? acknowledgeDraftInput(prev, fieldKey, rawVal) : prev);
      } catch (err) {
        if (!isCurrent()) return;
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
    if (!captureEditingContext()()) return;
    const revision = (fieldRevisions.current[fieldKey] ?? 0) + 1;
    fieldRevisions.current[fieldKey] = revision;
    setDraftInputs((prev) => ({ ...prev, [fieldKey]: nextValue }));
    setTabDirty(tabId, true);
    void commitFieldEdit(fieldKey, nextValue, revision);
  };

  const handleLock = async (fieldKey: string) => {
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = getWorkspaceState().artifactsById[artifactId] ?? artifact;
        const res = await lockArtifactFields(artifactId, {
          base_version: current.version,
          fields: [fieldKey],
        });
        if (!isCurrent()) return;
        upsertArtifact(res.artifact);
        appendArtifactOp(artifactId, res.op);
      } catch {
        if (!isCurrent()) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleUnlock = async (fieldKey: string) => {
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = getWorkspaceState().artifactsById[artifactId] ?? artifact;
        const res = await unlockArtifactFields(artifactId, {
          base_version: current.version,
          fields: [fieldKey],
        });
        if (!isCurrent()) return;
        upsertArtifact(res.artifact);
        appendArtifactOp(artifactId, res.op);
      } catch {
        if (!isCurrent()) return;
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
        const current = getWorkspaceState().artifactsById[artifactId] ?? artifact;
        const res = await acceptArtifactSuggestion(artifactId, opId, {
          base_version: current.version,
          field: fieldKey,
        });
        if (!isCurrent()) return;
        upsertArtifact(res.artifact);
        appendArtifactOp(artifactId, res.op);
      } catch {
        if (!isCurrent()) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleIgnoreSuggestion = async (fieldKey: string, opId?: string) => {
    if (!opId) { setStatusError("缺少服务端建议记录，未忽略。"); return; }
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = getWorkspaceState().artifactsById[artifactId] ?? artifact;
        const res = await ignoreArtifactSuggestion(artifactId, opId, {
          base_version: current.version,
          field: fieldKey,
        });
        if (!isCurrent()) return;
        upsertArtifact(res.artifact);
        appendArtifactOp(artifactId, res.op);
      } catch {
        if (!isCurrent()) return;
        setStatusError("服务端结果未确认，未冒充操作成功。请先核查最新草稿。");
      }
    });
  };

  const handleUndoOp = async (opId: string) => {
    setBusy(true);
    setStatusError(null);
    await enqueueCurrentArtifactOp(async (isCurrent) => {
      try {
        const current = getWorkspaceState().artifactsById[artifactId] ?? artifact;
        const res = await undoArtifact(artifactId, {
          base_version: current.version,
          target_op_id: opId,
        });
        if (!isCurrent()) return;
        upsertArtifact(res.artifact);
        appendArtifactOp(artifactId, res.op);
        const reverted = new Set(res.op.applied_fields ?? []);
        for (const f of reverted) {
          clearAiModifiedField(artifactId, String(f));
        }
        if (res.skipped_fields.length > 0) {
          setPartialUndoNotice(artifactId, {
            skippedFields: res.skipped_fields,
            message: `${res.skipped_fields.length} 个字段已被人工修改，未撤销`,
          });
        } else {
          setPartialUndoNotice(artifactId, null);
        }
      } catch {
        if (!isCurrent()) return;
        setStatusError("撤销结果未确认。未在本地伪造回滚，请先核查服务端草稿。");
      } finally {
        if (isCurrent()) setBusy(false);
      }
    });
  };

  const handleRejectAiField = async (fieldKey: string, opId?: string) => {
    const diff = aiFields[fieldKey];
    if (opId) {
      await handleUndoOp(opId);
      return;
    }
    if (diff) setStatusError("缺少服务端操作记录，未执行字段回滚。");

  };

  const latestAgentOp = ops.find((op) => op.actor_type === "agent" && op.op_type === "patch");
  const latestAiDiff = Object.values(aiFields)[0];

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
    undoable: op.actor_type === "agent" && op.op_type === "patch",
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
          latestAiDiff
            ? () => void handleUndoOp(latestAiDiff.opId)
            : latestAgentOp
              ? () => void handleUndoOp(latestAgentOp.id)
              : undefined
        }
        undoConflictNotice={partialUndo?.message ?? null}
        onDismissConflictNotice={() => setPartialUndoNotice(artifactId, null)}
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
                onClick={() => {
                  const isCurrent = captureEditingContext();
                  void getArtifactDetail(artifactId)
                    .then((detail) => {
                      if (!isCurrent()) return;
                      upsertArtifact(detail.artifact);
                      setArtifactOps(artifactId, detail.ops);
                      pushToast("已重新查询服务端真实状态。", "info");
                    })
                    .catch(() => {
                      if (!isCurrent()) return;
                      pushToast("暂无法连上服务端核查状态。", "warning");
                    });
                }}
              >
                先查实际状态
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="primary"
              disabled={busy || artifact.status === "submitted"}
              onClick={() => onRequestL2Submit(artifact)}
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
            const currentValue =
              draftInputs[fieldKey] ??
              (rawScalar !== undefined && rawScalar !== null
                ? String(rawScalar)
                : "");
            const aiDiff = aiFields[fieldKey];
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
                disabled={artifact.status === "submitted"}
                aiEdited={Boolean(aiDiff)}
                aiOpId={aiDiff?.opId}
                aiPreviousValue={
                  typeof aiDiff?.previousValue === "string" ||
                  typeof aiDiff?.previousValue === "number"
                    ? aiDiff.previousValue
                    : undefined
                }
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
                onDismissPartialUndoNotice={() => setPartialUndoNotice(artifactId, null)}
                onChange={(nextVal) => handleFieldInput(fieldKey, nextVal)}
                onLock={(fk) => void handleLock(fk)}
                onUnlock={(fk) => void handleUnlock(fk)}
                onAcceptSuggestion={(fk, val) =>
                  void handleAcceptSuggestion(fk, val, suggestionRaw?.op_id)
                }
                onIgnoreSuggestion={(fk) =>
                  void handleIgnoreSuggestion(fk, suggestionRaw?.op_id)
                }
                onRejectAiField={(fk, opId) => void handleRejectAiField(fk, opId)}
                onUndoWholeOp={(opId) => void handleUndoOp(opId)}
              />
            );
          })}
        </div>
      </TemplateCEvidenceSidebar>
    </div>
  );
}
