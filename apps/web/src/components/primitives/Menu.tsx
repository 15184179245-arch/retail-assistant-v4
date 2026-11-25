import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { resolveMenuKeyAction } from './keyboard';
import styles from './primitives.module.css';

export interface MenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  disabled?: boolean;
  danger?: boolean;
  shortcut?: string;
}

export interface MenuProps {
  triggerLabel: string;
  triggerIcon?: ReactNode;
  items: MenuItem[];
  onSelect: (id: string) => void;
  align?: 'left' | 'right';
  disabled?: boolean;
  defaultOpen?: boolean;
  id?: string;
  className?: string;
}

export function Menu({
  triggerLabel,
  triggerIcon,
  items,
  onSelect,
  align = 'left',
  disabled = false,
  defaultOpen = false,
  id,
  className,
}: MenuProps) {
  const autoId = useId();
  const baseId = id ?? `menu-${autoId}`;
  const triggerId = `${baseId}-trigger`;
  const menuId = `${baseId}-list`;

  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [activeIndex, setActiveIndex] = useState<number>(defaultOpen ? 0 : -1);

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => {
    if (!isOpen || typeof document === 'undefined') {
      return;
    }
    if (activeIndex >= 0) {
      itemRefs.current[activeIndex]?.focus();
    }
    const handleOutside = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleOutside);
    return () => document.removeEventListener('mousedown', handleOutside);
  }, [isOpen, activeIndex]);

  const closeAndRestoreFocus = () => {
    setIsOpen(false);
    setActiveIndex(-1);
    triggerRef.current?.focus();
  };

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const action = resolveMenuKeyAction({
      key: event.key,
      isOpen,
      activeIndex,
      items,
    });
    if (action.type === 'open') {
      event.preventDefault();
      setIsOpen(true);
      setActiveIndex(action.activeIndex);
    } else if (action.type === 'close') {
      event.preventDefault();
      closeAndRestoreFocus();
    }
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const action = resolveMenuKeyAction({
      key: event.key,
      isOpen: true,
      activeIndex,
      items,
    });
    if (action.type === 'noop') {
      return;
    }
    event.preventDefault();
    if (action.type === 'highlight') {
      setActiveIndex(action.activeIndex);
      itemRefs.current[action.activeIndex]?.focus();
    } else if (action.type === 'select') {
      const selected = items[action.activeIndex];
      if (selected && !selected.disabled) {
        onSelect(selected.id);
        closeAndRestoreFocus();
      }
    } else if (action.type === 'close') {
      closeAndRestoreFocus();
    }
  };

  return (
    <div
      ref={wrapRef}
      className={[styles.menuWrap, className].filter(Boolean).join(' ')}
    >
      <button
        ref={triggerRef}
        id={triggerId}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={menuId}
        className={styles.menuTrigger}
        onClick={() => {
          if (isOpen) {
            setIsOpen(false);
            setActiveIndex(-1);
          } else {
            const firstEnabled = items.findIndex((it) => !it.disabled);
            setIsOpen(true);
            setActiveIndex(firstEnabled);
          }
        }}
        onKeyDown={handleTriggerKeyDown}
      >
        {triggerIcon ? <span aria-hidden="true">{triggerIcon}</span> : null}
        <span>{triggerLabel}</span>
      </button>

      {isOpen ? (
        <ul
          id={menuId}
          role="menu"
          aria-labelledby={triggerId}
          className={[
            styles.menuList,
            align === 'right' ? styles.menuListRight : styles.menuListLeft,
          ].join(' ')}
          onKeyDown={handleMenuKeyDown}
        >
          {items.map((item, index) => {
            const isHighlighted = index === activeIndex;
            return (
              <li key={item.id} role="none">
                <button
                  ref={(el) => {
                    itemRefs.current[index] = el;
                  }}
                  type="button"
                  role="menuitem"
                  disabled={item.disabled}
                  aria-disabled={item.disabled ? 'true' : undefined}
                  tabIndex={isHighlighted ? 0 : -1}
                  className={[
                    styles.menuItem,
                    isHighlighted ? styles.menuItemHighlighted : '',
                    item.danger ? styles.menuItemDanger : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onClick={() => {
                    if (!item.disabled) {
                      onSelect(item.id);
                      closeAndRestoreFocus();
                    }
                  }}
                >
                  <span>
                    {item.icon ? <span aria-hidden="true">{item.icon} </span> : null}
                    {item.label}
                  </span>
                  {item.shortcut ? (
                    <span className={styles.menuShortcut}>{item.shortcut}</span>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
