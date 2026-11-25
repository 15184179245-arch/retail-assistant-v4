import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError, apiRequest, type LoginResponse } from "../../lib/api/client";
import { setSession } from "../../lib/auth/session";
import { refreshCapabilities } from "../../lib/auth/capabilities";

export interface LoginPageProps {
  baseUrl?: string;
  onLoggedIn?: () => void;
}

export function LoginPage({ baseUrl = "", onLoggedIn }: LoginPageProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (pending) {
      return;
    }
    setPending(true);
    setError(null);
    try {
      const data = await apiRequest<LoginResponse>("/api/v1/auth/login", {
        method: "POST",
        baseUrl,
        body: { username, password },
      });
      setSession({
        accessToken: data.access_token,
        tokenType: data.token_type,
        expiresAt: data.expires_at,
        subjectId: data.subject_id,
        organizationId: data.organization_id,
        storeId: data.store_id,
        username,
      });
      try {
        await refreshCapabilities();
      } catch {
        // Fallback gracefully if identity context endpoint is unreachable
      }
      setPassword("");
      onLoggedIn?.();
    } catch (caught) {
      const message =
        caught instanceof ApiError
          ? caught.userMessage
          : "登录失败，请稍后重试。";
      setError(message);
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="welcome-card" aria-labelledby="login-title">
      <p className="eyebrow">管理端 · 员工身份</p>
      <h2 id="login-title">员工登录</h2>
      <p className="welcome-copy">
        登录后进入管理端；业务模块将在后续批次按已确认的契约逐步接入。
      </p>
      <form className="login-form" onSubmit={handleSubmit} noValidate>
        <label className="field">
          <span>账号</span>
          <input
            name="username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />
        </label>
        <label className="field">
          <span>密码</span>
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error ? (
          <p className="login-error" role="alert">
            {error}
          </p>
        ) : null}
        <button className="login-button" type="submit" disabled={pending}>
          {pending ? "登录中…" : "登录"}
        </button>
      </form>
    </section>
  );
}
