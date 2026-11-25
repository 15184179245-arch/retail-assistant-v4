import type { ButtonHTMLAttributes, CSSProperties, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  icon?: ReactNode;
  children?: ReactNode;
}

const VARIANT_STYLES: Record<ButtonVariant, CSSProperties> = {
  primary: {
    backgroundColor: "var(--brand-primary, #173f35)",
    color: "#ffffff",
    border: "1px solid transparent",
  },
  secondary: {
    backgroundColor: "var(--surface, #ffffff)",
    color: "var(--ink, #17232b)",
    border: "1px solid var(--border-subtle, #d7dedb)",
  },
  danger: {
    backgroundColor: "var(--danger, #8a3324)",
    color: "#ffffff",
    border: "1px solid transparent",
  },
  ghost: {
    backgroundColor: "transparent",
    color: "var(--brand-primary, #173f35)",
    border: "1px solid transparent",
  },
};

const SIZE_STYLES: Record<ButtonSize, CSSProperties> = {
  sm: {
    padding: "6px 12px",
    fontSize: "var(--font-size-caption, 12px)",
    minHeight: "var(--min-touch-target, 44px)",
  },
  md: {
    padding: "10px 18px",
    fontSize: "var(--font-size-small, 13px)",
    minHeight: "var(--min-touch-target, 44px)",
  },
  lg: {
    padding: "14px 24px",
    fontSize: "var(--font-size-body, 14px)",
    minHeight: "48px",
  },
};

export function Button({
  variant = "primary",
  size = "md",
  loading = false,
  disabled = false,
  icon,
  children,
  style,
  className,
  type = "button",
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;

  const combinedStyle: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "8px",
    borderRadius: "var(--radius-md, 10px)",
    fontFamily: "inherit",
    fontWeight: 600,
    cursor: isDisabled ? (loading ? "progress" : "not-allowed") : "pointer",
    opacity: isDisabled ? 0.65 : 1,
    transition: "background-color 0.15s ease, opacity 0.15s ease",
    boxSizing: "border-box",
    ...VARIANT_STYLES[variant],
    ...SIZE_STYLES[size],
    ...style,
  };

  return (
    <button
      type={type}
      disabled={isDisabled}
      aria-busy={loading ? "true" : undefined}
      data-variant={variant}
      data-size={size}
      style={combinedStyle}
      className={className}
      {...rest}
    >
      {loading ? (
        <span aria-hidden="true" style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>
          ↻
        </span>
      ) : (
        icon ? <span aria-hidden="true">{icon}</span> : null
      )}
      {children}
    </button>
  );
}
