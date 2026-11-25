import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '../Button';
import { ErrorPanel } from '../ErrorPanel';
import { StatusBadge } from '../StatusBadge';
import { Dialog } from './Dialog';
import { canSubmitCommandConfirm } from './keyboard';
import styles from './primitives.module.css';

export type CommandConfirmOutcome =
  | 'idle'
  | 'submitting'
  | 'verified'
  | 'unverified'
  | 'error';

export interface CommandConfirmErrorInfo {
  userMessage: string;
  hint?: string | null;
  code?: string | null;
}

export interface CommandConfirmResult {
  status: 'verified' | 'unverified' | 'error';
  userMessage?: string;
  hint?: string | null;
  code?: string | null;
  receiptRef?: string | null;
}

export interface CommandConfirmProps {
  open: boolean;
  onClose: () => void;
  title: string;
  commandKey?: string;
  whatHappens: string;
  impactScope: string;
  whatItWontDo: string;
  approver: string;
  idempotencyKey?: string | null;
  baseVersion?: number | string | null;
  metadata?: Record<string, string | number | boolean | null | undefined>;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: (payload: {
    idempotencyKey: string;
    baseVersion?: number | string | null;
  }) => Promise<CommandConfirmResult | void> | CommandConfirmResult | void;
  onCheckActualStatus?: () => Promise<void> | void;
  externalOutcome?: CommandConfirmOutcome;
  externalError?: CommandConfirmErrorInfo | null;
  offline?: boolean;
  disableSubmit?: boolean;
  id?: string;
}

function isTimeoutOrUnknownError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) {
    return false;
  }
  const err = error as Record<string, unknown>;
  const code = typeof err.code === 'string' ? err.code.toUpperCase() : '';
  const status = typeof err.status === 'number' ? err.status : 0;
  return (
    err.isTimeout === true ||
    code === 'TIMEOUT' ||
    code === 'UNKNOWN_OUTCOME' ||
    code === 'UNVERIFIED' ||
    status === 408 ||
    status === 504
  );
}

function extractErrorInfo(error: unknown): CommandConfirmErrorInfo {
  if (typeof error === 'object' && error !== null) {
    const err = error as Record<string, unknown>;
    const userMessage =
      (typeof err.userMessage === 'string' && err.userMessage) ||
      (typeof err.user_message === 'string' && err.user_message) ||
      (typeof err.message === 'string' && err.message) ||
      '命令执行失败，请稍后排查。';
    const hint =
      typeof err.hint === 'string' && err.hint.trim().length > 0
        ? err.hint
        : null;
    const code = typeof err.code === 'string' ? err.code : null;
    return { userMessage, hint, code };
  }
  return {
    userMessage: '命令执行失败，请稍后重试。',
    hint: '请核对网络状态或单据版本后再试。',
    code: 'COMMAND_ERROR',
  };
}

