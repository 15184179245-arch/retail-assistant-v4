import { NAV_ROUTES } from "./routes";
import type { RouteItem } from "./routes";

export interface NavigationFilterOptions {
  isModelOffline?: boolean;
}

export interface RouteAccessCheck {
  allowed: boolean;
  reason?: "unauthenticated" | "forbidden" | "not_found";
  requiredCapability?: string;
}

/**
 * Determines whether a route is visible in navigation bars or menus based on current capabilities.
 */
export function isRouteVisible(
  route: RouteItem,
  capabilities: string[],
  _options: NavigationFilterOptions = {},
): boolean {
  if (!route.inNav) {
    return false;
  }

  // Route requires no special capability, or only authentication
  if (!route.requiredCapability || route.requiredCapability === "authenticated" || route.requiredCapability === "public") {
    return true;
  }

  // Model offline rule: Agent 工作台 remains visible in degraded read-only mode even if model is offline
  // (per design-system.md rule 8: AI failure must never block entry, timeline is visible).

  if (route.requiredCapability === "agent.run") {
    return capabilities.includes("agent.run") || capabilities.includes("agents.run");
  }
  if (route.requiredCapability === "receiving.read") {
    return capabilities.includes("receiving.read") || capabilities.includes("erp.receiving.read");
  }

  return capabilities.includes(route.requiredCapability);
}

/**
 * Returns all navigation items that the currently authenticated employee has capability to view.
 */
export function getVisibleNavRoutes(
  capabilities: string[],
  options: NavigationFilterOptions = {},
): RouteItem[] {
  return NAV_ROUTES.filter((route) => isRouteVisible(route, capabilities, options));
}

/**
 * Evaluates route guard permissions for a target URL path.
 * Direct URL access to forbidden pages is blocked before protected child components render.
 */
export function canAccessRoute(
  path: string,
  capabilities: string[],
  isAuthenticated: boolean,
): RouteAccessCheck {
  if (path === "/login") {
    return { allowed: true };
  }

  const route = NAV_ROUTES.find((r) => r.path === path);
  if (!route) {
    return { allowed: false, reason: "not_found" };
  }

  if (!isAuthenticated) {
    return { allowed: false, reason: "unauthenticated" };
  }

  if (
    !route.requiredCapability ||
    route.requiredCapability === "authenticated" ||
    route.requiredCapability === "public"
  ) {
    return { allowed: true };
  }

  if (
    route.requiredCapability === "agent.run" &&
    (capabilities.includes("agent.run") || capabilities.includes("agents.run"))
  ) {
    return { allowed: true };
  }
  if (
    route.requiredCapability === "receiving.read" &&
    (capabilities.includes("receiving.read") || capabilities.includes("erp.receiving.read"))
  ) {
    return { allowed: true };
  }
  if (capabilities.includes(route.requiredCapability)) {
    return { allowed: true };
  }

  return {
    allowed: false,
    reason: "forbidden",
    requiredCapability: route.requiredCapability,
  };
}
