import { Button } from '../components/Button';
import styles from './blocks.module.css';

export interface PendingAiSuggestion {
  suggestionId?: string;
  opId?: string;
  value: string | number;
  reason?: string | null;
  createdAt?: string;
}

export interface PartialUndoPlan {
  revertFields: string[];
  skippedFields: string[];
  conflictCount: number;
  conflictMessage: string | null;
}

/**
 * Computes which fields from an AI operation can be safely reverted vs skipped
 * because a human modified or locked them afterwards.
 */
export function computePartialUndoPlan(
  opChangedFields: readonly string[],
  humanModifiedAfterOp: readonly string[],
): PartialUndoPlan {
  const humanSet = new Set(humanModifiedAfterOp);
  const revertFields: string[] = [];
  const skippedFields: string[] = [];
  for (const field of opChangedFields) {
    if (humanSet.has(field)) {
      skippedFields.push(field);
    } else {
      revertFields.push(field);
    }
  }
  const conflictCount = skippedFields.length;
  return {
    revertFields,
    skippedFields,
    conflictCount,
    conflictMessage:
      conflictCount > 0 ? `${conflictCount} 个字段已被人工修改，未撤销` : null,
  };
}

export interface PartialUndoNoticeProps {
  conflictCount: number;
  skippedFields?: readonly string[];
  customMessage?: string | null;
  onDismiss?: () => void;
}

export function PartialUndoNotice({
  conflictCount,
  skippedFields = [],
  customMessage,
  onDismiss,
}: PartialUndoNoticeProps) {
  if (conflictCount <= 0 && !customMessage) {
    return null;
  }
  const message =
    customMessage ?? `${conflictCount} 个字段已被人工修改，未撤销`;

  return (
    <div
      role="alert"
      data-partial-undo-notice="true"
      className={styles.partialUndoBanner}
    >
      <div>
        <span>{message}</span>
        {skippedFields.length > 0 ? (
          <span style={{ marginLeft: '6px', fontWeight: 400 }}>
            （已跳过：{skippedFields.join('、')}）
          </span>
        ) : null}
      </div>
      {onDismiss ? (
        <Button size="sm" variant="secondary" onClick={onDismiss}>
          知道了
        </Button>
      ) : null}
    </div>
  );
}

export interface L1CoeditFieldProps {
  fieldKey: string;
  label: string;
  value: string | number;
  type?: 'text' | 'number';
  min?: number;
  max?: number;
  step?: number | string;
  disabled?: boolean;
  aiEdited?: boolean;
  aiOpId?: string;
  aiPreviousValue?: string | number;
  locked?: boolean;
  lockedBy?: string;
  pendingSuggestion?: PendingAiSuggestion | null;
  onChange?: (nextValue: string) => void;
  onLock?: (fieldKey: string) => void;
  onUnlock?: (fieldKey: string) => void;
  onAcceptSuggestion?: (fieldKey: string, suggestedValue: string | number) => void;
  onIgnoreSuggestion?: (fieldKey: string) => void;
  onRejectAiField?: (fieldKey: string, opId?: string) => void;
  onUndoWholeOp?: (opId: string) => void;
  partialUndoConflictCount?: number;
  partialUndoSkippedFields?: readonly string[];
  partialUndoNotice?: string | null;
  onDismissPartialUndoNotice?: () => void;
  id?: string;
  className?: string;
}

