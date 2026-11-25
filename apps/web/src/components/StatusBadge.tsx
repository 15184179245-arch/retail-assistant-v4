import type { CSSProperties } from "react";
import vocabulary from "../../../../docs/ui/status-vocabulary.json";

export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger";

export interface StatusVocabularyItem {
  kind: string;
  label: string;
  description: string;
  tone: StatusTone;
  icon: string;
  allow_success_tone: boolean;
  rule: string;
}

export type StatusKind =
  | "development"
  | "production"
  | "suggestion"
  | "fact"
  | "pending_sync"
  | "settled"
  | "verified"
  | "unverified"
  | "waiting"
  | "error";

export interface StatusDefinition {
  label: string;
  description: string;
  tone: StatusTone;
  icon: string;
  allowSuccessTone: boolean;
}

const rawStatuses = vocabulary.statuses as Record<string, StatusVocabularyItem>;

// Enforce semantic safety rule at load time: only verified may ever have success tone or check icon
export const STATUS_DEFINITIONS: Record<StatusKind, StatusDefinition> = (
  Object.entries(rawStatuses) as [StatusKind, StatusVocabularyItem][]
).reduce((acc, [kind, def]) => {
  if (def.tone === "success" && !def.allow_success_tone) {
    throw new Error(`Semantic safety violation: status '${kind}' cannot use success tone without allow_success_tone`);
  }
  if (def.icon === "✓" && kind !== "verified") {
    throw new Error(`Semantic safety violation: status '${kind}' cannot use check icon '✓'`);
  }
  acc[kind] = {
    label: def.label,
    description: def.description,
    tone: def.tone,
    icon: def.icon,
    allowSuccessTone: def.allow_success_tone,
  };
  return acc;
}, {} as Record<StatusKind, StatusDefinition>);

const TONE_STYLES: Record<StatusTone, CSSProperties> = {
  neutral: { color: "var(--neutral-ink, #3c4a46)", background: "var(--neutral-bg, #f1f4f3)", borderColor: "var(--neutral-border, #d7dedb)" },
  info: { color: "var(--info-ink, #1f5f86)", background: "var(--info-bg, #eaf4fb)", borderColor: "var(--info-border, #b9d8ea)" },
  success: { color: "var(--success-ink, #1f5e4b)", background: "var(--success-bg, #eaf6f0)", borderColor: "var(--success-border, #b7dfcd)" },
  warning: { color: "var(--warning-ink, #7a4d00)", background: "var(--warning-bg, #fff5dd)", borderColor: "var(--warning-border, #e7c57c)" },
  danger: { color: "var(--danger-ink, #7d2c1f)", background: "var(--danger-bg, #fdf0ed)", borderColor: "var(--danger-border, #e8b9ad)" },
};

export interface StatusBadgeProps {
  kind: StatusKind;
  source?: string;
  className?: string;
}

export function StatusBadge({ kind, source, className }: StatusBadgeProps) {
  const definition = STATUS_DEFINITIONS[kind];
  if (!definition) {
    throw new Error(`Status kind '${kind}' is not defined in status-vocabulary.json`);
  }

  const style: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "4px 9px",
    borderRadius: "var(--radius-pill, 999px)",
    border: "1px solid",
    fontSize: "var(--font-size-caption, 12px)",
    fontWeight: 600,
    lineHeight: 1.4,
    ...TONE_STYLES[definition.tone],
  };

  const label = source ? `${definition.label}，来源：${source}` : definition.label;

  return (
    <span
      role="status"
      aria-label={label}
      title={definition.description}
      data-status={kind}
      data-tone={definition.tone}
      className={className}
      style={style}
    >
      <span aria-hidden="true">{definition.icon}</span>
      <span>{definition.label}</span>
      {source ? <span data-status-source>{source}</span> : null}
    </span>
  );
}
