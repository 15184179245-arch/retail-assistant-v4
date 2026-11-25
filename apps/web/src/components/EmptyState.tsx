import type { CSSProperties, ReactNode } from "react";

export type EmptyStateType = "empty" | "forbidden" | "unsupported";

export interface EmptyStateProps {
  type?: EmptyStateType;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

const TYPE_CONFIG: Record<
  EmptyStateType,
  { icon: string; defaultDescription: string; toneColor: string }
> = {
  empty: {
    icon: "▫",
    defaultDescription: "当前列表或检索结果暂无记录。",
    toneColor: "var(--muted, #65736f)",
  },
  forbidden: {
    icon: "⊘",
    defaultDescription: "您当前的员工账号缺少访问此工作区所需的权限能力，请联系管理员。",
    toneColor: "var(--warning-ink, #7a4d00)",
  },
  unsupported: {
    icon: "⚙",
    defaultDescription: "外部设备或第三方系统尚未接入或当前处于离线状态。",
    toneColor: "var(--info-ink, #1f5f86)",
  },
};

export function EmptyState({
  type = "empty",
  title,
  description,
  action,
  className,
  style,
}: EmptyStateProps) {
  const config = TYPE_CONFIG[type];
  const finalDescription = description ?? config.defaultDescription;

  const containerStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    justifyContent: "center",
    textAlign: "center",
    padding: "48px 24px",
    borderRadius: "var(--radius-lg, 14px)",
    border: "1px dashed var(--border-subtle, #d7dedb)",
    backgroundColor: "var(--surface, #ffffff)",
    minHeight: "220px",
    gap: "12px",
    ...style,
  };

  const iconStyle: CSSProperties = {
    fontSize: "32px",
    lineHeight: 1,
    color: config.toneColor,
    marginBottom: "4px",
  };

  const titleStyle: CSSProperties = {
    margin: 0,
    fontSize: "var(--font-size-h2, 16px)",
    fontWeight: 700,
    color: "var(--ink, #17232b)",
  };

  const descStyle: CSSProperties = {
    margin: 0,
    maxWidth: "420px",
    fontSize: "var(--font-size-small, 13px)",
    color: "var(--muted, #65736f)",
    lineHeight: 1.6,
  };

  return (
    <div
      className={className}
      style={containerStyle}
      data-empty-state={type}
      role={type === "forbidden" ? "alert" : "status"}
    >
      <span aria-hidden="true" style={iconStyle}>
        {config.icon}
      </span>
      <h3 style={titleStyle}>{title}</h3>
      <p style={descStyle}>{finalDescription}</p>
      {action ? <div style={{ marginTop: "8px" }}>{action}</div> : null}
    </div>
  );
}
