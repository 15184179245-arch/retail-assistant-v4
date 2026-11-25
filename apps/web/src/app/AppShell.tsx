import type { ReactNode } from "react";
import type { Session } from "../lib/auth/session";
import { logoutSession } from "../lib/auth/session";
import { useCapabilities } from "../lib/auth/capabilities";
import { navigate } from "./router";
import { getVisibleNavRoutes } from "./navigation";
import { ErrorBoundary } from "./ErrorBoundary";
import { ConnectionBanner } from "./ConnectionBanner";
import { AiFirstShell } from "../shell/AiFirstShell";
import { setShellMode, useShellMode } from "../shell/shellMode";

export interface AppShellProps {
  session: Session | null;
  currentPath: string;
  children?: ReactNode;
}

export function AppShell({ session, currentPath, children }: AppShellProps) {
  const { capabilities } = useCapabilities();
  const shellMode = useShellMode();
  const visibleRoutes = session ? getVisibleNavRoutes(capabilities) : [];

  if (session && currentPath === '/assistant') {
    return <main className="assistant-shell"><nav aria-label="助理与工作台切换"><button type="button" className="link-button" onClick={() => navigate("/")}>返回工作台</button></nav><ErrorBoundary>{children}</ErrorBoundary></main>;
  }

  return (
    <main className="workspace">
      <header className="topbar">
        <div className="brand-mark" aria-hidden="true">三</div>
        <div>
          <p className="eyebrow">RETAIL SMART OPS</p>
          <h1>智能经营工作台</h1>
        </div>
        {session ? (
          <div className="identity">
            <span>
              {session.username} · {session.organizationId} / {session.storeId}
            </span>
            <button
              className="link-button"
              type="button"
              onClick={() => {
                void logoutSession().then(() => {
                  navigate("/login");
                });
              }}
            >
              退出登录
            </button>
          </div>
        ) : null}
        <span className="dev-badge">开发版</span>
      </header>

      <ConnectionBanner session={session} />

      {session ? (
        <nav className="workspace-nav" aria-label="工作台导航">
          {visibleRoutes.map((route) => {
            const isActive = currentPath === route.path;
            return (
              <button
                key={route.path}
                type="button"
                className={isActive ? "nav-active" : ""}
                aria-current={isActive ? "page" : undefined}
                onClick={() => navigate(route.path)}
              >
                {route.navLabel || route.title}
              </button>
            );
          })}
          {shellMode === "legacy" ? <button type="button" onClick={() => setShellMode("ai")} data-return-ai-shell="true">回到 AI 工作台</button> : null}
        </nav>
      ) : null}

      <div className="workspace-content">
        <ErrorBoundary>
          {session && shellMode === "ai" ? <AiFirstShell session={session} capabilities={capabilities} currentPath={currentPath}>{children}</AiFirstShell> : children}
        </ErrorBoundary>
      </div>

      <footer>零售门店智能经营系统 · 开发基线</footer>
    </main>
  );
}
