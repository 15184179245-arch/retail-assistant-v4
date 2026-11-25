import { useEffect } from 'react';
import { Button } from '../Button';
import { StatusBadge } from '../StatusBadge';
import type { StatusKind } from '../StatusBadge';
import styles from './primitives.module.css';

export type ToastTone = 'info' | 'warning' | 'danger' | 'neutral';

export interface ToastItem {
  id: string;
  title: string;
  description?: string;
  tone?: ToastTone;
  statusKind?: StatusKind;
  assertive?: boolean;
  durationMs?: number;
  actionLabel?: string;
  onAction?: () => void;
}

export interface ToastProps {
  toasts: readonly ToastItem[];
  onDismiss: (id: string) => void;
  className?: string;
}

const TONE_CLASS: Record<ToastTone, string> = {
  info: styles.toastInfo,
  warning: styles.toastWarning,
  danger: styles.toastDanger,
  neutral: styles.toastNeutral,
};

function ToastEntry({
  item,
  onDismiss,
}: {
  item: ToastItem;
  onDismiss: (id: string) => void;
}) {
  const tone = item.tone ?? 'info';
  const isAssertive = Boolean(item.assertive || tone === 'danger');

  useEffect(() => {
    if (!item.durationMs || item.durationMs <= 0) {
      return;
    }
    const timer = setTimeout(() => {
      onDismiss(item.id);
    }, item.durationMs);
    return () => clearTimeout(timer);
  }, [item.id, item.durationMs, onDismiss]);

  return (
    <div
      role={isAssertive ? 'alert' : 'status'}
      aria-live={isAssertive ? 'assertive' : 'polite'}
      aria-atomic="true"
      data-toast-id={item.id}
      data-toast-tone={tone}
      className={[styles.toastItem, TONE_CLASS[tone]].join(' ')}
    >
      <div className={styles.toastContent}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          {item.statusKind ? <StatusBadge kind={item.statusKind} /> : null}
          <p className={styles.toastTitle}>{item.title}</p>
        </div>
        {item.description ? (
          <p className={styles.toastDesc}>{item.description}</p>
        ) : null}
        {item.actionLabel && item.onAction ? (
          <div>
            <Button size="sm" variant="secondary" onClick={item.onAction}>
              {item.actionLabel}
            </Button>
          </div>
        ) : null}
      </div>
      <button
        type="button"
        className={styles.toastClose}
        aria-label={`关闭提示：${item.title}`}
        onClick={() => onDismiss(item.id)}
      >
        ✕
      </button>
    </div>
  );
}

export function Toast({ toasts, onDismiss, className }: ToastProps) {
  const politeToasts = toasts.filter((t) => !t.assertive && t.tone !== 'danger');
  const assertiveToasts = toasts.filter((t) => t.assertive || t.tone === 'danger');

  return (
    <section
      aria-label="系统通知区域"
      className={[styles.toastViewport, className].filter(Boolean).join(' ')}
    >
      <div
        role="region"
        aria-label="常规状态提示"
        aria-live="polite"
        data-toast-region="polite"
        style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}
      >
        {politeToasts.map((item) => (
          <ToastEntry key={item.id} item={item} onDismiss={onDismiss} />
        ))}
      </div>
      <div
        role="region"
        aria-label="紧急告警提示"
        aria-live="assertive"
        data-toast-region="assertive"
        style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}
      >
        {assertiveToasts.map((item) => (
          <ToastEntry key={item.id} item={item} onDismiss={onDismiss} />
        ))}
      </div>
    </section>
  );
}
