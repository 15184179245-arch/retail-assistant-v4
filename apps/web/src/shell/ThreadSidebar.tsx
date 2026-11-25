import { useState } from "react";
import { BlockRenderer, type ValidatedUiBlock } from "../blocks";
import { StatusBadge } from "../components/StatusBadge";
import { navigate } from "../app/router";
import { captureSessionScope } from "../lib/auth/session";
import { pinBlockToArtifact } from "../lib/api/workspace";
import {
  attachObjectToComposer,
  detachObjectFromComposer,
  getActiveThread,
  openWorkbenchTab,
  pushToast,
  upsertArtifact,
  useWorkspaceState,
} from "../workspace/store";
import styles from "../styles/workbench.module.css";

export interface ThreadSidebarProps {
  modelOffline: boolean;
  onTriggerL2ActionCard: (block: ValidatedUiBlock) => void;
}

// 后端 SSE 是「取完增量即结束」的短流，正常结束后会自动续连下一次增量，
// 界面上应显示为已连接，而不是让用户以为一直在重连。
const STREAM_STATE_LABEL: Record<string, string> = {
  connecting: "连接中",
  open: "已连接",
  reconnecting: "重连中",
  closed: "已断开",
};

export function ThreadSidebar({
  modelOffline,
  onTriggerL2ActionCard,
}: ThreadSidebarProps) {
  const ws = useWorkspaceState();
  const activeThread = getActiveThread();
  const [prompt, setPrompt] = useState("");
  const [turnError, setTurnError] = useState<string | null>(null);

  const activeArtifact =
    activeThread.activeObjectRef?.artifact_id
      ? ws.artifactsById[activeThread.activeObjectRef.artifact_id]
      : undefined;

  const handlePinBlock = async (block: ValidatedUiBlock) => {
    const isCurrent = captureSessionScope();
    const title =
      typeof block.props.title === "string" && block.props.title.trim()
        ? block.props.title
        : `报价草稿 (${block.type})`;

    try {
      const result = await pinBlockToArtifact({
        thread_id: activeThread.id,
        block: block as unknown as Record<string, unknown>,
        title,
        kind: block.type === "purchase_draft" ? "purchase_draft" : "quote_draft",
      });
      if (!isCurrent()) return;
      upsertArtifact(result.artifact, { openTab: true });
      pushToast("已固定到右侧多标签工作台。", "success");
    } catch {
      if (!isCurrent()) return;
      setTurnError("固定结果未确认。请先刷新工作区核查，不会创建本地替身或自动重发。");
      pushToast("未确认固定结果，请先核查工作区。", "warning");
    }
  };

  return (
    <aside className={styles.threadPanel} aria-label="AI 对话线程栏" data-thread-sidebar="true">
      <div className={styles.panelHeader}>
        <div>
          <h3 className={styles.panelTitle}>{activeThread.title}</h3>
          <span style={{ fontSize: "11px", color: "var(--muted)" }}>
            流状态：{STREAM_STATE_LABEL[activeThread.streamState] ?? activeThread.streamState}
            {activeThread.lastCursor ? ` · 游标 #${activeThread.lastCursor}` : ""}
          </span>
        </div>
        <StatusBadge
          kind={modelOffline ? "waiting" : "fact"}
          source={modelOffline ? "AI 离线降级" : "线程上下文"}
        />
      </div>

      {modelOffline ? (
        <div className={styles.offlineBanner} role="status" data-thread-offline-banner="true">
          AI 模型当前离线：对话新生成已禁用，请继续在右侧工作台完成查询、手工开单与核验。
        </div>
      ) : null}

      <div className={styles.chipsRow} aria-label="对话上下文对象芯片">
        {activeThread.activeObjectRef ? (
          <span className={styles.contextChip} data-active-context-chip="true">
            <span>焦点：{activeThread.activeObjectRef.title ?? activeThread.activeObjectRef.id}</span>
            <button
              type="button"
              className={styles.chipAction}
              onClick={() => attachObjectToComposer(activeThread.activeObjectRef!)}
              title="@ 引用当前对象"
            >
              +@
            </button>
          </span>
        ) : (
          <span className={styles.shortcutBadge}>上下文：[今日] 全局</span>
        )}
        {activeThread.attachedObjects.map((obj) => (
          <span key={`${obj.kind}:${obj.id}`} className={styles.contextChip} data-attached-chip="true">
            <span>@{obj.title ?? obj.id}</span>
            <button
              type="button"
              className={styles.chipAction}
              aria-label={`移除引用 ${obj.title ?? obj.id}`}
              onClick={() => detachObjectFromComposer(obj.kind, obj.id)}
            >
              ×
            </button>
          </span>
        ))}
      </div>

      <div className={styles.streamMessages} data-thread-messages="true">
        {activeThread.messages.length === 0 ? (
          <div className={styles.messageCard}>
            <div style={{ fontSize: "13px", color: "var(--muted)" }}>
              这里展示已取得的工作区消息与成果。自然语言执行尚未接入，请使用「我的助理」；人工检索与草稿编辑仍可使用。
            </div>
          </div>
        ) : null}

        {activeThread.messages.map((msg) => (
          <div
            key={msg.id}
            className={`${styles.messageCard} ${
              msg.role === "user" ? styles.messageCardUser : ""
            }`}
          >
            <div className={styles.messageMeta}>
              <strong>{msg.role === "user" ? "我" : "智能助手"}</strong>
              <span>{msg.created_at.slice(11, 19)}</span>
            </div>
            {msg.text ? <p style={{ margin: "0 0 8px", fontSize: "13px" }}>{msg.text}</p> : null}
            {msg.blocks.map((block, idx) => (
              <div key={`${msg.id}-blk-${idx}`} style={{ marginTop: "8px" }}>
                <BlockRenderer
                  block={block}
                  onPinToWorkbench={(valid) => void handlePinBlock(valid)}
                  onOpenArtifact={(artId, kind) =>
                    openWorkbenchTab({
                      id: `artifact:${artId}`,
                      kind: "artifact",
                      title: ws.artifactsById[artId]?.title ?? `草稿 ${artId}`,
                      objectKind: kind ?? "quote_draft",
                      objectId: artId,
                      artifactId: artId,
                      closable: true,
                    })
                  }
                  onTriggerActionCard={(valid) => onTriggerL2ActionCard(valid)}
                />
              </div>
            ))}
          </div>
        ))}

        {turnError ? <p role="alert">{turnError}</p> : null}
      </div>

      <form
        className={styles.composerForm}
        onSubmit={(e) => {
          e.preventDefault();
          setTurnError("此工作区对话尚未接入常驻助理，输入未发送、草稿未修改。请前往「我的助理」，当前输入会留在此处直到离开本页。");
        }}
      >
        <textarea
          className={styles.composerTextarea}
          placeholder={
            modelOffline
              ? "模型离线中（只读历史可查，工作台手工操作不受影响）"
              : "可暂存补充文字；此工作区尚未接入 Agent，不会发送或修改草稿。"
          }
          disabled={modelOffline}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          data-thread-input="true"
        />
        <button type="button" className={styles.secondaryBtn} onClick={() => navigate("/assistant")}>前往我的助理（不自动发送）</button>
        <div className={styles.composerActions}>
          {activeArtifact && !modelOffline ? (
            <button
              type="button"
              className={styles.secondaryBtn}
              onClick={() =>
                onTriggerL2ActionCard({
                  type: "action_card",
                  version: "1.0",
                  props: {
                    title: `查看提交边界：${activeArtifact.title}`,
                    action_key: "workspace.artifact.submit",
                    description: "正式业务提交尚未接入；查看边界说明，不执行操作。",
                    confirm_required: true,
                    target_artifact_id: activeArtifact.id,
                    parameters: { base_version: activeArtifact.version },
                  },
                  refs: [{ kind: "artifact", id: activeArtifact.id, label: activeArtifact.title }],
                })
              }
            >
              查看正式提交边界
            </button>
          ) : (
            <span style={{ fontSize: "11px", color: "var(--muted)" }}>
              人工编辑仅作用于工作区草稿；正式单据未提交
            </span>
          )}
          <button
            type="submit"
            className={styles.primaryBtn}
            disabled={modelOffline || !prompt.trim()}
            data-thread-send="true"
          >
            查看连接说明
          </button>
        </div>
      </form>
    </aside>
  );
}
