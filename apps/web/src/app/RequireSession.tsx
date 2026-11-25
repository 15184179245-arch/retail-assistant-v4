import type { ReactNode } from "react";
import type { Session } from "../lib/auth/session";
import { LoginPage } from "../features/auth/LoginPage";
import { isSafeInternalPath, navigate, useSearchParams } from "./router";

export interface RequireSessionProps {
  session: Session | null;
  currentPath: string;
  children: ReactNode;
}

export function RequireSession({ session, currentPath, children }: RequireSessionProps) {
  const searchParams = useSearchParams();
  if (!session) {
    const redirectParam = searchParams.get("redirect");
    const target = isSafeInternalPath(redirectParam)
      ? redirectParam!
      : isSafeInternalPath(currentPath) && currentPath !== "/login"
        ? currentPath
        : "/";

    return (
      <LoginPage
        onLoggedIn={() => {
          navigate(target, { replace: true });
        }}
      />
    );
  }

  return <>{children}</>;
}
