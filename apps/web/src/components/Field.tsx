import type { CSSProperties, ReactNode } from "react";

export interface FieldProps {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  required?: boolean;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}

export function Field({
  id,
  label,
  hint,
  error,
  required,
  children,
  className,
  style,
}: FieldProps) {
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;

  const containerStyle: CSSProperties = {
    display: "grid",
    gap: "6px",
    color: "var(--ink-secondary, #4c5a56)",
    fontSize: "var(--font-size-small, 13px)",
    fontWeight: 600,
    ...style,
  };

  const labelStyle: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: "4px",
    color: "var(--ink-secondary, #4c5a56)",
  };

  const hintStyle: CSSProperties = {
    margin: 0,
    color: "var(--muted, #65736f)",
    fontSize: "var(--font-size-caption, 12px)",
    fontWeight: 400,
    lineHeight: 1.4,
  };

  const errorStyle: CSSProperties = {
    margin: 0,
    color: "var(--danger, #8a3324)",
    fontSize: "var(--font-size-caption, 12px)",
    fontWeight: 500,
    lineHeight: 1.4,
  };

  return (
    <div className={className} style={containerStyle} data-field={id}>
      <label htmlFor={id} style={labelStyle}>
        <span>{label}</span>
        {required ? (
          <span aria-hidden="true" style={{ color: "var(--danger, #8a3324)" }}>
            *
          </span>
        ) : null}
      </label>

      {/* Render children - if consumer passes input, describedBy can be associated */}
      <div data-field-control data-describedby={describedBy}>
        {children}
      </div>

      {hint ? (
        <p id={hintId} style={hintStyle} data-field-hint>
          {hint}
        </p>
      ) : null}

      {error ? (
        <p id={errorId} role="alert" style={errorStyle} data-field-error>
          {error}
        </p>
      ) : null}
    </div>
  );
}
