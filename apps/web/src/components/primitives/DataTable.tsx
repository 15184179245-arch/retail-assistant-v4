import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Button } from '../Button';
import { EmptyState } from '../EmptyState';
import { computeVirtualWindow } from './keyboard';
import styles from './primitives.module.css';

export interface DataTableColumn<T> {
  key: string;
  header: string;
  render?: (row: T, index: number) => ReactNode;
  align?: 'left' | 'right' | 'center';
  width?: string;
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: readonly T[];
  getRowId: (row: T) => string;
  selectable?: boolean;
  selectedIds?: readonly string[];
  onSelectionChange?: (selectedIds: string[]) => void;
  batchActions?: ReactNode;
  renderBatchBar?: (selectedRows: T[], clearSelection: () => void) => ReactNode;
  virtualize?: boolean;
  virtualThreshold?: number;
  rowHeight?: number;
  viewportHeight?: number;
  overscan?: number;
  scrollTop?: number;
  caption?: string;
  emptyState?: ReactNode;
  onRowClick?: (row: T) => void;
  className?: string;
}

export function DataTable<T>({
  columns,
  rows,
  getRowId,
  selectable = false,
  selectedIds = [],
  onSelectionChange,
  batchActions,
  renderBatchBar,
  virtualize,
  virtualThreshold = 40,
  rowHeight = 52,
  viewportHeight = 420,
  overscan = 5,
  scrollTop: controlledScrollTop,
  caption,
  emptyState,
  onRowClick,
  className,
}: DataTableProps<T>) {
  const [internalScrollTop, setInternalScrollTop] = useState(0);
  const currentScrollTop = controlledScrollTop ?? internalScrollTop;
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds]);
  const selectedRows = useMemo(
    () => rows.filter((row) => selectedSet.has(getRowId(row))),
    [rows, selectedSet, getRowId],
  );
  const allSelected =
    rows.length > 0 && rows.every((row) => selectedSet.has(getRowId(row)));
  const someSelected = selectedRows.length > 0 && !allSelected;

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someSelected;
    }
  }, [someSelected]);

  const shouldVirtualize = virtualize ?? rows.length > virtualThreshold;
  const virtualWindow = useMemo(() => {
    if (!shouldVirtualize) {
      return {
        startIndex: 0,
        endIndex: rows.length,
        paddingTop: 0,
        paddingBottom: 0,
        totalHeight: rows.length * rowHeight,
        visibleCount: rows.length,
      };
    }
    return computeVirtualWindow({
      totalCount: rows.length,
      rowHeight,
      viewportHeight,
      scrollTop: currentScrollTop,
      overscan,
    });
  }, [shouldVirtualize, rows.length, rowHeight, viewportHeight, currentScrollTop, overscan]);

  const visibleRows = useMemo(
    () => rows.slice(virtualWindow.startIndex, virtualWindow.endIndex),
    [rows, virtualWindow.startIndex, virtualWindow.endIndex],
  );

  const totalColCount = columns.length + (selectable ? 1 : 0);

  const toggleAll = () => {
    if (!onSelectionChange) {
      return;
    }
    if (allSelected) {
      onSelectionChange([]);
    } else {
      onSelectionChange(rows.map((row) => getRowId(row)));
    }
  };

  const toggleRow = (id: string) => {
    if (!onSelectionChange) {
      return;
    }
    if (selectedSet.has(id)) {
      onSelectionChange(selectedIds.filter((x) => x !== id));
    } else {
      onSelectionChange([...selectedIds, id]);
    }
  };

  const clearSelection = () => {
    onSelectionChange?.([]);
  };

  if (rows.length === 0) {
    return (
      <div className={[styles.tableWrapper, className].filter(Boolean).join(' ')}>
        {emptyState ?? <EmptyState type="empty" title="暂无数据记录" />}
      </div>
    );
  }

  return (
    <div className={[styles.tableWrapper, className].filter(Boolean).join(' ')}>
      <div
        className={styles.tableScroll}
        style={shouldVirtualize ? { maxHeight: `${viewportHeight}px` } : undefined}
        data-virtualized={shouldVirtualize ? 'true' : 'false'}
        data-total-rows={rows.length}
        data-rendered-rows={visibleRows.length}
        onScroll={(event) => {
          if (controlledScrollTop === undefined) {
            setInternalScrollTop(event.currentTarget.scrollTop);
          }
        }}
      >
        <table className={styles.dataTable}>
          {caption ? <caption className={styles.srOnly}>{caption}</caption> : null}
          <thead className={styles.dataTableHead}>
            <tr>
              {selectable ? (
                <th className={styles.checkboxCell} scope="col">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    className={styles.checkboxInput}
                    aria-label="全选当前列表"
                    checked={allSelected}
                    onChange={toggleAll}
                  />
                </th>
              ) : null}
              {columns.map((col) => (
                <th
                  key={col.key}
                  scope="col"
                  style={{
                    width: col.width,
                    textAlign: col.align ?? 'left',
                  }}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {virtualWindow.paddingTop > 0 ? (
              <tr aria-hidden="true" data-virtual-spacer="top">
                <td
                  colSpan={totalColCount}
                  style={{ height: `${virtualWindow.paddingTop}px`, padding: 0, border: 0 }}
                />
              </tr>
            ) : null}
            {visibleRows.map((row, sliceIdx) => {
              const absoluteIdx = virtualWindow.startIndex + sliceIdx;
              const id = getRowId(row);
              const isSelected = selectedSet.has(id);
              return (
                <tr
                  key={id}
                  data-row-id={id}
                  data-row-index={absoluteIdx}
                  aria-selected={selectable ? isSelected : undefined}
                  className={[
                    styles.dataTableRow,
                    isSelected ? styles.dataTableRowSelected : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  style={shouldVirtualize ? { height: `${rowHeight}px` } : undefined}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                >
                  {selectable ? (
                    <td
                      className={styles.checkboxCell}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <input
                        type="checkbox"
                        className={styles.checkboxInput}
                        aria-label={`选择第 ${absoluteIdx + 1} 行`}
                        checked={isSelected}
                        onChange={() => toggleRow(id)}
                      />
                    </td>
                  ) : null}
                  {columns.map((col) => {
                    const rawValue = (row as Record<string, unknown>)[col.key];
                    return (
                      <td
                        key={col.key}
                        style={{ textAlign: col.align ?? 'left' }}
                      >
                        {col.render
                          ? col.render(row, absoluteIdx)
                          : rawValue !== undefined && rawValue !== null
                            ? String(rawValue)
                            : ''}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {virtualWindow.paddingBottom > 0 ? (
              <tr aria-hidden="true" data-virtual-spacer="bottom">
                <td
                  colSpan={totalColCount}
                  style={{
                    height: `${virtualWindow.paddingBottom}px`,
                    padding: 0,
                    border: 0,
                  }}
                />
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      {selectable && selectedRows.length > 0 ? (
        <div
          role="region"
          aria-label="批量操作栏"
          data-batch-bar="true"
          className={styles.batchBar}
        >
          <span className={styles.batchCount}>已选 {selectedRows.length} 项</span>
          <div className={styles.batchActions}>
            {renderBatchBar ? renderBatchBar(selectedRows, clearSelection) : batchActions}
            <Button size="sm" variant="ghost" onClick={clearSelection}>
              取消选择
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