export function CommandConfirm({
  open,
  onClose,
  title,
  commandKey,
  whatHappens,
  impactScope,
  whatItWontDo,
  approver,
  idempotencyKey,
  baseVersion,
  metadata,
  confirmLabel = '确认执行',
  cancelLabel = '取消',
  onConfirm,
  onCheckActualStatus,
  externalOutcome,
  externalError,
  offline = false,
  disableSubmit = false,
  id,
}: CommandConfirmProps) {
  const [internalOutcome, setInternalOutcome] =
    useState<CommandConfirmOutcome>('idle');
  const [internalError, setInternalError] =
    useState<CommandConfirmErrorInfo | null>(null);
  const [checkingStatus, setCheckingStatus] = useState(false);

  // Synchronous ref lock guarantees double-click in the same tick cannot fire twice.
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!open) {
      inFlightRef.current = false;
      setInternalOutcome('idle');
      setInternalError(null);
      setCheckingStatus(false);
    }
  }, [open]);

  const outcome = externalOutcome ?? internalOutcome;
  const activeError = externalError ?? internalError;

  const missingIdempotencyKey =
    !idempotencyKey || idempotencyKey.trim().length === 0;

  const guard = canSubmitCommandConfirm({
    idempotencyKey,
    offline,
    disableSubmit,
    state: {
      inFlight: inFlightRef.current,
      outcome,
    },
  });

  const handleSubmit = useCallback(async () => {
    const check = canSubmitCommandConfirm({
      idempotencyKey,
      offline,
      disableSubmit,
      state: {
        inFlight: inFlightRef.current,
        outcome: externalOutcome ?? internalOutcome,
      },
    });
    if (!check.allowed || !idempotencyKey) {
      return;
    }

    inFlightRef.current = true;
    setInternalOutcome('submitting');
    setInternalError(null);

    try {
      const result = await onConfirm({
        idempotencyKey,
        baseVersion,
      });
      if (result && result.status === 'unverified') {
        setInternalOutcome('unverified');
      } else if (result && result.status === 'error') {
        setInternalOutcome('error');
        setInternalError({
          userMessage: result.userMessage ?? '命令执行被服务端拒绝。',
          hint: result.hint ?? null,
          code: result.code ?? null,
        });
      } else {
        setInternalOutcome('verified');
      }
    } catch (err) {
      if (isTimeoutOrUnknownError(err)) {
        setInternalOutcome('unverified');
        setInternalError(null);
      } else {
        setInternalOutcome('error');
        setInternalError(extractErrorInfo(err));
      }
    } finally {
      inFlightRef.current = false;
    }
  }, [
    idempotencyKey,
    offline,
    disableSubmit,
    externalOutcome,
    internalOutcome,
    onConfirm,
    baseVersion,
  ]);

  const handleCheckStatus = async () => {
    if (!onCheckActualStatus || checkingStatus) {
      return;
    }
    setCheckingStatus(true);
    try {
      await onCheckActualStatus();
    } finally {
      setCheckingStatus(false);
    }
  };

  const footer =
    outcome === 'unverified' ? (
      <>
        <Button variant="secondary" onClick={onClose}>
          稍后处理
        </Button>
        <Button
          variant="primary"
          loading={checkingStatus}
          disabled={!onCheckActualStatus || checkingStatus}
          onClick={() => void handleCheckStatus()}
          data-check-actual-status="true"
        >
          先查实际状态
        </Button>
      </>
    ) : (
      <>
        <Button
          variant="secondary"
          disabled={outcome === 'submitting'}
          onClick={onClose}
        >
          {cancelLabel}
        </Button>
        <Button
          variant="primary"
          loading={outcome === 'submitting'}
          disabled={!guard.allowed}
          onClick={() => void handleSubmit()}
          data-command-confirm-submit="true"
        >
          {outcome === 'submitting' ? '提交中…' : confirmLabel}
        </Button>
      </>
    );

  return (
    <Dialog
      open={open}
      onClose={() => {
        if (outcome !== 'submitting') {
          onClose();
        }
      }}
      id={id}
      eyebrow="L2 正式命令确认 · 不可静默越权"
      title={title}
      footer={footer}
    >
      <div className={styles.confirmSectionList}>
        <div className={styles.confirmBox} data-confirm-section="what-happens">
          <strong>将发生什么：</strong>
          <span>{whatHappens}</span>
        </div>
        <div className={styles.confirmBox} data-confirm-section="impact-scope">
          <strong>影响范围：</strong>
          <span>{impactScope}</span>
        </div>
        <div className={styles.confirmBox} data-confirm-section="what-it-wont-do">
          <strong>不会做什么：</strong>
          <span>{whatItWontDo}</span>
        </div>
        <div className={styles.confirmBox} data-confirm-section="approver">
          <strong>审批人：</strong>
          <span>{approver}</span>
        </div>
      </div>

      {offline ? (
        <div className={styles.warningBanner} role="alert" data-offline-l2-block="true">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <StatusBadge kind="waiting" source="待网络" />
            <strong>当前处于离线或服务不可达状态</strong>
          </div>
          <span>离线期间严禁提交 L2 正式命令，请等待网络恢复后再确认执行。</span>
        </div>
      ) : null}

      {missingIdempotencyKey ? (
        <div
          className={styles.warningBanner}
          role="alert"
          data-missing-idempotency-key="true"
        >
          <strong>缺少幂等键（idempotencyKey）</strong>
          <span>为防止重复创建正式单据或重复扣账，系统已禁止本次提交。</span>
        </div>
      ) : null}

      {outcome === 'unverified' ? (
        <div
          className={styles.warningBanner}
          role="alert"
          data-unverified-outcome="true"
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
            <StatusBadge kind="unverified" />
            <strong>请求已超时或外部结果未知</strong>
          </div>
          <span>
            外部写操作可能已生效或仍在处理，严禁自动或手动重发提交。请先点击「先查实际状态」向服务端核实回执。
          </span>
        </div>
      ) : null}

      {outcome === 'error' && activeError ? (
        <ErrorPanel
          userMessage={activeError.userMessage}
          hint={activeError.hint}
          code={activeError.code}
        />
      ) : null}

      <details className={styles.techDetails} data-command-tech-details="true">
        <summary>技术详情（幂等键与版本信息）</summary>
        <div className={styles.techDetailsBody}>
          <div>
            idempotencyKey:{' '}
            <code>{missingIdempotencyKey ? '缺失' : idempotencyKey}</code>
          </div>
          <div>
            base_version:{' '}
            <code>
              {baseVersion !== undefined && baseVersion !== null
                ? String(baseVersion)
                : '未指定'}
            </code>
          </div>
          {commandKey ? (
            <div>
              commandKey: <code>{commandKey}</code>
            </div>
          ) : null}
          {metadata
            ? Object.entries(metadata).map(([k, v]) => (
                <div key={k}>
                  {k}: <code>{v === null || v === undefined ? 'null' : String(v)}</code>
                </div>
              ))
            : null}
        </div>
      </details>
    </Dialog>
  );
}
