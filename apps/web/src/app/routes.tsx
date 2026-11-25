import { BusinessHome } from "./BusinessHome";
import { useEffect, type ReactNode } from "react";
import type { Session } from "../lib/auth/session";
import { getScopeVersion } from "../lib/auth/session";
import { businessPermission } from "../lib/auth/businessPermissions";
import { EmptyState } from "../components/EmptyState";
import { Button } from "../components/Button";
import { navigate } from "./router";
import { PiTrialPage } from "../features/agents/PiTrialPage";
import { PiProbePage } from "../features/agents/PiProbePage";
import { MemoryPage } from "../features/memory/MemoryPage";
import { AssistantPage } from "../features/assistant/AssistantPage";
import { KnowledgeReviewPage } from "../features/knowledge/KnowledgeReviewPage";
import { PurchaseIntentPage } from "../features/purchasing/PurchaseIntentPage";
import { StocktakeReviewPage } from "../features/stocktake/StocktakeReviewPage";
import { NotificationInbox } from "../features/tasks/NotificationInbox";
import { OFC002Page } from "../features/office/OFC002Page";
import { OFC005Page } from "../features/office/OFC005Page";
import { OFC007Page } from "../features/office/OFC007Page";
import { AnalyticsPage } from "../features/analytics/AnalyticsPage";
import { ReceivingReviewPage } from "../features/receiving/ReceivingReviewPage";
import { CatalogSearchPage } from "../features/catalog/CatalogSearchPage";
import { SalesWorkbenchPage } from "../features/sales/SalesWorkbenchPage";

function RetiredAgentRoute() {
  useEffect(() => { navigate("/assistant", { replace: true }); }, []);
  return <p role="status">旧版 Agent 入口已移除，正在前往「我的助理」。</p>;
}

export interface RouteItem {
  path: string;
  title: string;
  navLabel?: string;
  inNav: boolean;
  requiredCapability?: string;
  objectCategory?: string;
  workObjectKind?: string;
  executionTier?: "L0" | "L1" | "L2" | "thread";
}

export function WelcomeHome({ session }: { session: Session }) {
  return (
    <section className="welcome-card" aria-labelledby="welcome-title">
      <p className="eyebrow">管理端 · 已登录</p>
      <h2 id="welcome-title">工作台已就绪</h2>
      <p className="welcome-copy">
        当前身份：{session.subjectId}。业务模块将在后续批次按已确认的契约逐步接入。
      </p>
      <div className="status-row">
        <span className="status-dot" aria-hidden="true" />
        <span>开发环境</span>
      </div>
    </section>
  );
}

export function ForbiddenPage({
  path,
  requiredCapability,
}: {
  path: string;
  requiredCapability?: string;
}) {
  return (
    <div style={{ maxWidth: 600, margin: "48px auto", padding: "0 16px" }} data-forbidden-page>
      <EmptyState
        type="forbidden"
        title="无权访问此工作区"
        description={`您当前的员工账号缺少访问路径 '${path}' 所需的功能权限${
          requiredCapability ? `（需要能力: ${requiredCapability}）` : ""
        }。请联系管理员开通后重新进入。`}
        action={
          <Button variant="secondary" onClick={() => navigate("/")}>
            返回工作台首页
          </Button>
        }
      />
    </div>
  );
}

