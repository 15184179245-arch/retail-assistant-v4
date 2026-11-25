import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { ValidatedUiBlock } from "../blocks";
import { Dialog } from "../components/primitives";
import { StatusBadge } from "../components/StatusBadge";
import { useConnectionStatus } from "../lib/api/health";
import { generateIdempotencyKey } from "../lib/api/idempotency";
import {
  createArtifact,
} from "../lib/api/workspace";
import { canAccessRoute } from "../app/navigation";
import { navigate } from "../app/router";
import { ForbiddenPage, renderRouteComponent } from "../app/routes";
import { captureSessionScope, type Session } from "../lib/auth/session";
import { getCapabilityVersion } from "../lib/auth/capabilities";
import type { WorkspaceArtifactDto } from "../lib/api/generated";
import {
  appendArtifactOp,
  attachObjectToComposer,
  bindWorkspaceSubject,
  closeWorkbenchTab,
  confirmDirtyLeavePrompt,
  dismissDirtyLeavePrompt,
  dismissToast,
  getActiveThread,
  hasAnyDirtyTab,
  hydrateWorkspaceFromUrl,
  openWorkbenchTab,
  pushToast,
  resetWorkspaceStore,
  selectOrCreateThread,
  selectWorkbenchTab,
  setLeftSection,
  setMobilePane,
  startWorkspaceEventStream,
  toggleThreadSidebar,
  TODAY_TAB,
  upsertArtifact,
  useWorkspaceState,
} from "../workspace/store";
import { ArtifactCoeditWorkspace } from "./ArtifactCoeditWorkspace";
import { CommandPalette } from "./CommandPalette";
import { ObjectLibraryView } from "./ObjectLibraryView";
import { setShellMode } from "./shellMode";
import { ThreadSidebar } from "./ThreadSidebar";
import { TodayView } from "./TodayView";
import { getWorkObjectByPath, toRouteWorkbenchTab } from "./workObjects";
import styles from "../styles/workbench.module.css";

export interface AiFirstShellProps {
  session: Session;
  capabilities: string[];
  currentPath: string;
  children?: ReactNode;
}

