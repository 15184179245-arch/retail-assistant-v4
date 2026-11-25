import { isRouteVisible } from "../app/navigation";
import { openWorkbenchTab, type WorkbenchTab } from "../workspace/store";

export type WorkObjectCategory =
  | "今日总览"
  | "销售与客户"
  | "采购与到货"
  | "库存与盘点"
  | "办公与异常"
  | "系统与线程";

export type WorkObjectTier = "L0" | "L1" | "L2" | "thread";

export interface WorkObjectModuleDescriptor {
  path: string;
  objectKind: string;
  title: string;
  navLabel: string;
  category: WorkObjectCategory;
  tier: WorkObjectTier;
  requiredCapability: string;
  summary: string;
  offlineAvailable: boolean;
  inLibrary?: boolean;
}

export const WORK_OBJECT_MODULES: WorkObjectModuleDescriptor[] = [
  {
    path: "/",
    objectKind: "today",
    title: "工作台首页",
    navLabel: "今日",
    category: "今日总览",
    tier: "L0",
    requiredCapability: "authenticated",
    summary: "跨端待我处理队列投影、最近打开对象与日常作业入口",
    offlineAvailable: true,
  },
  {
    path: "/catalog",
    objectKind: "catalog_lookup",
    title: "商品与库存查询",
    navLabel: "商品查询",
    category: "销售与客户",
    tier: "L0",
    requiredCapability: "catalog.read",
    summary: "只读商品资料、别名对照、库位分布与可售口径",
    offlineAvailable: true,
  },
  {
    path: "/sales",
    objectKind: "sales_workbench",
    title: "销售草稿与交接复核",
    navLabel: "销售复核",
    category: "销售与客户",
    tier: "L1",
    requiredCapability: "sales.read",
    summary: "L1 报价草稿共编、开单交接核验与正式结算确认",
    offlineAvailable: true,
  },
  {
    path: "/purchasing",
    objectKind: "purchasing_queue",
    title: "待到货清单",
    navLabel: "待到货",
    category: "采购与到货",
    tier: "L0",
    requiredCapability: "purchasing.read",
    summary: "订货意向、在途待到货汇总与供应商采购跟踪",
    offlineAvailable: true,
  },
  {
    path: "/receiving",
    objectKind: "receiving_batch",
    title: "采购到货清点复核",
    navLabel: "到货复核",
    category: "采购与到货",
    tier: "L2",
    requiredCapability: "receiving.read",
    summary: "实收数量与差异核对；正式入库不由工作区草稿完成",
    offlineAvailable: true,
  },
  {
    path: "/stocktake",
    objectKind: "stocktake_session",
    title: "盘点证据与复核",
    navLabel: "盘点证据与复核",
    category: "库存与盘点",
    tier: "L2",
    requiredCapability: "inventory.stocktake",
    summary: "实盘证据对比、部分盘点标注与批量差异复核",
    offlineAvailable: true,
  },
  {
    path: "/knowledge",
    objectKind: "knowledge_review",
    title: "知识与别名复核",
    navLabel: "知识复核",
    category: "办公与异常",
    tier: "L2",
    requiredCapability: "knowledge.read",
    summary: "商品别名、经验规则修订审批与原始凭证对照",
    offlineAvailable: true,
  },
  {
    path: "/notifications",
    objectKind: "notification_inbox",
    title: "站内提醒与待办",
    navLabel: "站内提醒",
    category: "办公与异常",
    tier: "L0",
    requiredCapability: "tasks.read",
    summary: "跨端提醒、超时未核实事项与直达处理入口",
    offlineAvailable: true,
  },
  {
    path: "/materials",
    objectKind: "office_material",
    title: "材料收件箱",
    navLabel: "材料收件箱",
    category: "办公与异常",
    tier: "L2",
    requiredCapability: "office.materials.read",
    summary: "微信/拍照来料原始凭证与结构化识别复核",
    offlineAvailable: true,
  },
  {
    path: "/documents",
    objectKind: "office_document",
    title: "办公文档流转",
    navLabel: "办公文档",
    category: "办公与异常",
    tier: "L2",
    requiredCapability: "office.documents.read",
    summary: "办公单据提取、打印预览与受控流转审批",
    offlineAvailable: true,
  },
  {
    path: "/exceptions",
    objectKind: "office_exception",
    title: "异常交接与差异对账",
    navLabel: "异常与交接",
    category: "办公与异常",
    tier: "L2",
    requiredCapability: "office.exceptions.read",
    summary: "交接异常、未核实回执追溯与人工核销处理",
    offlineAvailable: true,
  },
  {
    path: "/agents",
    inLibrary: false,
    objectKind: "agent_thread",
    title: "Agent 工作台",
    navLabel: "Agent 工作台",
    category: "系统与线程",
    tier: "thread",
    requiredCapability: "agent.run",
    summary: "执行轨迹回放、工具审批与受控自主运行控制台",
    offlineAvailable: false,
  },
  {
    path: "/analytics",
    objectKind: "analytics_report",
    title: "经营与销售数据分析",
    navLabel: "经营分析",
    category: "系统与线程",
    tier: "L0",
    requiredCapability: "analytics.read",
    summary: "只读确定性经营统计与门店履约分析",
    offlineAvailable: true,
  },

  { path: "/assistant", objectKind: "assistant_work", title: "我的助理", navLabel: "我的助理", category: "系统与线程", tier: "thread", requiredCapability: "agent.run", summary: "沿用现有能力、权限与恢复边界", offlineAvailable: false },
  { path: "/memory", objectKind: "assistant_memory", title: "助理记忆", navLabel: "助理记忆", category: "系统与线程", tier: "L0", requiredCapability: "memory.read", summary: "沿用现有能力、权限与恢复边界", offlineAvailable: false },
  { path: "/pi-trial", inLibrary: false, objectKind: "pi_trial", title: "Pi 只读试用", navLabel: "Pi 只读试用", category: "系统与线程", tier: "thread", requiredCapability: "agent.run", summary: "沿用现有能力、权限与恢复边界", offlineAvailable: false },
  { path: "/pi-probe", inLibrary: false, objectKind: "pi_probe", title: "Pi 单次诊断", navLabel: "Pi 单次诊断", category: "系统与线程", tier: "thread", requiredCapability: "agent.run", summary: "沿用现有能力、权限与恢复边界", offlineAvailable: false },
];

