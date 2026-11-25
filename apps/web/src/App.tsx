import { useSyncExternalStore } from "react";
import "./styles/global.css";
import { getScopeVersion, getSession, subscribe } from "./lib/auth/session";
import type { Session } from "./lib/auth/session";
import { useCapabilities } from "./lib/auth/capabilities";
import { usePath } from "./app/router";
import { canAccessRoute } from "./app/navigation";
import { AppShell } from "./app/AppShell";
import { RequireSession } from "./app/RequireSession";
import { NotFoundPage } from "./app/NotFoundPage";
import { NAV_ROUTES, ForbiddenPage, renderRouteComponent } from "./app/routes";

function useSession(): Session | null {
  return useSyncExternalStore(subscribe, getSession, getSession);
}

function isKnownRoute(path: string): boolean {
  return path === "/login" || NAV_ROUTES.some((r) => r.path === path);
}

export function App() {
  const session = useSession();
  // Every identity/store/token epoch owns its own local inputs and dialogs.
  const scopeVersion = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const currentPath = usePath();
  const { capabilities } = useCapabilities();

  let content: React.ReactNode;
  if (!isKnownRoute(currentPath)) {
    content = <NotFoundPage path={currentPath} />;
  } else {
    content = (
      <RequireSession session={session} currentPath={currentPath}>
        {session ? (() => {
          const access = canAccessRoute(currentPath, capabilities, true);
          if (!access.allowed && access.reason === "forbidden") {
            return <ForbiddenPage path={currentPath} requiredCapability={access.requiredCapability} />;
          }
          return renderRouteComponent(currentPath, session);
        })() : null}
      </RequireSession>
    );
  }

  return (
    <AppShell key={scopeVersion} session={session} currentPath={currentPath}>
      {content}
    </AppShell>
  );
}
