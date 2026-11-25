import { useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { resolveNextTabIndex } from './keyboard';
import styles from './primitives.module.css';

export interface TabItem {
  id: string;
  label: string;
  content?: ReactNode;
  badge?: string | number;
  closable?: boolean;
  disabled?: boolean;
  pinned?: boolean;
}

export interface TabsProps {
  items: TabItem[];
  activeId?: string;
  defaultActiveId?: string;
  onChange?: (id: string) => void;
  onCloseTab?: (id: string) => void;
  ariaLabel?: string;
  orientation?: 'horizontal' | 'vertical';
  renderPanel?: (item: TabItem) => ReactNode;
  className?: string;
  id?: string;
}

export function Tabs({
  items,
  activeId,
  defaultActiveId,
  onChange,
  onCloseTab,
  ariaLabel = '标签页切换',
  orientation = 'horizontal',
  renderPanel,
  className,
  id,
}: TabsProps) {
  const autoId = useId();
  const baseId = id ?? `tabs-${autoId}`;
  const [uncontrolledActiveId, setUncontrolledActiveId] = useState(
    defaultActiveId ?? items[0]?.id ?? '',
  );
  const currentActiveId = activeId ?? uncontrolledActiveId;
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const activeIndex = items.findIndex((item) => item.id === currentActiveId);
  const resolvedIndex = activeIndex >= 0 ? activeIndex : 0;
  const activeItem = items[resolvedIndex];

  const selectTab = (nextId: string) => {
    if (activeId === undefined) {
      setUncontrolledActiveId(nextId);
    }
    onChange?.(nextId);
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    const nextIndex = resolveNextTabIndex(items, index, event.key, orientation);
    if (nextIndex !== index && nextIndex >= 0) {
      const nextItem = items[nextIndex];
      if (nextItem) {
        event.preventDefault();
        selectTab(nextItem.id);
        tabRefs.current[nextItem.id]?.focus();
      }
    }
  };

  return (
    <div
      className={[
        styles.tabsRoot,
        orientation === 'vertical' ? styles.tabsVertical : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div
        role="tablist"
        aria-label={ariaLabel}
        aria-orientation={orientation}
        className={[
          styles.tabList,
          orientation === 'vertical' ? styles.tabListVertical : '',
        ]
          .filter(Boolean)
          .join(' ')}
      >
        {items.map((item, index) => {
          const isSelected = activeItem?.id === item.id;
          const tabId = `${baseId}-tab-${item.id}`;
          const panelId = `${baseId}-panel-${item.id}`;
          return (
            <div
              key={item.id}
              className={[
                styles.tabGroup,
                isSelected ? styles.tabGroupActive : '',
              ]
                .filter(Boolean)
                .join(' ')}
              data-tab-id={item.id}
            >
              <button
                ref={(el) => {
                  tabRefs.current[item.id] = el;
                }}
                type="button"
                role="tab"
                id={tabId}
                aria-selected={isSelected}
                aria-controls={panelId}
                tabIndex={isSelected ? 0 : -1}
                disabled={item.disabled}
                className={[
                  styles.tabButton,
                  isSelected ? styles.tabButtonActive : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
                onClick={() => {
                  if (!item.disabled) {
                    selectTab(item.id);
                  }
                }}
                onKeyDown={(event) => handleTabKeyDown(event, index)}
              >
                {item.pinned ? <span aria-hidden="true">📌</span> : null}
                <span>{item.label}</span>
                {item.badge !== undefined ? (
                  <span className={styles.tabBadge}>{item.badge}</span>
                ) : null}
              </button>
              {item.closable && !item.pinned && onCloseTab ? (
                <button
                  type="button"
                  className={styles.tabCloseButton}
                  aria-label={`关闭 ${item.label}`}
                  onClick={() => onCloseTab(item.id)}
                >
                  ×
                </button>
              ) : null}
            </div>
          );
        })}
      </div>

      {activeItem ? (
        <div
          role="tabpanel"
          id={`${baseId}-panel-${activeItem.id}`}
          aria-labelledby={`${baseId}-tab-${activeItem.id}`}
          tabIndex={0}
          className={styles.tabPanel}
        >
          {renderPanel ? renderPanel(activeItem) : activeItem.content}
        </div>
      ) : null}
    </div>
  );
}