export const OBJECT_LIBRARY_CATEGORIES: WorkObjectCategory[] = [
  "销售与客户",
  "采购与到货",
  "库存与盘点",
  "办公与异常",
  "系统与线程",
];

export function getWorkObjectByPath(path: string): WorkObjectModuleDescriptor | undefined {
  return WORK_OBJECT_MODULES.find((item) => item.path === path);
}

export function getPermittedWorkObjects(
  capabilities: string[],
  options: { includeHome?: boolean } = {},
): WorkObjectModuleDescriptor[] {
  const includeHome = options.includeHome ?? false;
  return WORK_OBJECT_MODULES.filter((item) => {
    if (item.inLibrary === false) return false;
    if (!includeHome && item.path === "/") {
      return false;
    }
    return isRouteVisible(
      {
        path: item.path,
        title: item.title,
        navLabel: item.navLabel,
        inNav: true,
        requiredCapability: item.requiredCapability,
      },
      capabilities,
    );
  });
}

export function toRouteWorkbenchTab(desc: WorkObjectModuleDescriptor): WorkbenchTab {
  if (desc.path === "/") {
    return {
      id: "today",
      kind: "today",
      title: "今日",
      path: "/",
      closable: false,
    };
  }
  return {
    id: `route:${desc.path}`,
    kind: "route",
    title: desc.navLabel,
    path: desc.path,
    objectKind: desc.objectKind,
    objectId: desc.path,
    closable: true,
  };
}

export function openWorkObjectRouteTab(path: string): boolean {
  const desc = getWorkObjectByPath(path);
  if (!desc) {
    return false;
  }
  return openWorkbenchTab(toRouteWorkbenchTab(desc));
}