export const NAV_ROUTES: RouteItem[] = [
  { path: "/business", title: "人工业务", inNav: false, requiredCapability: "authenticated" },
  { path: "/workspace", title: "人工草稿工作区", inNav: false, requiredCapability: "authenticated" },
  { path: "/", title: "工作台首页", navLabel: "工作台", inNav: true, requiredCapability: "authenticated", objectCategory: "今日总览", workObjectKind: "today", executionTier: "L0" },
  { path: "/catalog", title: "商品与库存查询", navLabel: "商品查询", inNav: true, requiredCapability: "catalog.read", objectCategory: "销售与客户", workObjectKind: "catalog_lookup", executionTier: "L0" },
  { path: "/sales", title: "销售草稿与交接复核", navLabel: "销售复核", inNav: true, requiredCapability: businessPermission.sales.read, objectCategory: "销售与客户", workObjectKind: "sales_workbench", executionTier: "L1" },
  { path: "/assistant", title: "我的助理", navLabel: "我的助理", inNav: true, requiredCapability: "agent.run", objectCategory: "系统与线程", workObjectKind: "assistant_work", executionTier: "thread" },
  { path: "/agents", title: "旧版入口重定向", inNav: false, requiredCapability: "authenticated" },
  { path: "/pi-trial", title: "Pi 只读试用", inNav: false, requiredCapability: "agent.run", objectCategory: "系统与线程", workObjectKind: "pi_trial", executionTier: "thread" },
  { path: "/pi-probe", title: "Pi 单次诊断", inNav: false, requiredCapability: "agent.run", objectCategory: "系统与线程", workObjectKind: "pi_probe", executionTier: "thread" },
  { path: "/memory", title: "助理记忆", navLabel: "助理记忆", inNav: true, requiredCapability: "memory.read", objectCategory: "系统与线程", workObjectKind: "assistant_memory", executionTier: "L0" },
  { path: "/knowledge", title: "知识复核", navLabel: "知识复核", inNav: true, requiredCapability: businessPermission.knowledge.review, objectCategory: "办公与异常", workObjectKind: "knowledge_review", executionTier: "L2" },
  { path: "/purchasing", title: "待到货", navLabel: "待到货", inNav: true, requiredCapability: businessPermission.purchasing.read, objectCategory: "采购与到货", workObjectKind: "purchasing_queue", executionTier: "L0" },
  { path: "/stocktake", title: "盘点证据与复核", navLabel: "盘点证据与复核", inNav: true, requiredCapability: businessPermission.stocktake.read, objectCategory: "库存与盘点", workObjectKind: "stocktake_session", executionTier: "L2" },
  { path: "/notifications", title: "站内提醒", navLabel: "站内提醒", inNav: true, requiredCapability: businessPermission.notifications.read, objectCategory: "办公与异常", workObjectKind: "notification_inbox", executionTier: "L0" },
  { path: "/materials", title: "材料收件箱", navLabel: "材料收件箱", inNav: true, requiredCapability: businessPermission.materials.read, objectCategory: "办公与异常", workObjectKind: "office_material", executionTier: "L2" },
  { path: "/documents", title: "办公文档", navLabel: "办公文档", inNav: true, requiredCapability: "office.documents.read", objectCategory: "办公与异常", workObjectKind: "office_document", executionTier: "L2" },
  { path: "/exceptions", title: "异常与交接", navLabel: "异常与交接", inNav: true, requiredCapability: "office.exceptions.read", objectCategory: "办公与异常", workObjectKind: "office_exception", executionTier: "L2" },
  { path: "/analytics", title: "经营与销售数据分析", navLabel: "经营分析", inNav: true, requiredCapability: "analytics.read", objectCategory: "系统与线程", workObjectKind: "analytics_report", executionTier: "L0" },
  { path: "/receiving", title: "采购到货清点复核", navLabel: "到货复核", inNav: true, requiredCapability: businessPermission.receiving.read, objectCategory: "采购与到货", workObjectKind: "receiving_batch", executionTier: "L2" },
];

export function renderRouteComponent(path: string, session: Session): ReactNode | null {
  const scopeKey = getScopeVersion();
  switch (path) {
    case "/business": return <BusinessHome />;
    case "/workspace": return null;
    case "/":
    case "/login":
      return <WelcomeHome session={session} />;
    case "/catalog":
      return <CatalogSearchPage session={session} />;
    case "/sales":
      return <SalesWorkbenchPage key={scopeKey} session={session} />;
    case "/agents":
      return <RetiredAgentRoute />;
    case "/assistant":
      return <AssistantPage key={scopeKey} session={session} />;
    case "/pi-trial":
      return <PiTrialPage key={scopeKey} session={session} />;
    case "/pi-probe":
      return <PiProbePage key={scopeKey} session={session} />;
    case "/memory":
      return <MemoryPage key={scopeKey} session={session} />;
    case "/knowledge":
      return <KnowledgeReviewPage key={scopeKey} session={session} />;
    case "/purchasing":
      return <PurchaseIntentPage key={scopeKey} session={session} />;
    case "/stocktake":
      return <StocktakeReviewPage key={scopeKey} session={session} />;
    case "/notifications":
      return <NotificationInbox key={scopeKey} session={session} />;
    case "/materials":
      return <OFC002Page key={scopeKey} session={session} />;
    case "/documents":
      return <OFC005Page key={scopeKey} session={session} />;
    case "/exceptions":
      return <OFC007Page key={scopeKey} session={session} />;
    case "/analytics":
      return <AnalyticsPage session={session} />;
    case "/receiving":
      return <ReceivingReviewPage session={session} />;
    default:
      return null;
  }
}
