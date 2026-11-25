import { type CSSProperties, useEffect, useState } from "react";
import type { Session } from "../lib/auth/session";
import { useConnectionStatus } from "../lib/api/health";
import { navigate } from "./router";

export interface ConnectionBannerProps {
  session?: Session | null;
  audience?: "assistant" | "general";
  onRetryConnection?: () => void;
  onRelogin?: () => void;
}

function getSessionExpiryState(expiresAt?: string): {
  warning: string | null;
  isExpired: boolean;
} {
  if (!expiresAt) {
    return { warning: null, isExpired: false };
  }
  const expiryTime = new Date(expiresAt).getTime();
  const remainingMs = expiryTime - Date.now();

  if (remainingMs <= 0) {
    return {
      warning: "登录凭据已过期，请重新登录以继续操作。",
      isExpired: true,
    };
  }
  if (remainingMs <= 5 * 60 * 1000) {
    const remainingMinutes = Math.max(1, Math.ceil(remainingMs / 60000));
    return {
      warning: `登录会话即将在 ${remainingMinutes} 分钟后过期，请及时重新登录以避免操作中断。`,
      isExpired: false,
    };
  }
  return { warning: null, isExpired: false };
}

export function ConnectionBanner({
  session,
  audience = "general",
  onRetryConnection,
  onRelogin,
}: ConnectionBannerProps) {
  const { status, checkHealth } = useConnectionStatus();
  const initialExpiry = getSessionExpiryState(session?.expiresAt);
  const [sessionExpiryWarning, setSessionExpiryWarning] = useState<string | null>(
    initialExpiry.warning,
  );
  const [isSessionExpired, setIsSessionExpired] = useState(initialExpiry.isExpired);

  useEffect(() => {
    if (!session?.expiresAt) {
      setSessionExpiryWarning(null);
      setIsSessionExpired(false);
      return;
    }

    const checkExpiry = () => {
      const state = getSessionExpiryState(session.expiresAt);
      setIsSessionExpired(state.isExpired);
      setSessionExpiryWarning(state.warning);
    };

    checkExpiry();
    const timer = setInterval(checkExpiry, 15000);
    return () => clearInterval(timer);
  }, [session?.expiresAt]);

  const handleRelogin = () => {
    if (onRelogin) {
      onRelogin();
    } else {
      navigate("/login");
    }
  };

  const handleRetry = () => {
    if (onRetryConnection) {
      onRetryConnection();
    } else {
      void checkHealth();
    }
  };

  // Determine banner configuration
  let bannerType: "danger" | "warning" | "info" | null = null;
  let badgeLabel = "";
  let message = "";
  let action: { label: string; onClick: () => void } | null = null;
  let role: "alert" | "status" = "status";

  if (sessionExpiryWarning) {
    bannerType = isSessionExpired ? "danger" : "warning";
    badgeLabel = isSessionExpired ? "会话已过期" : "会话即将过期";
    message = sessionExpiryWarning;
    action = { label: "重新登录", onClick: handleRelogin };
    role = "alert";
  } else if (status === "offline" && audience !== "assistant") {
    bannerType = "danger";
    badgeLabel = "离线";
    message = "网络连接已断开，系统已切换至离线模式。现场扫码查货与人工开单保持可用，数据将在网络恢复后同步。";
    role = "alert";
  } else if (status === "backend_unreachable") {
    bannerType = "warning";
    badgeLabel = "服务未连接";
    message = audience === "assistant"
      ? "暂时无法连接助理服务。可继续编辑草稿；已经发送的消息请先核对原记录，不要重发。"
      : "服务端连接中断，正在尝试重新连接... 现场扫码与手工开单保持可用，写操作暂未同步。";
    action = { label: "重试连接", onClick: handleRetry };
    role = "alert";
  } else if (status === "model_offline") {
    bannerType = "info";
    badgeLabel = "AI 离线";
    message = audience === "assistant"
      ? "模型暂不可用。已保存事项仍可查看；本页是否接收新消息以当前发送入口为准。"
      : "AI 辅助模型当前不可用，Agent 助手建议功能已暂停；现场手工开单、扫码与收货业务正常可用。";
    role = "status";
  }

  if (!bannerType) {
    return null;
  }

  const colorStyles: Record<
    "danger" | "warning" | "info",
    { bg: string; border: string; ink: string; badgeBg: string }
  > = {
    danger: {
      bg: "var(--danger-bg, #fdf0ed)",
      border: "var(--danger-border, #e8b9ad)",
      ink: "var(--danger-ink, #7d2c1f)",
      badgeBg: "#ffffff",
    },
    warning: {
      bg: "var(--warning-bg, #fff5dd)",
      border: "var(--warning-border, #e7c57c)",
      ink: "var(--warning-ink, #7a4d00)",
      badgeBg: "#ffffff",
    },
    info: {
      bg: "var(--info-bg, #eaf4fb)",
      border: "var(--info-border, #b9d8ea)",
      ink: "var(--info-ink, #1f5f86)",
      badgeBg: "#ffffff",
    },
  };

  const currentColors = colorStyles[bannerType];

  const containerStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "10px 16px",
    gap: "12px",
    backgroundColor: currentColors.bg,
    borderBottom: `1px solid ${currentColors.border}`,
    color: currentColors.ink,
    fontSize: "var(--font-size-small, 13px)",
    lineHeight: 1.4,
    boxSizing: "border-box",
  };

  const leftStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    flex: 1,
  };

  const badgeStyle: CSSProperties = {
    padding: "2px 6px",
    borderRadius: "var(--radius-sm, 6px)",
    backgroundColor: currentColors.badgeBg,
    border: `1px solid ${currentColors.border}`,
    fontSize: "var(--font-size-caption, 12px)",
    fontWeight: 700,
    whiteSpace: "nowrap",
  };

  const buttonStyle: CSSProperties = {
    padding: "4px 10px",
    borderRadius: "var(--radius-sm, 6px)",
    border: `1px solid ${currentColors.border}`,
    backgroundColor: "#ffffff",
    color: currentColors.ink,
    fontSize: "var(--font-size-small, 13px)",
    fontWeight: 600,
    cursor: "pointer",
    whiteSpace: "nowrap",
    minHeight: "32px",
  };

  return (
    <div
      className={`connection-banner connection-banner-${bannerType}`}
      style={containerStyle}
      role={role}
      aria-live={role === "alert" ? "assertive" : "polite"}
    >
      <div style={leftStyle}>
        <span style={badgeStyle}>{badgeLabel}</span>
        <span>{message}</span>
      </div>
      {action ? (
        <button
          type="button"
          style={buttonStyle}
          onClick={action.onClick}
        >
          {action.label}
        </button>
      ) : null}
    </div>
  );
}
