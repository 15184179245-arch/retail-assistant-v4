import { ConnectionBanner } from "../app/ConnectionBanner";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Dialog } from "../components/primitives";
import { StatusBadge } from "../components/StatusBadge";
import { useConnectionStatus } from "../lib/api/health";
import {
  createArtifact,
} from "../lib/api/workspace";
import { canAccessRoute } from "../app/navigation";
import { navigate } from "../app/router";
import { ForbiddenPage, renderRouteComponent } from "../app/routes";
import { captureSessionScope, logoutSession, type Session } from "../lib/auth/session";
import { getCapabilityVersion, hasCapability } from "../lib/auth/capabilities";
import { businessPermission } from "../lib/auth/businessPermissions";
import {
  appendArtifactOp,
  attachObjectToComposer,
  bindWorkspaceSubject,
  closeWorkbenchTab,
  confirmDirtyLeavePrompt,
  dismissDirtyLeavePrompt,
  dismissToast,
  getActiveThread,
  getWorkspaceState,
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
  useVerifiedArtifactSummaries,
  useWorkspaceState,
  workspaceTabLabel,
} from "../workspace/store";
import { ArtifactCoeditWorkspace } from "./ArtifactCoeditWorkspace";
import { CommandPalette } from "./CommandPalette";
import { ObjectLibraryView } from "./ObjectLibraryView";
import { AssistantPage } from "../features/assistant/AssistantPage";
import { TodayView } from "./TodayView";
import { getWorkObjectByPath, toRouteWorkbenchTab } from "./workObjects";
import styles from "../styles/workbench.module.css";

export interface AiFirstShellProps {
  session: Session;
  capabilities: string[];
  currentPath: string;
  children?: ReactNode;
  notice?: ReactNode;
  workspaceOnly?: boolean;
}

