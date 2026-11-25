import type { CSSProperties, ReactNode } from "react";
import { StatusBadge } from "./StatusBadge";
import type { StatusKind } from "./StatusBadge";

export interface PageHeaderProps {
  title: string;
  eyebrow?: string;
  source?: string;
  timestamp?: string;
  statusKind?: StatusKind;
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  style?: CSSProperties;
}

export function PageHeader({
  title,
  eyebrow,
  source,
  timestamp,
  statusKind,
  actions,
  children,
  className,
  style,
}: PageHeaderProps) {
  const containerStyle: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "12px",
    padding: "24px 0 16px",
    borderBottom: "1px solid var(--border-subtle, #d7dedb)",
    marginBottom: "24px",
    ...style,
  };

  const topRowStyle: CSSProperties = {
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: "16px",
    flexWrap: "wrap",
  };

  const eyebrowStyle: CSSProperties = {
    margin: "0 0 4px",
    fontSize: "var(--font-size-eyebrow, 11px)",
    fontWeight: 700,
    color: "var(--ink-eyebrow, #6b7b78)",
    letterSpacing: ".1em",
    textTransform: "uppercase",
  };

  const titleStyle: CSSProperties = {
    margin: 0,
    fontSize: "var(--font-size-h1, 18px)",
    fontWeight: 700,
    color: "var(--ink, #17232b)",
  };

  const auditRowStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    flexWrap: "wrap",
    fontSize: "var(--font-size-caption, 12px)",
    color: "var(--muted, #65736f)",
  };

  return (
    <header className={className} style={containerStyle} data-page-header>
      <div style={topRowStyle}>
        <div>
          {eyebrow ? <p style={eyebrowStyle}>{eyebrow}</p> : null}
          <h1 style={titleStyle}>{title}</h1>
        </div>
        {actions ? <div>{actions}</div> : null}
      </div>

      {(source || timestamp || statusKind) ? (
        <div style={auditRowStyle} data-audit-context>
          {statusKind ? <StatusBadge kind={statusKind} source={source} /> : null}
          {source && !statusKind ? <span>来源：{source}</span> : null}
          {timestamp ? <span>核验/同步时点：{timestamp}</span> : null}
        </div>
      ) : null}

      {children ? <div>{children}</div> : null}
    </header>
  );
}
