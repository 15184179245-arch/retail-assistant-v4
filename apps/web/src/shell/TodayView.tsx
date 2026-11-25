import type { ReactNode } from "react";
import { navigate } from "../app/router";
import type { Session } from "../lib/auth/session";
import { StatusBadge } from "../components/StatusBadge";
import { openWorkbenchTab, useWorkspaceState } from "../workspace/store";
import { getPermittedWorkObjects, OBJECT_LIBRARY_CATEGORIES, toRouteWorkbenchTab } from "./workObjects";
import styles from "../styles/workbench.module.css";

export interface TodayViewProps {
  session: Session;
  capabilities: string[];
  homeContent?: ReactNode;
  onCreateQuoteDraft: () => void;
}

export function TodayView({
  capabilities,
  homeContent,
  onCreateQuoteDraft,
}: TodayViewProps) {
  const ws = useWorkspaceState();
  const permitted = getPermittedWorkObjects(capabilities);
  const artifacts = Object.values(ws.artifactsById);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "16px" }} data-today-view="true">
      {homeContent}

      <section aria-label="今日工作队列与快捷工作对象">
        <div className={styles.panelHeader} style={{ marginBottom: "8px" }}>
          <div>
            <h3 className={styles.panelTitle}>[今日] 工作对象与跨端协同入口</h3>
            <p style={{ margin: "4px 0 0", fontSize: "13px", color: "var(--muted)" }}>
              L0 只读摘要 · 模型离线时全手工开单、扫码查货与复核工作对象正常可用
            </p>
          </div>
          {(capabilities.includes("sales.read") || capabilities.includes("sales.draft")) ? (
            <button
              type="button"
              className={styles.primaryBtn}
              onClick={onCreateQuoteDraft}
            >
              + 新建报价草稿 (L1)
            </button>
          ) : null}
        </div>

        {artifacts.length > 0 ? (
          <div style={{ marginBottom: "16px" }}>
            <h4 style={{ margin: "0 0 8px", fontSize: "14px" }}>最近打开的工作成果（Artifact）</h4>
            <div className={styles.cardGrid}>
              {artifacts.map((art) => (
                <article key={art.id} className={styles.moduleCard}>
                  <div>
                    <div className={styles.panelHeader}>
                      <strong>{art.title}</strong>
                      <StatusBadge
                        kind={
                          art.status === "submitted"
                            ? "verified"
                            : art.status === "unverified"
                              ? "unverified"
                              : "fact"
                        }
                        source={`v${art.version}`}
                      />
                    </div>
                    <p style={{ margin: "6px 0 0", fontSize: "12px", color: "var(--muted)" }}>
                      类型：{art.kind} · 锁定字段：{art.locked_fields.length} 项
                    </p>
                  </div>
                  <button
                    type="button"
                    className={styles.secondaryBtn}
                    onClick={() =>
                      openWorkbenchTab({
                        id: `artifact:${art.id}`,
                        kind: "artifact",
                        title: art.title,
                        objectKind: art.kind,
                        objectId: art.id,
                        artifactId: art.id,
                        closable: true,
                      })
                    }
                  >
                    在工作台打开
                  </button>
                </article>
              ))}
            </div>
          </div>
        ) : null}

        {OBJECT_LIBRARY_CATEGORIES.map((category) => {
          const items = permitted.filter((item) => item.category === category);
          if (items.length === 0) {
            return null;
          }
          return (
            <div key={category} style={{ marginBottom: "12px" }}>
              <h4 style={{ margin: "0 0 8px", fontSize: "13px", color: "var(--ink-eyebrow)" }}>
                {category}
              </h4>
              <div className={styles.cardGrid}>
                {items.map((mod) => (
                  <article key={mod.path} className={styles.moduleCard} data-work-object-card={mod.path}>
                    <div>
                      <div className={styles.panelHeader}>
                        <strong>{mod.title}</strong>
                        <span className={styles.shortcutBadge}>{mod.tier}</span>
                      </div>
                      <p style={{ margin: "6px 0 0", fontSize: "12px", color: "var(--muted)" }}>
                        {mod.summary}
                      </p>
                    </div>
                    <button
                      type="button"
                      className={styles.secondaryBtn}
                      onClick={() => {
                        openWorkbenchTab(toRouteWorkbenchTab(mod));
                        navigate(mod.path);
                      }}
                    >
                      打开标签：{mod.navLabel}
                    </button>
                  </article>
                ))}
              </div>
            </div>
          );
        })}
      </section>
    </div>
  );
}
