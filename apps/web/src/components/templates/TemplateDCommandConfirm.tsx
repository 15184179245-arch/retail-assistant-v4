import { useState } from 'react';
import { Button } from '../Button';
import { StatusBadge } from '../StatusBadge';
import type { StatusKind } from '../StatusBadge';
import { CommandConfirm } from '../primitives/CommandConfirm';
import type {
  CommandConfirmErrorInfo,
  CommandConfirmOutcome,
  CommandConfirmResult,
} from '../primitives/CommandConfirm';
import styles from './templates.module.css';

export interface TemplateDCommandConfirmProps {
  title: string;
  commandKey?: string;
  whatHappens: string;
  impactScope: string;
  whatItWontDo: string;
  approver: string;
  idempotencyKey?: string | null;
  baseVersion?: number | string | null;
  metadata?: Record<string, string | number | boolean | null | undefined>;
  triggerLabel?: string;
  confirmLabel?: string;
  onConfirm: (payload: {
    idempotencyKey: string;
    baseVersion?: number | string | null;
  }) => Promise<CommandConfirmResult | void> | CommandConfirmResult | void;
  onCheckActualStatus?: () => Promise<void> | void;
  offline?: boolean;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  receiptRef?: string | null;
  outcome?: CommandConfirmOutcome;
  error?: CommandConfirmErrorInfo | null;
  className?: string;
}

export function TemplateDCommandConfirm({
  title,
  commandKey,
  whatHappens,
  impactScope,
  whatItWontDo,
  approver,
  idempotencyKey,
  baseVersion,
  metadata,
  triggerLabel = '打开 L2 命令确认弹窗',
  confirmLabel = '确认执行正式命令',
  onConfirm,
  onCheckActualStatus,
  offline = false,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange,
  receiptRef,
  outcome,
  error,
  className,
}: TemplateDCommandConfirmProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const isOpen = controlledOpen ?? uncontrolledOpen;

  const setOpen = (next: boolean) => {
    if (controlledOpen === undefined) {
      setUncontrolledOpen(next);
    }
    onOpenChange?.(next);
  };

  const previewBadgeKind: StatusKind =
    outcome === 'verified' && receiptRef
      ? 'verified'
      : outcome === 'unverified'
        ? 'unverified'
        : outcome === 'error'
          ? 'error'
          : 'waiting';

  return (
    <section
      data-template="D"
      aria-label={`L2 命令预览：${title}`}
      className={[styles.commandCard, className].filter(Boolean).join(' ')}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '8px',
          flexWrap: 'wrap',
        }}
      >
        <div>
          <p className={styles.sideEyebrow}>TEMPLATE D · L2 正式命令确认工作流</p>
          <h3 style={{ margin: '4px 0 0', fontSize: 'var(--font-size-h2, 16px)' }}>
            {title}
          </h3>
        </div>
        <StatusBadge
          kind={previewBadgeKind}
          source={receiptRef ?? (offline ? '待网络' : '需人工确认')}
        />
      </div>

      <div className={styles.commandSummaryGrid}>
        <div className={styles.commandCell}>
          <strong>将发生什么：</strong>
          <span>{whatHappens}</span>
        </div>
        <div className={styles.commandCell}>
          <strong>影响范围：</strong>
          <span>{impactScope}</span>
        </div>
        <div className={styles.commandCell}>
          <strong>不会做什么：</strong>
          <span>{whatItWontDo}</span>
        </div>
        <div className={styles.commandCell}>
          <strong>审批人：</strong>
          <span>{approver}</span>
        </div>
      </div>

      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
        {outcome === 'unverified' ? (
          <Button
            variant="primary"
            onClick={() => setOpen(true)}
            data-template-d-check-status="true"
          >
            ? 待核实 · 先查实际状态
          </Button>
        ) : (
          <Button
            variant="primary"
            disabled={offline}
            onClick={() => setOpen(true)}
            data-template-d-trigger="true"
          >
            {offline ? '待网络（离线禁止 L2 确认）' : triggerLabel}
          </Button>
        )}
      </div>

      <CommandConfirm
        open={isOpen}
        onClose={() => setOpen(false)}
        title={title}
        commandKey={commandKey}
        whatHappens={whatHappens}
        impactScope={impactScope}
        whatItWontDo={whatItWontDo}
        approver={approver}
        idempotencyKey={idempotencyKey}
        baseVersion={baseVersion}
        metadata={metadata}
        confirmLabel={confirmLabel}
        onConfirm={onConfirm}
        onCheckActualStatus={onCheckActualStatus}
        externalOutcome={outcome}
        externalError={error}
        offline={offline}
      />
    </section>
  );
}
