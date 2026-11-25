import { useEffect, useId, useRef } from 'react';
import type { KeyboardEvent, ReactNode, RefObject } from 'react';
import {
  getFocusableElements,
  resolveEscapeCloseKey,
  resolveFocusTrapNext,
} from './keyboard';
import styles from './primitives.module.css';

export interface DialogProps {
  open: boolean;
  role?: 'dialog' | 'alertdialog';
  onClose: () => void;
  title: string;
  description?: string;
  eyebrow?: string;
  children: ReactNode;
  footer?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  closeOnBackdrop?: boolean;
  closeLabel?: string;
  initialFocusRef?: RefObject<HTMLElement | null>;
  id?: string;
  className?: string;
}

export function Dialog({
  open,
  role = 'dialog',
  onClose,
  title,
  description,
  eyebrow,
  children,
  footer,
  size = 'md',
  closeOnBackdrop = true,
  closeLabel = '关闭弹窗',
  initialFocusRef,
  id,
  className,
}: DialogProps) {
  const autoId = useId();
  const baseId = id ?? `dialog-${autoId}`;
  const titleId = `${baseId}-title`;
  const descId = description ? `${baseId}-desc` : undefined;

  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open || typeof document === 'undefined') {
      return;
    }

    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const container = dialogRef.current;
    if (container) {
      const target =
        initialFocusRef?.current ?? getFocusableElements(container)[0] ?? container;
      target.focus();
    }

    const handleDocKeyDown = (event: globalThis.KeyboardEvent) => {
      if (resolveEscapeCloseKey(event.key)) {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key === 'Tab' && dialogRef.current) {
        const focusables = getFocusableElements(dialogRef.current);
        if (focusables.length === 0) {
          event.preventDefault();
          dialogRef.current.focus();
          return;
        }
        const active =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
        if (!active || !dialogRef.current.contains(active)) {
          event.preventDefault();
          focusables[0]?.focus();
          return;
        }
        const next = resolveFocusTrapNext(focusables, active, event.shiftKey);
        if (next) {
          event.preventDefault();
          next.focus();
        }
      }
    };

    document.addEventListener('keydown', handleDocKeyDown);
    return () => {
      document.removeEventListener('keydown', handleDocKeyDown);
      previousFocusRef.current?.focus?.();
    };
  }, [open, onClose, initialFocusRef]);

  if (!open) {
    return null;
  }

  const sizeClass =
    size === 'sm' ? styles.dialogSm : size === 'lg' ? styles.dialogLg : '';

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (resolveEscapeCloseKey(event.key)) {
      event.stopPropagation();
      onClose();
    }
  };

  return (
    <div
      className={styles.backdrop}
      data-dialog-backdrop="true"
      onClick={(event) => {
        if (closeOnBackdrop && event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        ref={dialogRef}
        id={baseId}
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        tabIndex={-1}
        data-dialog-size={size}
        className={[styles.dialogCard, sizeClass, className].filter(Boolean).join(' ')}
        onKeyDown={handleKeyDown}
      >
        <div className={styles.dialogHeader}>
          <div className={styles.dialogTitleWrap}>
            {eyebrow ? <p className={styles.dialogEyebrow}>{eyebrow}</p> : null}
            <h2 id={titleId} className={styles.dialogTitle}>
              {title}
            </h2>
            {description ? (
              <p id={descId} className={styles.dialogDescription}>
                {description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            className={styles.iconCloseBtn}
            aria-label={closeLabel}
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        <div className={styles.dialogBody}>{children}</div>
        {footer ? <div className={styles.dialogFooter}>{footer}</div> : null}
      </div>
    </div>
  );
}
