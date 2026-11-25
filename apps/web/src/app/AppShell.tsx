import type { ReactNode } from "react";
import type { Session } from "../lib/auth/session";
import { logoutSession } from "../lib/auth/session";
import { refreshCapabilities, useCapabilities } from "../lib/auth/capabilities";
import { canUseQuoteWorkspace } from "../lib/auth/businessPermissions";
import { getPermittedWorkObjects } from "../shell/workObjects";
import { canAccessRoute } from "./navigation";
import { navigate } from "./router";
import { ErrorBoundary } from "./ErrorBoundary";
import { ConnectionBanner } from "./ConnectionBanner";
import { AiFirstShell } from "../shell/AiFirstShell";
import { AssistantPage } from "../features/assistant/AssistantPage";
import { isAssistantPath } from "../features/assistant/assistantRouteVisibility";
import "../styles/open-design-shell.css";

export interface AppShellProps { session: Session | null; currentPath: string; children?: ReactNode }

/** A destination is not a second conversation. Identity/scope still owns this whole frame. */
export function AppShell({ session, currentPath, children }: AppShellProps) {
  const { capabilities, status } = useCapabilities();
  const workActive = isAssistantPath(currentPath);
  const workspaceActive = currentPath === "/workspace";
  const canReadWork = status === "ready" && (capabilities.includes("agent.run") || capabilities.includes("agents.run")) && capabilities.includes("tasks.read_self");
  const businessModules = status === "ready"
    ? getPermittedWorkObjects(capabilities).filter((m) => !["/", "/assistant", "/pi-trial", "/pi-probe"].includes(m.path))
    : [];
  const canOpenDrafts = status === "ready" && canUseQuoteWorkspace(capabilities);
  const notice = session && status !== "ready" ? <aside className="od-permission-notice" role="status" aria-label="权限核查状态">
    <span>{status === "loading" ? "正在核查当前身份权限…" : "权限尚未核实，登录保持有效，业务能力暂未开放。"}</span>
    <button type="button" disabled={status === "loading"} onClick={() => { void refreshCapabilities(); }}>重试权限核查</button>
  </aside> : null;
  const content = session && status !== "ready" && canAccessRoute(currentPath, [], true).reason === "forbidden"
    ? <p role="status">权限尚未核实，此模块暂未打开；请重试权限核查。</p> : children;
  if (!session) return <main className="od-auth"><header><span className="od-brand-mark" aria-hidden="true">三</span><strong>零售智能经营</strong><span>员工工作台</span></header>
    <div className="od-auth-body"><div className="od-auth-intro"><p>找商品，交代工作，查看结果。</p><h1>围绕一件事，<br/>把工作做清楚。</h1><span>商品目录可直接查询。交给助理的工作会保存，结果、来源与执行过程可以随时回看。</span></div>
      <ErrorBoundary>{content}</ErrorBoundary></div></main>;
  return <main className="od-core" data-core-workspace="true">
    <header className="od-core-header"><a className="od-core-brand" href="/assistant" onClick={e=>{e.preventDefault();navigate('/assistant');}}><span className="od-brand-mark" aria-hidden="true">三</span><strong>零售智能经营</strong></a>
      <nav className="od-core-nav" aria-label="主要功能">
        <a href="/assistant" aria-current={workActive ? "page" : undefined} onClick={e=>{e.preventDefault();navigate('/assistant');}}>助理事项</a>
        <a href="/catalog" aria-current={currentPath === '/catalog' ? "page" : undefined} onClick={e=>{e.preventDefault();navigate('/catalog');}}>商品查询</a>
        <a href="/business" aria-current={!workActive && currentPath !== '/catalog' ? "page" : undefined} onClick={e=>{e.preventDefault();navigate('/business');}}>人工业务</a>
      </nav><div className="od-core-account"><span className="od-core-identity">
        <span className="od-core-user-name">{session.username}</span>
        <small title={session.storeId}>当前门店：<span>{session.storeId}</span></small>
      </span><button type="button" onClick={()=>{void logoutSession().then(()=>navigate('/login'));}}>退出登录</button></div>
    </header>
    {notice}
    {/* One instance survives destination changes; hidden removes keyboard and screen-reader exposure. */}
    {canReadWork && <section className="od-core-work" hidden={!workActive} aria-label="助理事项"><ErrorBoundary><AssistantPage session={session} framed /></ErrorBoundary></section>}
    {workActive && !canReadWork && <section className="od-core-empty"><h1>从一件事开始</h1><p>{status === 'ready' ? '当前身份尚无事项读取权限。你仍可使用已授权的商品查询与人工业务。' : '先核查身份权限，再开放对应工作。'}</p><button type="button" onClick={()=>navigate('/catalog')}>查询商品目录</button></section>}
    {/* Restored tabs must not be pruned or synced against pending permissions. */}
    {workspaceActive && status === "ready" && (
      <section className="od-core-manual" hidden={!workspaceActive} aria-label="独立人工草稿工作区">
        <div className="od-manual-subnav">
          <button type="button" onClick={() => navigate("/business")}>← 返回人工业务</button>
          {canReadWork && <button type="button" onClick={() => navigate("/assistant")}>助理事项</button>}
        </div>
        <ErrorBoundary><AiFirstShell session={session} capabilities={capabilities} currentPath="/workspace" workspaceOnly /></ErrorBoundary>
      </section>
    )}
    {!workActive && !workspaceActive && (
      <section className="od-core-business" data-business-route={currentPath} aria-label="当前业务页面">
        {status === "ready" && (businessModules.length > 0 || canOpenDrafts) && (
          <nav className="od-business-subnav" aria-label="业务模块快捷切换">
            {currentPath !== "/business" && (
              <a className="od-subnav-hub" href="/business" onClick={e => { e.preventDefault(); navigate("/business"); }}>← 业务总览</a>
            )}
            {businessModules.map(m => (
              <a
                key={m.path}
                href={m.path}
                className="od-subnav-pill"
                aria-current={currentPath === m.path ? "page" : undefined}
                onClick={e => { e.preventDefault(); navigate(m.path); }}
              >
                {m.navLabel}
              </a>
            ))}
            {canOpenDrafts && (
              <a
                href="/workspace"
                className="od-subnav-pill"
                aria-current={currentPath === "/workspace" ? "page" : undefined}
                onClick={e => { e.preventDefault(); navigate("/workspace"); }}
              >
                草稿工作区
              </a>
            )}
          </nav>
        )}
        <ConnectionBanner session={session}/>
        <ErrorBoundary>{content}</ErrorBoundary>
      </section>
    )}
  </main>;
}