export function L1CoeditField({
  fieldKey,
  label,
  value,
  type = 'text',
  min,
  max,
  step,
  disabled = false,
  aiEdited = false,
  aiOpId,
  aiPreviousValue,
  locked = false,
  lockedBy,
  pendingSuggestion,
  onChange,
  onLock,
  onUnlock,
  onAcceptSuggestion,
  onIgnoreSuggestion,
  onRejectAiField,
  onUndoWholeOp,
  partialUndoConflictCount = 0,
  partialUndoSkippedFields = [],
  partialUndoNotice,
  onDismissPartialUndoNotice,
  id,
  className,
}: L1CoeditFieldProps) {
  const inputId = id ?? `coedit-${fieldKey}`;
  const showAiEncoding = aiEdited && !locked;

  return (
    <div
      data-coedit-field={fieldKey}
      data-ai-edited={showAiEncoding ? 'true' : 'false'}
      data-human-locked={locked ? 'true' : 'false'}
      className={[
        styles.coeditField,
        showAiEncoding ? styles.coeditAiEdited : '',
        locked ? styles.coeditLocked : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className={styles.coeditHeader}>
        <label htmlFor={inputId} className={styles.coeditLabel}>
          {label}
        </label>
        <div className={styles.coeditBadges}>
          {showAiEncoding ? (
            <span className={styles.aiBadge} data-ai-edited-badge="true">
              <span aria-hidden="true">✦</span>
              <span>AI 刚改</span>
            </span>
          ) : null}
          {locked ? (
            <span className={styles.lockBadge} data-human-locked-badge="true">
              <span aria-hidden="true">🔒</span>
              <span>人工锁定</span>
              {lockedBy ? <span>· {lockedBy}</span> : null}
            </span>
          ) : null}
        </div>
      </div>

      <div className={styles.coeditControlRow}>
        <input
          id={inputId}
          type={type}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          value={String(value)}
          className={styles.coeditInput}
          onChange={(event) => onChange?.(event.target.value)}
        />
        {locked && onUnlock ? (
          <Button
            size="sm"
            variant="secondary"
            onClick={() => onUnlock(fieldKey)}
            data-unlock-field={fieldKey}
          >
            解锁
          </Button>
        ) : !locked && onLock ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onLock(fieldKey)}
            data-lock-field={fieldKey}
          >
            锁定
          </Button>
        ) : null}
      </div>

      {showAiEncoding && (onRejectAiField || (onUndoWholeOp && aiOpId)) ? (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' }}>
          {aiPreviousValue !== undefined ? (
            <span style={{ fontSize: '12px', color: 'var(--muted, #65736f)' }}>
              原值：{String(aiPreviousValue)}
            </span>
          ) : null}
          {onRejectAiField ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => onRejectAiField(fieldKey, aiOpId)}
              data-reject-ai-field={fieldKey}
            >
              拒绝此字段
            </Button>
          ) : null}
          {onUndoWholeOp && aiOpId ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => onUndoWholeOp(aiOpId)}
              data-undo-whole-op={aiOpId}
            >
              整次撤销
            </Button>
          ) : null}
        </div>
      ) : null}

      {locked && pendingSuggestion ? (
        <div
          className={styles.suggestionCard}
          data-suggestion-card="true"
          role="region"
          aria-label={`${label} AI 修改建议`}
        >
          <div>
            <strong>◇ AI 修改建议（原人工锁定值保留不被覆盖）：</strong>
            <span>
              当前锁定值 <code>{String(value)}</code> → 建议改为{' '}
              <code>{String(pendingSuggestion.value)}</code>
            </span>
          </div>
          {pendingSuggestion.reason ? (
            <div>理由：{pendingSuggestion.reason}</div>
          ) : null}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
            <Button
              size="sm"
              variant="primary"
              onClick={() =>
                onAcceptSuggestion?.(fieldKey, pendingSuggestion.value)
              }
              data-accept-suggestion={fieldKey}
            >
              接受建议（保持人工锁定）
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => onIgnoreSuggestion?.(fieldKey)}
              data-ignore-suggestion={fieldKey}
            >
              忽略建议
            </Button>
          </div>
        </div>
      ) : null}

      <PartialUndoNotice
        conflictCount={partialUndoConflictCount}
        skippedFields={partialUndoSkippedFields}
        customMessage={partialUndoNotice}
        onDismiss={onDismissPartialUndoNotice}
      />
    </div>
  );
}