export function AiFirstShell({
  session,
  capabilities,
  currentPath,
  children,
}: AiFirstShellProps) {
  const ws = useWorkspaceState();
  const connection = useConnectionStatus();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [confirmState, setConfirmState] = useState<{
    artifact: WorkspaceArtifactDto;
    idempotencyKey: string;
  } | null>(null);

  const modelOffline =
    connection.status === "model_offline" ||
    connection.status === "offline" ||
    !connection.isAgentPermitted;

  useEffect(() => {
    bindWorkspaceSubject(session.subjectId);
    hydrateWorkspaceFromUrl();
    let stopStream: (() => void) | null = null;
    const timer = setTimeout(() => {
      stopStream = startWorkspaceEventStream();
    }, 50);
    return () => {
      clearTimeout(timer);
      stopStream?.();
    };
  }, [session.subjectId]);

  useEffect(() => {
    if (getCapabilityVersion() === "v0-bootstrap") {
      return;
    }
    for (const tab of ws.openTabs) {
      if (tab.kind === "route" && tab.path && !canAccessRoute(tab.path, capabilities, true).allowed) {
        closeWorkbenchTab(tab.id, { force: true });
      }
    }
  }, [capabilities]);

  useEffect(() => {
    if (currentPath === "/") {
      const currentTab = ws.openTabs.find((t) => t.id === ws.activeTabId);
      if (currentTab?.kind === "route") {
        selectWorkbenchTab("today");
      }
      return;
    }
    const mod = getWorkObjectByPath(currentPath);
    if (mod && canAccessRoute(currentPath, capabilities, true).allowed) {
      openWorkbenchTab(toRouteWorkbenchTab(mod), { activate: true, syncBackend: true });
    }
  }, [currentPath]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((prev) => !prev);
      }
    };
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if (hasAnyDirtyTab()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, []);

  const handleCreateQuoteDraft = useCallback(
    async (prefill?: { sku?: string; name?: string; unitPrice?: string }) => {
      const isCurrent = captureSessionScope();
      const activeThread = getActiveThread();
      const title = prefill?.name
        ? `报价草稿：${prefill.name}`
        : `门店报价草稿 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
      const payload: Record<string, unknown> = {
        stage: "draft",
        status: "pending",
        customer_name: "门店散客",
        sku: prefill?.sku ?? "",
        product_name: prefill?.name ?? "",
        qty: 1,
        unit_price: prefill?.unitPrice ?? "",
        note: "",
      };

      try {
        const res = await createArtifact({
          thread_id: activeThread.id,
          kind: "quote_draft",
          title,
          payload,
          source_refs: prefill?.sku
            ? [{ kind: "product", id: prefill.sku, name: prefill.name ?? prefill.sku }]
            : [],
          actor_type: "human",
        });
        if (!isCurrent()) return;
        upsertArtifact(res.artifact, { openTab: true });
        appendArtifactOp(res.artifact.id, res.op);
      } catch {
        if (!isCurrent()) return;
        pushToast("草稿创建结果未确认，请先刷新工作区核查。没有创建本地替身，也不会自动重发。", "warning");
        return;
      }
      setLeftSection("today");
      setMobilePane("workbench");
    },
    [session.organizationId, session.storeId, session.subjectId],
  );

  const openL2ConfirmForArtifact = useCallback((artifact: WorkspaceArtifactDto) => {
    setConfirmState({
      artifact,
      idempotencyKey: generateIdempotencyKey(),
    });
  }, []);

  const handleTriggerActionCard = useCallback(
    (block: ValidatedUiBlock) => {
      const targetId =
        typeof block.props.target_artifact_id === "string"
          ? block.props.target_artifact_id
          : typeof block.props.artifact_id === "string"
            ? block.props.artifact_id
            : block.refs.find((r) => r.kind === "artifact")?.id;
      const artifact = targetId ? ws.artifactsById[targetId] : undefined;
      if (artifact) {
        openL2ConfirmForArtifact(artifact);
      } else {
        pushToast("请先固定或打开对应的 L1 工作成果后再确认提交。", "warning");
      }
    },
    [openL2ConfirmForArtifact, ws.artifactsById],
  );

  const activeTab = ws.openTabs.find((t) => t.id === ws.activeTabId) ?? TODAY_TAB;
  const activeThread = getActiveThread();
  const isRouteDirectMismatch = currentPath !== "/" && activeTab.id === TODAY_TAB.id;

  const renderWorkbenchContent = () => {
    if (ws.leftSection === "objects") {
      return <ObjectLibraryView capabilities={capabilities} />;
    }

    if (ws.leftSection === "threads") {
      return (
        <section className={styles.tabMainCard} data-threads-archive="true">
          <div className={styles.panelHeader} style={{ marginBottom: "12px" }}>
            <h3 className={styles.panelTitle}>历史与当前对话线程</h3>
            <button
              type="button"
              className={styles.primaryBtn}
              onClick={() => {
                const newId = `thread-${Date.now().toString(36)}`;
                selectOrCreateThread(newId, `新会话 ${ws.threads.length + 1}`);
                setLeftSection("today");
              }}
            >
              + 新建对话线程
            </button>
          </div>
          <div className={styles.cardGrid}>
            {ws.threads.map((thr) => (
              <article key={thr.id} className={styles.moduleCard}>
                <div>
                  <strong>{thr.title}</strong>
                  <p style={{ margin: "4px 0 0", fontSize: "12px", color: "var(--muted)" }}>
                    消息：{thr.messages.length} · 绑定对象：{thr.openObjects.length} · 流：
                    {thr.streamState}
                  </p>
                </div>
                <button
                  type="button"
                  className={styles.secondaryBtn}
                  onClick={() => {
                    selectOrCreateThread(thr.id);
                    setLeftSection("today");
                  }}
                >
                  {thr.id === activeThread.id ? "当前激活" : "切换至此线程"}
                </button>
              </article>
            ))}
          </div>
        </section>
      );
    }

    if (ws.leftSection === "settings") {
      return (
        <section className={styles.tabMainCard} data-shell-settings="true">
          <h3 className={styles.panelTitle}>工作台设置与壳层回滚开关</h3>
          <p style={{ fontSize: "13px", color: "var(--muted)" }}>
            当前身份：{session.username}（{session.organizationId} / {session.storeId}）· 权限数：
            {capabilities.length}
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", marginTop: "12px" }}>
            <button
              type="button"
              className={styles.secondaryBtn}
              onClick={() => setShellMode("legacy")}
              data-switch-legacy-shell="true"
            >
              切换回经典单页壳层 (?shell=legacy)
            </button>
            <button
              type="button"
              className={styles.secondaryBtn}
              onClick={() => {
                resetWorkspaceStore();
                navigate("/");
              }}
            >
              重置全部工作台标签
            </button>
          </div>
        </section>
      );
    }

    if (activeTab.kind === "artifact" && activeTab.artifactId) {
      return (
        <ArtifactCoeditWorkspace
          key={JSON.stringify([ws.activeThreadId, activeTab.artifactId])}
          artifactId={activeTab.artifactId}
          tabId={activeTab.id}
          session={session}
          onRequestL2Submit={openL2ConfirmForArtifact}
        />
      );
    }

    if (isRouteDirectMismatch || (activeTab.kind === "route" && activeTab.path === currentPath)) {
      const desc = getWorkObjectByPath(currentPath);
      return (
        <div className={styles.tabMainCard} data-work-object-route={currentPath}>
          {desc ? (
            <div className={styles.panelHeader} style={{ marginBottom: "12px" }}>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                <span className={styles.shortcutBadge}>{desc.category}</span>
                <span className={styles.shortcutBadge}>{desc.tier}</span>
                <span style={{ fontSize: "12px", color: "var(--muted)" }}>
                  {desc.offlineAvailable ? "离线/模型故障可用" : "需模型在线推理"}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                {(currentPath === "/sales" || currentPath === "/purchasing") ? (
                  <button
                    type="button"
                    className={styles.primaryBtn}
                    onClick={() => void handleCreateQuoteDraft()}
                  >
                    + 在工作台新建 L1 草稿
                  </button>
                ) : null}
                <button
                  type="button"
                  className={styles.secondaryBtn}
                  onClick={() =>
                    attachObjectToComposer({
                      kind: desc.objectKind,
                      id: desc.path,
                      title: desc.title,
                      path: desc.path,
                    })
                  }
                >
                  + @引用到对话
                </button>
              </div>
            </div>
          ) : null}
          {children}
        </div>
      );
    }

    if (activeTab.kind === "route" && activeTab.path) {
      const targetPath = activeTab.path;
      const access = canAccessRoute(targetPath, capabilities, true);
      const desc = getWorkObjectByPath(targetPath);
      if (!access.allowed) {
        return (
          <div className={styles.tabMainCard}>
            <ForbiddenPage path={targetPath} requiredCapability={desc?.requiredCapability} />
          </div>
        );
      }
      return (
        <div className={styles.tabMainCard} data-work-object-route={targetPath}>
          {renderRouteComponent(targetPath, session)}
        </div>
      );
    }

    return (
      <TodayView
        session={session}
        capabilities={capabilities}
        homeContent={currentPath === "/" ? children : null}
        onCreateQuoteDraft={() => void handleCreateQuoteDraft()}
      />
    );
  };

  return (
    <div className={styles.shellRoot} data-ai-first-shell="true">
      {/* Top command / collapse utility strip */}
      <div className={styles.shellBar}>
        <div className={styles.shellBarGroup}>
          <button
            type="button"
            className={styles.commandTrigger}
            onClick={() => setPaletteOpen(true)}
            data-command-palette-trigger="true"
          >
            <span>搜索或快捷命令…</span>
            <span className={styles.shortcutBadge}>Ctrl+K</span>
          </button>
          <button
            type="button"
            className={styles.modeButton}
            onClick={() => toggleThreadSidebar()}
            data-toggle-thread-btn="true"
          >
            {ws.threadCollapsed ? "展开对话线程" : "收起对话线程"}
          </button>
        </div>

        <div className={styles.shellBarGroup}>
          <StatusBadge
            kind={modelOffline ? "waiting" : "fact"}
            source={modelOffline ? "模型离线" : `AI 工作台 · 零售智能经营`}
          />
          <button
            type="button"
            className={styles.modeButton}
            onClick={() => setShellMode("legacy")}
            data-header-legacy-toggle="true"
          >
            经典视图
          </button>
        </div>
      </div>

      {/* Narrow viewport (<1024px) two-pane tab switch */}
      <div className={styles.mobileModeSwitch} role="tablist" aria-label="移动端双栏切换">
        <button
          type="button"
          role="tab"
          aria-selected={ws.mobilePane === "thread"}
          className={`${styles.mobileModeTab} ${
            ws.mobilePane === "thread" ? styles.mobileModeTabActive : ""
          }`}
          onClick={() => setMobilePane("thread")}
          data-mobile-tab="thread"
        >
          对话 ({activeThread.messages.length})
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={ws.mobilePane === "workbench"}
          className={`${styles.mobileModeTab} ${
            ws.mobilePane === "workbench" ? styles.mobileModeTabActive : ""
          }`}
          onClick={() => setMobilePane("workbench")}
          data-mobile-tab="workbench"
        >
          工作台 ({ws.openTabs.length})
        </button>
      </div>

      {/* 3-Column AI-First Shell Layout */}
      <div
        className={`${styles.gridThreeCol} ${
          ws.threadCollapsed ? styles.gridCollapsedThread : ""
        }`}
      >
        {/* Left Navigation Rail */}
        <nav className={styles.railNav} aria-label="工作台侧边主导航">
          <h2 className={styles.railHeading}>工作区</h2>
          {(
            [
              { id: "today", label: "Today 今日" },
              { id: "objects", label: "对象库 (5类)" },
              { id: "threads", label: `线程 (${ws.threads.length})` },
              { id: "settings", label: "设置 / 模式" },
            ] as const
          ).map((nav) => (
            <button
              key={nav.id}
              type="button"
              className={`${styles.railButton} ${
                ws.leftSection === nav.id ? styles.railButtonActive : ""
              }`}
              onClick={() => {
                setLeftSection(nav.id);
                if (nav.id === "today" && activeTab.id !== TODAY_TAB.id) {
                  setMobilePane("workbench");
                }
              }}
              data-rail-item={nav.id}
            >
              <span>{nav.label}</span>
            </button>
          ))}
        </nav>

        {/* Collapsible Dialogue Thread Sidebar */}
        {!ws.threadCollapsed ? (
          <div className={ws.mobilePane !== "thread" ? styles.hiddenOnMobilePane : ""}>
            <ThreadSidebar
              modelOffline={modelOffline}
              onTriggerL2ActionCard={handleTriggerActionCard}
            />
          </div>
        ) : null}

        {/* Multi-Tab Workbench Main Pane */}
        <section
          className={`${styles.workbenchColumn} ${
            ws.mobilePane !== "workbench" ? styles.hiddenOnMobilePane : ""
          }`}
          aria-label="多标签工作对象工作台"
          data-workbench-pane="true"
        >
          <div className={styles.tabBar} role="tablist" aria-label="工作台多标签页">
            {ws.openTabs.map((tab) => {
              const isActive =
                isRouteDirectMismatch ? tab.path === currentPath : tab.id === activeTab.id;
              return (
                <div
                  key={tab.id}
                  className={`${styles.tabPill} ${isActive ? styles.tabPillActive : ""}`}
                  data-workbench-tab={tab.id}
                >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={isActive}
                    className={styles.tabSelectBtn}
                    onClick={() => {
                      setLeftSection("today");
                      const ok = selectWorkbenchTab(tab.id);
                      if (ok && tab.id === TODAY_TAB.id && currentPath !== "/") {
                        navigate("/");
                      } else if (ok && tab.kind === "route" && tab.path && tab.path !== currentPath) {
                        navigate(tab.path);
                      }
                    }}
                  >
                    {tab.dirty ? <span className={styles.dirtyDot} aria-label="未保存修改" /> : null}{" "}
                    {tab.title}
                  </button>
                  {tab.closable ? (
                    <button
                      type="button"
                      className={styles.tabCloseBtn}
                      aria-label={`关闭标签 ${tab.title}`}
                      onClick={() => {
                        const closed = closeWorkbenchTab(tab.id);
                        if (closed && tab.path === currentPath) {
                          navigate("/");
                        }
                      }}
                    >
                      ×
                    </button>
                  ) : null}
                </div>
              );
            })}
          </div>

          {renderWorkbenchContent()}
        </section>
      </div>

      {/* Ctrl+K Command Palette */}
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        capabilities={capabilities}
        modelOffline={modelOffline}
        onCreateQuoteDraft={(prefill) => void handleCreateQuoteDraft(prefill)}
      />

      {/* No business adapter: retain an explanatory boundary, not a fake confirmation. */}
      {confirmState ? (
        <Dialog open={true} onClose={() => setConfirmState(null)}
          title="正式业务提交尚未接入"
          description="当前工作区只支持查看和人工编辑草稿。工作区保存凭据不等于管家婆、库存、采购或销售正式单据回执。"
          footer={<button type="button" className={styles.secondaryBtn} onClick={() => setConfirmState(null)}>返回核对</button>}>
          <p>当前对象：{confirmState.artifact.title} · v{confirmState.artifact.version}</p>
          <p role="status" data-workspace-submit-unavailable="true">本入口不会发送提交请求，不会改变正式单据或库存。请使用已接入的原业务页面办理。</p>
        </Dialog>
      ) : null}

      {/* Dirty Tab Leave Confirmation Modal */}
      {ws.dirtyLeavePrompt ? (
        <div className={styles.paletteBackdrop} role="presentation">
          <div
            className={styles.dirtyDialog}
            role="alertdialog"
            aria-modal="true"
            aria-label="未保存修改确认"
            data-dirty-leave-dialog="true"
          >
            <h3 className={styles.panelTitle}>存在未保存的草稿编辑</h3>
            <p style={{ margin: 0, fontSize: "14px", color: "var(--ink-secondary)" }}>
              您有未提交的本地编辑。确定{ws.dirtyLeavePrompt.targetDescription}吗？
            </p>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
              <button
                type="button"
                className={styles.secondaryBtn}
                onClick={dismissDirtyLeavePrompt}
              >
                留在此标签
              </button>
              <button
                type="button"
                className={styles.primaryBtn}
                onClick={confirmDirtyLeavePrompt}
                data-confirm-dirty-leave="true"
              >
                放弃未保存修改并切换
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* Cross-device active editor / status toasts */}
      {ws.toasts.length > 0 ? (
        <div className={styles.toastStack} aria-live="polite" data-workspace-toasts="true">
          {ws.toasts.map((toast) => (
            <div key={toast.id} className={styles.toastBanner} role="status">
              <span>{toast.message}</span>
              <button
                type="button"
                className={styles.chipAction}
                aria-label="关闭提示"
                onClick={() => dismissToast(toast.id)}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
