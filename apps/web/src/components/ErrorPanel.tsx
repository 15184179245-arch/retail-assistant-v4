import type { CSSProperties } from "react";
import { Button } from "./Button";

export interface ErrorPanelProps {
  userMessage: string;
  hint?: string | null;
  code?: string | null;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
  style?: CSSProperties;
}

export function ErrorPanel({
  userMessage,
  hint,
  code,
  onRetry,
  retryLabel = "重试操作",
  className,
  style,
}: ErrorPanelProps) {
  const containerStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "10px",
    padding: "16px 20px",
    borderRadius: "var(--radius-md, 10px)",
    backgroundColor: "var(--danger-bg, #fdf0ed)",
    border: "1px solid var(--danger-border, #e8b9ad)",
    color: "var(--danger-ink, #7d2c1f)",
    fontSize: "var(--font-size-small, 13px)",
    boxSizing: "border-box",
    ...style,
  };

  const headerStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: "12px",
    flexWrap: "wrap",
  };

  const titleRowStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    fontWeight: 700,
    fontSize: "var(--font-size-body, 14px)",
  };

  const codeBadgeStyle: CSSProperties = {
    fontFamily: "monospace",
    fontSize: "var(--font-size-caption, 12px)",
    padding: "2px 6px",
    borderRadius: "var(--radius-sm, 6px)",
    backgroundColor: "#ffffff",
    border: "1px solid var(--danger-border, #e8b9ad)",
    color: "var(--danger, #8a3324)",
  };

  const hintStyle: CSSProperties = {
    margin: 0,
    fontSize: "var(--font-size-caption, 12px)",
    color: "var(--muted, #65736f)",
    lineHeight: 1.5,
  };

  return (
    <div
      role="alert"
      aria-live="assertive"
      data-error-panel
      className={className}
      style={containerStyle}
    >
      <div style={headerStyle}>
        <div style={titleRowStyle}>
          <span aria-hidden="true" style={{ fontSize: "16px" }}>
            !
          </span>
          <span data-error-user-message>{userMessage}</span>
        </div>
        {code ? <span style={codeBadgeStyle} data-error-code>{code}</span> : null}
      </div>

      {hint ? (
        <p style={hintStyle} data-error-hint>
          建议排查：{hint}
        </p>
      ) : null}

      {onRetry ? (
        <div style={{ marginTop: "4px" }}>
          <Button variant="danger" size="sm" onClick={onRetry}>
            {retryLabel}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
