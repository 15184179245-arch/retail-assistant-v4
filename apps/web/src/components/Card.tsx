import type { CSSProperties, ReactNode } from "react";

export type CardPadding = "none" | "sm" | "md" | "lg";

export interface CardProps {
  title?: string;
  subtitle?: string;
  eyebrow?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  padding?: CardPadding;
}

const PADDING_MAP: Record<CardPadding, string> = {
  none: "0",
  sm: "var(--space-4, 16px)",
  md: "var(--space-6, 24px)",
  lg: "var(--space-12, 48px)",
};

export function Card({
  title,
  subtitle,
  eyebrow,
  actions,
  children,
  className,
  style,
  padding = "md",
}: CardProps) {
  const containerStyle: CSSProperties = {
    backgroundColor: "var(--surface, #ffffff)",
    border: "1px solid var(--border, #e4e9e8)",
    borderRadius: "var(--radius-xl, 20px)",
    boxShadow: "0 10px 30px rgb(24 48 42 / 4%)",
    padding: PADDING_MAP[padding],
    boxSizing: "border-box",
    ...style,
  };

  const headerStyle: CSSProperties = {
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: "16px",
    marginBottom: title || subtitle || eyebrow ? "16px" : "0",
  };

  const titleStyle: CSSProperties = {
    margin: "4px 0 0",
    fontSize: "var(--font-size-h2, 16px)",
    fontWeight: 700,
    color: "var(--ink, #17232b)",
  };

  const subtitleStyle: CSSProperties = {
    margin: "4px 0 0",
    fontSize: "var(--font-size-small, 13px)",
    color: "var(--muted, #65736f)",
  };

  const eyebrowStyle: CSSProperties = {
    margin: 0,
    fontSize: "var(--font-size-eyebrow, 11px)",
    fontWeight: 700,
    color: "var(--ink-eyebrow, #6b7b78)",
    letterSpacing: ".08em",
    textTransform: "uppercase",
  };

  const hasHeader = Boolean(title || subtitle || eyebrow || actions);

  return (
    <article className={className} style={containerStyle}>
      {hasHeader ? (
        <header style={headerStyle}>
          <div>
            {eyebrow ? <p style={eyebrowStyle}>{eyebrow}</p> : null}
            {title ? <h3 style={titleStyle}>{title}</h3> : null}
            {subtitle ? <p style={subtitleStyle}>{subtitle}</p> : null}
          </div>
          {actions ? <div>{actions}</div> : null}
        </header>
      ) : null}
      <div>{children}</div>
    </article>
  );
}
