import type { ReactNode } from 'react';
import { EmptyState } from '../EmptyState';
import { PageHeader } from '../PageHeader';
import type { StatusKind } from '../StatusBadge';
import { SixStateView } from '../primitives/SixStateView';
import type { SixState, SixStateError } from '../primitives/SixStateView';
import styles from './templates.module.css';

export interface TemplateAListDetailProps<T> {
  title: string;
  eyebrow?: string;
  source?: string;
  timestamp?: string;
  statusKind?: StatusKind;
  actions?: ReactNode;
  filters?: ReactNode;
  items: readonly T[];
  selectedId: string | null;
  getItemId: (item: T) => string;
  onSelectItem: (item: T) => void;
  renderListItem: (item: T, selected: boolean) => ReactNode;
  renderDetail: (item: T | null) => ReactNode;
  state?: SixState;
  error?: SixStateError | null;
  onRetry?: () => void;
  emptyListTitle?: string;
  emptyDetailTitle?: string;
  className?: string;
}

export function TemplateAListDetail<T>({
  title,
  eyebrow = 'TEMPLATE A · 列表与详情分栏',
  source,
  timestamp,
  statusKind,
  actions,
  filters,
  items,
  selectedId,
  getItemId,
  onSelectItem,
  renderListItem,
  renderDetail,
  state = 'ready',
  error,
  onRetry,
  emptyListTitle = '当前筛选条件暂无待处理对象',
  emptyDetailTitle = '请从左侧列表选择一个工作对象',
  className,
}: TemplateAListDetailProps<T>) {
  const selectedItem =
    items.find((item) => getItemId(item) === selectedId) ?? null;

  return (
    <section
      data-template="A"
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

      {filters ? <div className={styles.toolbarRow}>{filters}</div> : null}

      <SixStateView
        state={items.length === 0 && state === 'ready' ? 'empty' : state}
        statusKind={statusKind}
        source={source}
        timestamp={timestamp}
        emptyTitle={emptyListTitle}
        error={error}
        onRetry={onRetry}
      >
        <div className={styles.splitGrid}>
          <div
            className={styles.listColumn}
            role="listbox"
            aria-label={`${title} 列表`}
          >
            {items.map((item) => {
              const id = getItemId(item);
              const isSelected = id === selectedId;
              return (
                <button
                  key={id}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  data-list-item-id={id}
                  className={[
                    styles.listItemBtn,
                    isSelected ? styles.listItemActive : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onClick={() => onSelectItem(item)}
                >
                  {renderListItem(item, isSelected)}
                </button>
              );
            })}
          </div>

          <section
            className={styles.detailColumn}
            aria-label="工作对象详情"
            data-template-detail="true"
          >
            {selectedItem ? (
              renderDetail(selectedItem)
            ) : (
              <EmptyState type="empty" title={emptyDetailTitle} />
            )}
          </section>
        </div>
      </SixStateView>
    </section>
  );
}