export function AiFirstShell({
  session,
  capabilities,
  currentPath,
  children,
  notice,
  workspaceOnly = false,
}: AiFirstShellProps) {
  const ws = useWorkspaceState();
  const artifactSummaries = useVerifiedArtifactSummaries(ws);
  const connection = useConnectionStatus();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [submitBoundaryOpen, setSubmitBoundaryOpen] = useState(false);
  const keepDraftButton = useRef<HTMLButtonElement>(null);

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
    if (currentPath === "/assistant") { toggleThreadSidebar(false); setMobilePane("thread"); return; }
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
        if (getWorkspaceState().dirtyLeavePrompt) return;
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
      if (!hasCapability(businessPermission.sales.write)) return;
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

  const activeTab = ws.openTabs.find((t) => t.id === ws.activeTabId) ?? TODAY_TAB;
  const activeThread = getActiveThread();
  const isRouteDirectMismatch = currentPath !== "/" && currentPath !== "/assistant" && currentPath !== "/workspace" && activeTab.id === TODAY_TAB.id;

  const renderWorkbenchContent = () => {
    // Redirects and unknown/forbidden routes must not be swallowed by restored tabs.
    if (currentPath !== "/" && currentPath !== "/assistant" && currentPath !== "/workspace" && !getWorkObjectByPath(currentPath)) return children;
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
          <h3 className={styles.panelTitle}>身份与工作区</h3>
          <p style={{ fontSize: "13px", color: "var(--muted)" }}>
            当前身份：{session.username}（{session.organizationId} / {session.storeId}）· 权限数：
            {capabilities.length}
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", marginTop: "12px" }}>
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
          onOpenSubmitBoundary={() => setSubmitBoundaryOpen(true)}
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
                {currentPath === "/sales" && capabilities.includes(businessPermission.sales.write) ? (
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
        onCreateQuoteDraft={() => void handleCreateQuoteDraft()}
      />
    );
  };

  return (
    <div className={`${styles.shellRoot} ${workspaceOnly ? "od-manual-workspace" : ""}`} data-ai-first-shell="true">
      {!workspaceOnly && <header className={styles.shellBar}>
        <div className={styles.shellBrand}><span className="od-brand-mark" aria-hidden="true">三</span><div><strong>零售智能经营</strong><small>工作区 · {activeThread.title}</small></div><StatusBadge kind="development" source="开发环境" /></div>
        <button type="button" className={styles.commandTrigger} onClick={() => setPaletteOpen(true)} data-command-palette-trigger="true"><span>⌕ 搜索商品、单据或快捷命令</span><kbd>Ctrl K</kbd></button>
        <div className={styles.shellIdentity}><span className={styles.serviceState}>{connection.status === "online" ? "● 服务可用" : "◌ 连接待核"}</span><span>{session.username}<small>{session.storeId}</small></span><button type="button" onClick={() => { void logoutSession().then(() => navigate("/login")); }}>退出登录</button></div>
      </header>}
      {workspaceOnly && <p className="od-manual-note">人工草稿工作区 · 与助理事项独立。保存草稿不等于提交或写入ERP。</p>}
      {notice}
      <ConnectionBanner session={session} />
      {/* Narrow viewport (<1024px) two-pane tab switch */}
      {!workspaceOnly && <div className={styles.mobileModeSwitch} role="tablist" aria-label="移动端双栏切换">
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
          我的助理
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
      </div>}

      {/* 3-Column AI-First Shell Layout */}
      <div
        className={`${styles.gridThreeCol} ${
          ws.threadCollapsed ? styles.gridCollapsedThread : ""
        }`}
      >
        {/* Left Navigation Rail */}
        {!workspaceOnly && <nav className={styles.railNav} aria-label="工作台侧边主导航">
          <h2 className={styles.railHeading}>工作区</h2>
          {(
            [
              { id: "today", label: "◈ 今日" },
              { id: "objects", label: "▦ 对象库" },
              { id: "threads", label: "☷ 工作区" },
              { id: "settings", label: "⚙ 设置" },
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
          <button type="button" className={styles.railButton} onClick={() => { const collapsed = !ws.threadCollapsed; toggleThreadSidebar(collapsed); setMobilePane(collapsed ? "workbench" : "thread"); }} data-toggle-thread-btn="true">{ws.threadCollapsed ? "◧ 展开对话" : "◧ 收起对话"}</button>
        </nav>}

        {/* Collapsible Dialogue Thread Sidebar */}
        {!workspaceOnly && !ws.threadCollapsed ? (
          <div className={`${styles.conversationColumn} ${ws.mobilePane !== "thread" ? styles.hiddenOnMobilePane : ""}`}>
            {(capabilities.includes("agent.run") || capabilities.includes("agents.run")) && capabilities.includes("tasks.read_self")
              ? <AssistantPage session={session} embedded /> : <div className={styles.conversationUnavailable}><strong>我的助理</strong><p>当前身份尚无事项读取权限。右侧已授权的人工工作台仍可使用。</p></div>}
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
              const label = workspaceTabLabel(tab, artifactSummaries);
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
                      selectWorkbenchTab(tab.id, { onActivated: () => {
                        setLeftSection("today");
                        if (tab.id === TODAY_TAB.id) navigate("/");
                        else if (tab.kind === "route" && tab.path) navigate(tab.path);
                      } });
                    }}
                  >
                    {tab.dirty ? <span className={styles.dirtyDot} aria-label="未保存修改" /> : null}{" "}
                    {label}
                  </button>
                  {tab.closable ? (
                    <button
                      type="button"
                      className={styles.tabCloseBtn}
                      aria-label={`关闭标签 ${label}`}
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

      {/* Static guidance only: never retain an artifact or approval state here. */}
      {submitBoundaryOpen ? (
        <Dialog open={true} onClose={() => setSubmitBoundaryOpen(false)}
          title="正式业务提交尚未接入"
          description="当前工作区只支持查看和人工编辑草稿。工作区保存凭据不等于管家婆、库存、采购或销售正式单据回执。"
          footer={<button type="button" className={styles.secondaryBtn} onClick={() => setSubmitBoundaryOpen(false)}>返回核对</button>}>
          <p role="status" data-workspace-submit-unavailable="true">本入口不会发送提交请求，不会改变正式单据或库存。请使用已接入的原业务页面办理。</p>
        </Dialog>
      ) : null}

      {ws.dirtyLeavePrompt ? (
        <Dialog
          open={true}
          role="alertdialog"
          id="workspace-dirty-leave"
          size="sm"
          onClose={dismissDirtyLeavePrompt}
          closeOnBackdrop={false}
          initialFocusRef={keepDraftButton}
          title="存在未保存的草稿编辑"
          description={`您有未提交的本地编辑。确定${ws.dirtyLeavePrompt.targetDescription}吗？`}
        >
          <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "flex-end", gap: "8px" }}>
            <button
              ref={keepDraftButton}
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
        </Dialog>
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
