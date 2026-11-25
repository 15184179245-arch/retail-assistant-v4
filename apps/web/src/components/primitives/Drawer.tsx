import { useEffect, useId, useRef } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import {
  getFocusableElements,
  resolveEscapeCloseKey,
  resolveFocusTrapNext,
} from './keyboard';
import styles from './primitives.module.css';

export type DrawerSide = 'right' | 'left' | 'bottom';

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  side?: DrawerSide;
  children: ReactNode;
  footer?: ReactNode;
  closeOnBackdrop?: boolean;
  closeLabel?: string;
  id?: string;
  className?: string;
}

export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  side = 'right',
  children,
  footer,
  closeOnBackdrop = true,
  closeLabel = '关闭抽屉',
  id,
  className,
}: DrawerProps) {
  const autoId = useId();
  const baseId = id ?? `drawer-${autoId}`;
  const titleId = `${baseId}-title`;
  const subtitleId = subtitle ? `${baseId}-subtitle` : undefined;

  const panelRef = useRef<HTMLDivElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open || typeof document === 'undefined') {
      return;
    }
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const panel = panelRef.current;
    if (panel) {
      const firstFocusable = getFocusableElements(panel)[0] ?? panel;
      firstFocusable.focus();
    }

    const handleDocKeyDown = (event: globalThis.KeyboardEvent) => {
      if (resolveEscapeCloseKey(event.key)) {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key === 'Tab' && panelRef.current) {
        const focusables = getFocusableElements(panelRef.current);
        if (focusables.length === 0) {
          event.preventDefault();
          panelRef.current.focus();
          return;
        }
        const active =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
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
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  const backdropSideClass =
    side === 'left'
      ? styles.drawerBackdropLeft
      : side === 'bottom'
        ? styles.drawerBackdropBottom
        : styles.drawerBackdropRight;

  const panelShapeClass =
    side === 'bottom' ? styles.drawerPanelBottom : styles.drawerPanelSide;

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (resolveEscapeCloseKey(event.key)) {
      event.stopPropagation();
      onClose();
    }
  };

  return (
    <div
      className={[styles.drawerBackdrop, backdropSideClass].join(' ')}
      data-drawer-backdrop="true"
      onClick={(event) => {
        if (closeOnBackdrop && event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        ref={panelRef}
        id={baseId}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={subtitleId}
        data-drawer-side={side}
        tabIndex={-1}
        className={[styles.drawerPanel, panelShapeClass, className]
          .filter(Boolean)
          .join(' ')}
        onKeyDown={handleKeyDown}
      >
        <div className={styles.dialogHeader}>
          <div className={styles.dialogTitleWrap}>
            <h2 id={titleId} className={styles.dialogTitle}>
              {title}
            </h2>
            {subtitle ? (
              <p id={subtitleId} className={styles.dialogDescription}>
                {subtitle}
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
