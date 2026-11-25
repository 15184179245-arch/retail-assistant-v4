import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { resolveComboboxKeyAction } from './keyboard';
import styles from './primitives.module.css';

export interface ComboboxOption {
  value: string;
  label: string;
  description?: string;
  meta?: string;
  disabled?: boolean;
}

export interface ComboboxProps {
  options: ComboboxOption[];
  value?: string;
  onChange: (value: string, option: ComboboxOption) => void;
  query?: string;
  onQueryChange?: (query: string) => void;
  placeholder?: string;
  label?: string;
  id?: string;
  disabled?: boolean;
  emptyText?: string;
  defaultOpen?: boolean;
  className?: string;
}

export function filterComboboxOptions(
  options: readonly ComboboxOption[],
  query: string,
): ComboboxOption[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) {
    return [...options];
  }
  return options.filter((opt) => {
    const haystack = [opt.label, opt.value, opt.description ?? '', opt.meta ?? '']
      .join(' ')
      .toLowerCase();
    return haystack.includes(normalized);
  });
}

export function Combobox({
  options,
  value,
  onChange,
  query,
  onQueryChange,
  placeholder = '搜索或选择…',
  label,
  id,
  disabled = false,
  emptyText = '未找到匹配项',
  defaultOpen = false,
  className,
}: ComboboxProps) {
  const autoId = useId();
  const inputId = id ?? `combobox-${autoId}`;
  const listboxId = `${inputId}-listbox`;

  const selectedOption = options.find((opt) => opt.value === value) ?? null;
  const [internalQuery, setInternalQuery] = useState(selectedOption?.label ?? '');
  const [isFiltering, setIsFiltering] = useState(false);
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [activeIndex, setActiveIndex] = useState<number>(-1);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const currentQuery = query !== undefined ? query : internalQuery;
  const effectiveFilterQuery =
    query !== undefined ? query : isFiltering ? internalQuery : '';

  const filtered = useMemo(
    () => filterComboboxOptions(options, effectiveFilterQuery),
    [options, effectiveFilterQuery],
  );

  useEffect(() => {
    if (query === undefined && selectedOption && !isOpen) {
      setInternalQuery(selectedOption.label);
      setIsFiltering(false);
    }
  }, [query, selectedOption, isOpen]);

  useEffect(() => {
    if (!isOpen || typeof document === 'undefined') {
      return;
    }
    const handleOutside = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setIsOpen(false);
        setIsFiltering(false);
      }
    };
    document.addEventListener('mousedown', handleOutside);
    return () => document.removeEventListener('mousedown', handleOutside);
  }, [isOpen]);

  const updateQuery = (next: string) => {
    if (query === undefined) {
      setInternalQuery(next);
    }
    onQueryChange?.(next);
  };

  const commitOption = (option: ComboboxOption) => {
    if (option.disabled) {
      return;
    }
    updateQuery(option.label);
    setIsFiltering(false);
    setIsOpen(false);
    setActiveIndex(-1);
    onChange(option.value, option);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const action = resolveComboboxKeyAction({
      key: event.key,
      isOpen,
      activeIndex,
      options: filtered,
    });
    if (action.type === 'noop') {
      return;
    }
    event.preventDefault();
    if (action.type === 'open') {
      setIsOpen(true);
      setActiveIndex(action.activeIndex);
    } else if (action.type === 'highlight') {
      setActiveIndex(action.activeIndex);
    } else if (action.type === 'select') {
      const opt = filtered[action.activeIndex];
      if (opt) {
        commitOption(opt);
      }
    } else if (action.type === 'close') {
      setIsOpen(false);
      setIsFiltering(false);
      setActiveIndex(-1);
    }
  };

  const activeOptionId =
    isOpen && activeIndex >= 0 && filtered[activeIndex]
      ? `${listboxId}-opt-${activeIndex}`
      : undefined;

  return (
    <div
      ref={wrapRef}
      className={[styles.comboboxWrap, className].filter(Boolean).join(' ')}
    >
      {label ? (
        <label htmlFor={inputId} className={styles.comboboxLabel}>
          {label}
        </label>
      ) : null}
      <div className={styles.comboboxInputRow}>
        <input
          id={inputId}
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={isOpen}
          aria-controls={listboxId}
          aria-activedescendant={activeOptionId}
          aria-label={label ? undefined : placeholder}
          disabled={disabled}
          value={currentQuery}
          placeholder={placeholder}
          className={styles.comboboxInput}
          onFocus={() => {
            if (!disabled) {
              setIsOpen(true);
            }
          }}
          onChange={(event) => {
            setIsFiltering(true);
            updateQuery(event.target.value);
            setIsOpen(true);
            setActiveIndex(0);
          }}
          onKeyDown={handleKeyDown}
        />
      </div>
      {isOpen ? (
        <ul
          id={listboxId}
          role="listbox"
          aria-label={label ?? placeholder}
          className={styles.comboboxListbox}
        >
          {filtered.length === 0 ? (
            <li className={styles.comboboxEmpty} role="presentation">
              {emptyText}
            </li>
          ) : (
            filtered.map((opt, index) => {
              const isSelected = opt.value === value;
              const isActive = index === activeIndex;
              return (
                <li
                  key={opt.value}
                  id={`${listboxId}-opt-${index}`}
                  role="option"
                  aria-selected={isSelected}
                  aria-disabled={opt.disabled ? 'true' : undefined}
                  className={[
                    styles.comboboxOption,
                    isActive ? styles.comboboxOptionActive : '',
                    isSelected ? styles.comboboxOptionSelected : '',
                    opt.disabled ? styles.comboboxOptionDisabled : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onMouseEnter={() => setActiveIndex(index)}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    commitOption(opt);
                  }}
                >
                  <span>{opt.label}</span>
                  {opt.description || opt.meta ? (
                    <span className={styles.comboboxOptionMeta}>
                      {[opt.description, opt.meta].filter(Boolean).join(' · ')}
                    </span>
                  ) : null}
                </li>
              );
            })
          )}
        </ul>
      ) : null}
    </div>
  );
}
