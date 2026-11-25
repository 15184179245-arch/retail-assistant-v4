import type { ReactNode } from 'react';
import { Button } from '../Button';
import { EmptyState } from '../EmptyState';
import { ErrorPanel } from '../ErrorPanel';
import { StatusBadge } from '../StatusBadge';
import type { StatusKind } from '../StatusBadge';
import styles from './primitives.module.css';

export type SixState =
  | 'loading'
  | 'empty'
  | 'forbidden'
  | 'offline'
  | 'error'
  | 'ready';

export interface SixStateError {
  userMessage: string;
  hint?: string | null;
  code?: string | null;
}

export interface SixStateViewProps {
  state: SixState;
  statusKind?: StatusKind;
  source?: string;
  timestamp?: string;
  stale?: boolean;
  loadingLabel?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
  forbiddenTitle?: string;
  forbiddenDescription?: string;
  offlineTitle?: string;
  offlineDescription?: string;
  manualFallbackAction?: ReactNode;
  error?: SixStateError | null;
  onRetry?: () => void;
  retryLabel?: string;
  children?: ReactNode;
  className?: string;
}

export function SixStateView({
  state,
  statusKind,
  source,
  timestamp,
  stale = false,
  loadingLabel = '正在加载工作对象数据…',
  emptyTitle = '暂无相关工作记录',
  emptyDescription,
  emptyAction,
  forbiddenTitle = '当前账号无权查看此工作区',
  forbiddenDescription,
  offlineTitle = '当前处于离线或模型不可用状态',
  offlineDescription = '离线与模型故障绝不阻断现场扫码、商品查询与手工草稿编辑；待网络恢复后再同步提交。',
  manualFallbackAction,
  error,
  onRetry,
  retryLabel = '重试加载',
  children,
  className,
}: SixStateViewProps) {
  const effectiveReadyBadge: StatusKind | undefined = stale
    ? 'unverified'
    : statusKind;

  return (
    <div
      data-six-state={state}
      className={[styles.sixStateRoot, className].filter(Boolean).join(' ')}
    >
      {state === 'loading' ? (
        <div
          className={styles.skeletonBox}
          role="status"
          aria-busy="true"
          aria-label={loadingLabel}
        >
          <div className={styles.auditHeaderBar}>
            <StatusBadge kind="waiting" source={source ?? '同步中'} />
            <span>{loadingLabel}</span>
          </div>
          <div className={[styles.skeletonLine, styles.skeletonLineShort].join(' ')} />
          <div className={styles.skeletonLine} />
          <div className={[styles.skeletonLine, styles.skeletonLineMid].join(' ')} />
        </div>
      ) : null}

      {state === 'empty' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {source || statusKind ? (
            <div className={styles.auditHeaderBar}>
              <StatusBadge kind={stale ? 'unverified' : statusKind ?? 'fact'} source={source} />
              {timestamp ? <span>时点：{timestamp}</span> : null}
            </div>
          ) : null}
          <EmptyState
            type="empty"
            title={emptyTitle}
            description={emptyDescription}
            action={emptyAction}
          />
        </div>
      ) : null}

      {state === 'forbidden' ? (
        <div data-forbidden-view="true" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div className={styles.auditHeaderBar}>
            <StatusBadge kind="error" source={source ?? '权限不足'} />
          </div>
          <EmptyState
            type="forbidden"
            title={forbiddenTitle}
            description={forbiddenDescription}
          />
        </div>
      ) : null}

      {state === 'offline' ? (
        <div className={styles.offlineCard} role="status">
          <div className={styles.auditHeaderBar}>
            <StatusBadge kind="unverified" source={source ?? '待网络'} />
            {timestamp ? <span>最后同步：{timestamp}</span> : null}
          </div>
          <strong style={{ fontSize: 'var(--font-size-h2, 16px)' }}>
            {offlineTitle}
          </strong>
          <p style={{ margin: 0, fontSize: 'var(--font-size-small, 13px)', lineHeight: 1.55 }}>
            {offlineDescription}
          </p>
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {manualFallbackAction}
            {onRetry ? (
              <Button size="sm" variant="secondary" onClick={onRetry}>
                {retryLabel}
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}

      {state === 'error' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div className={styles.auditHeaderBar}>
            <StatusBadge kind="error" source={source} />
            {timestamp ? <span>时点：{timestamp}</span> : null}
          </div>
          <ErrorPanel
            userMessage={error?.userMessage ?? '数据加载失败，请稍后重试。'}
            hint={error?.hint ?? '请检查服务连接或刷新当前对象。'}
            code={error?.code ?? 'LOAD_ERROR'}
            onRetry={onRetry}
            retryLabel={retryLabel}
          />
        </div>
      ) : null}

      {state === 'ready' ? (
        <>
          {effectiveReadyBadge || source || timestamp ? (
            <div className={styles.auditHeaderBar} data-ready-audit-bar="true">
              {effectiveReadyBadge ? (
                <StatusBadge kind={effectiveReadyBadge} source={source} />
              ) : source ? (
                <span>来源：{source}</span>
              ) : null}
              {timestamp ? <span>时点：{timestamp}</span> : null}
              {stale ? <span>数据已超过有效期，已降级标为待核实</span> : null}
            </div>
          ) : null}
          {children}
        </>
      ) : null}
    </div>
  );
}
