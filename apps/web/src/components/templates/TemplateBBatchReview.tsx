import type { ReactNode } from 'react';
import { Button } from '../Button';
import { PageHeader } from '../PageHeader';
import type { StatusKind } from '../StatusBadge';
import { DataTable } from '../primitives/DataTable';
import type { DataTableColumn } from '../primitives/DataTable';
import { SixStateView } from '../primitives/SixStateView';
import type { SixState, SixStateError } from '../primitives/SixStateView';
import styles from './templates.module.css';

export interface TemplateBBatchReviewProps<T> {
  title: string;
  eyebrow?: string;
  source?: string;
  timestamp?: string;
  statusKind?: StatusKind;
  actions?: ReactNode;
  toolbar?: ReactNode;
  columns: DataTableColumn<T>[];
  rows: readonly T[];
  getRowId: (row: T) => string;
  selectedIds: readonly string[];
  onSelectionChange: (selectedIds: string[]) => void;
  batchActionLabel?: string;
  onBatchAction?: (selectedRows: T[]) => void;
  extraBatchActions?: ReactNode;
  summaryNotice?: string;
  virtualize?: boolean;
  rowHeight?: number;
  viewportHeight?: number;
  sidebar?: ReactNode;
  state?: SixState;
  error?: SixStateError | null;
  onRetry?: () => void;
  className?: string;
}

export function TemplateBBatchReview<T>({
  title,
  eyebrow = 'TEMPLATE B · 多选批量复核表',
  source,
  timestamp,
  statusKind,
  actions,
  toolbar,
  columns,
  rows,
  getRowId,
  selectedIds,
  onSelectionChange,
  batchActionLabel = '批量确认提交',
  onBatchAction,
  extraBatchActions,
  summaryNotice = '批量提交前将弹出影响范围确认，逐项核验乐观锁版本与审批权限。',
  virtualize,
  rowHeight,
  viewportHeight,
  sidebar,
  state = 'ready',
  error,
  onRetry,
  className,
}: TemplateBBatchReviewProps<T>) {
  const tableNode = (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', minWidth: 0 }}>
      {summaryNotice ? (
        <div
          style={{
            fontSize: 'var(--font-size-caption, 12px)',
            color: 'var(--muted, #65736f)',
          }}
        >
          {summaryNotice}
        </div>
      ) : null}
      <DataTable
        columns={columns}
        rows={rows}
        getRowId={getRowId}
        selectable
        selectedIds={selectedIds}
        onSelectionChange={onSelectionChange}
        virtualize={virtualize}
        rowHeight={rowHeight}
        viewportHeight={viewportHeight}
        caption={title}
        renderBatchBar={(selectedRows) => (
          <>
            {extraBatchActions}
            {onBatchAction ? (
              <Button
                size="sm"
                variant="primary"
                onClick={() => onBatchAction(selectedRows)}
                data-batch-submit="true"
              >
                {batchActionLabel} ({selectedRows.length})
              </Button>
            ) : null}
          </>
        )}
      />
    </div>
  );

  return (
    <section
      data-template="B"
      className={[styles.templateRoot, className].filter(Boolean).join(' ')}
    >
      <PageHeader
        title={title}
        eyebrow={eyebrow}
        source={source}
        timestamp={timestamp}
        statusKind={statusKind}
        actions={actions}
      />

      {toolbar ? <div className={styles.toolbarRow}>{toolbar}</div> : null}

      <SixStateView
        state={state}
        statusKind={statusKind}
        source={source}
        timestamp={timestamp}
        error={error}
        onRetry={onRetry}
      >
        {sidebar ? (
          <div className={styles.evidenceGrid}>
            <div className={styles.evidenceMain}>{tableNode}</div>
            <aside className={styles.evidenceSidebar} aria-label="批量复核参考信息">
              {sidebar}
            </aside>
          </div>
        ) : (
          tableNode
        )}
      </SixStateView>
    </section>
  );
}
