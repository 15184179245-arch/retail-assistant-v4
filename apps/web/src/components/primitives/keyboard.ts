/**
 * Pure WAI-ARIA keyboard, focus-trap, virtualization, and L2 submission guard helpers.
 * Kept framework-agnostic so both React primitives and Node Vitest suites share the same logic.
 */

export interface FocusableLike {
  focus?: () => void;
  disabled?: boolean;
  tabIndex?: number;
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'details > summary',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

export function getFocusableElements(container: HTMLElement | null): HTMLElement[] {
  if (!container || typeof container.querySelectorAll !== 'function') {
    return [];
  }
  const nodes = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
  return nodes.filter((el) => {
    if (el.hasAttribute('disabled') || el.getAttribute('aria-hidden') === 'true') {
      return false;
    }
    const tabIdx = el.getAttribute('tabindex');
    return tabIdx !== '-1';
  });
}

export function resolveFocusTrapNext<T>(
  elements: readonly T[],
  current: T | null,
  shiftKey: boolean,
): T | null {
  if (elements.length === 0) {
    return null;
  }
  const currentIndex = current ? elements.indexOf(current) : -1;
  if (shiftKey) {
    if (currentIndex <= 0) {
      return elements[elements.length - 1] ?? null;
    }
    return elements[currentIndex - 1] ?? null;
  }
  if (currentIndex === -1 || currentIndex >= elements.length - 1) {
    return elements[0] ?? null;
  }
  return elements[currentIndex + 1] ?? null;
}

export function resolveEscapeCloseKey(key: string): boolean {
  return key === 'Escape';
}

export interface TabLike {
  id: string;
  disabled?: boolean;
}

export function resolveNextTabIndex(
  items: readonly TabLike[],
  currentIndex: number,
  key: string,
  orientation: 'horizontal' | 'vertical' = 'horizontal',
): number {
  const enabledIndices = items
    .map((item, index) => (item.disabled ? -1 : index))
    .filter((index) => index >= 0);
  if (enabledIndices.length === 0) {
    return -1;
  }

  const nextKey = orientation === 'horizontal' ? 'ArrowRight' : 'ArrowDown';
  const prevKey = orientation === 'horizontal' ? 'ArrowLeft' : 'ArrowUp';

  if (key === 'Home') {
    return enabledIndices[0] ?? -1;
  }
  if (key === 'End') {
    return enabledIndices[enabledIndices.length - 1] ?? -1;
  }

  const posInEnabled = enabledIndices.indexOf(currentIndex);
  if (key === nextKey) {
    if (posInEnabled === -1) {
      return enabledIndices[0] ?? -1;
    }
    return enabledIndices[(posInEnabled + 1) % enabledIndices.length] ?? -1;
  }
  if (key === prevKey) {
    if (posInEnabled === -1) {
      return enabledIndices[enabledIndices.length - 1] ?? -1;
    }
    return enabledIndices[(posInEnabled - 1 + enabledIndices.length) % enabledIndices.length] ?? -1;
  }
  return currentIndex;
}

export interface OptionLike {
  disabled?: boolean;
}

export type ComboboxAction =
  | { type: 'noop' }
  | { type: 'open'; activeIndex: number }
  | { type: 'close' }
  | { type: 'highlight'; activeIndex: number }
  | { type: 'select'; activeIndex: number };

export function findNextEnabledIndex(
  items: readonly OptionLike[],
  currentIndex: number,
  direction: 1 | -1,
): number {
  const count = items.length;
  if (count === 0) {
    return -1;
  }
  for (let step = 1; step <= count; step++) {
    const candidate =
      currentIndex < 0
        ? direction === 1
          ? step - 1
          : count - step
        : (currentIndex + direction * step + count * 2) % count;
    if (!items[candidate]?.disabled) {
      return candidate;
    }
  }
  return -1;
}

export function findEdgeEnabledIndex(
  items: readonly OptionLike[],
  edge: 'first' | 'last',
): number {
  if (edge === 'first') {
    return items.findIndex((item) => !item.disabled);
  }
  for (let i = items.length - 1; i >= 0; i--) {
    if (!items[i]?.disabled) {
      return i;
    }
  }
  return -1;
}

export function resolveComboboxKeyAction(params: {
  key: string;
  isOpen: boolean;
  activeIndex: number;
  options: readonly OptionLike[];
}): ComboboxAction {
  const { key, isOpen, activeIndex, options } = params;
  if (key === 'ArrowDown') {
    const nextIndex = isOpen
      ? findNextEnabledIndex(options, activeIndex, 1)
      : findEdgeEnabledIndex(options, 'first');
    return isOpen
      ? { type: 'highlight', activeIndex: nextIndex }
      : { type: 'open', activeIndex: nextIndex };
  }
  if (key === 'ArrowUp') {
    const nextIndex = isOpen
      ? findNextEnabledIndex(options, activeIndex, -1)
      : findEdgeEnabledIndex(options, 'last');
    return isOpen
      ? { type: 'highlight', activeIndex: nextIndex }
      : { type: 'open', activeIndex: nextIndex };
  }
  if (key === 'Home' && isOpen) {
    return { type: 'highlight', activeIndex: findEdgeEnabledIndex(options, 'first') };
  }
  if (key === 'End' && isOpen) {
    return { type: 'highlight', activeIndex: findEdgeEnabledIndex(options, 'last') };
  }
  if (key === 'Enter' && isOpen && activeIndex >= 0 && !options[activeIndex]?.disabled) {
    return { type: 'select', activeIndex };
  }
  if (key === 'Escape' && isOpen) {
    return { type: 'close' };
  }
  return { type: 'noop' };
}

export type MenuAction =
  | { type: 'noop' }
  | { type: 'open'; activeIndex: number }
  | { type: 'close' }
  | { type: 'highlight'; activeIndex: number }
  | { type: 'select'; activeIndex: number };

export function resolveMenuKeyAction(params: {
  key: string;
  isOpen: boolean;
  activeIndex: number;
  items: readonly OptionLike[];
}): MenuAction {
  const { key, isOpen, activeIndex, items } = params;
  if (!isOpen) {
    if (key === 'ArrowDown' || key === 'Enter' || key === ' ') {
      return { type: 'open', activeIndex: findEdgeEnabledIndex(items, 'first') };
    }
    if (key === 'ArrowUp') {
      return { type: 'open', activeIndex: findEdgeEnabledIndex(items, 'last') };
    }
    return { type: 'noop' };
  }

  if (key === 'ArrowDown') {
    return { type: 'highlight', activeIndex: findNextEnabledIndex(items, activeIndex, 1) };
  }
  if (key === 'ArrowUp') {
    return { type: 'highlight', activeIndex: findNextEnabledIndex(items, activeIndex, -1) };
  }
  if (key === 'Home') {
    return { type: 'highlight', activeIndex: findEdgeEnabledIndex(items, 'first') };
  }
  if (key === 'End') {
    return { type: 'highlight', activeIndex: findEdgeEnabledIndex(items, 'last') };
  }
  if (key === 'Enter' || key === ' ') {
    if (activeIndex >= 0 && !items[activeIndex]?.disabled) {
      return { type: 'select', activeIndex };
    }
    return { type: 'noop' };
  }
  if (key === 'Escape' || key === 'Tab') {
    return { type: 'close' };
  }
  return { type: 'noop' };
}

export interface VirtualWindowInput {
  totalCount: number;
  rowHeight: number;
  viewportHeight: number;
  scrollTop: number;
  overscan?: number;
}

export interface VirtualWindowResult {
  startIndex: number;
  endIndex: number;
  paddingTop: number;
  paddingBottom: number;
  totalHeight: number;
  visibleCount: number;
}

export function computeVirtualWindow({
  totalCount,
  rowHeight,
  viewportHeight,
  scrollTop,
  overscan = 5,
}: VirtualWindowInput): VirtualWindowResult {
  if (totalCount <= 0 || rowHeight <= 0) {
    return {
      startIndex: 0,
      endIndex: 0,
      paddingTop: 0,
      paddingBottom: 0,
      totalHeight: 0,
      visibleCount: 0,
    };
  }
  const safeScrollTop = Math.max(0, scrollTop);
  const safeViewportHeight = Math.max(rowHeight, viewportHeight);
  const totalHeight = totalCount * rowHeight;
  const firstVisible = Math.floor(safeScrollTop / rowHeight);
  const visibleRows = Math.ceil(safeViewportHeight / rowHeight);
  const startIndex = Math.max(0, firstVisible - overscan);
  const endIndex = Math.min(totalCount, firstVisible + visibleRows + overscan);
  const paddingTop = startIndex * rowHeight;
  const paddingBottom = Math.max(0, (totalCount - endIndex) * rowHeight);

  return {
    startIndex,
    endIndex,
    paddingTop,
    paddingBottom,
    totalHeight,
    visibleCount: endIndex - startIndex,
  };
}

export interface CommandConfirmGuardState {
  inFlight: boolean;
  outcome: 'idle' | 'submitting' | 'verified' | 'unverified' | 'error';
}

export function canSubmitCommandConfirm(params: {
  idempotencyKey?: string | null;
  offline?: boolean;
  disableSubmit?: boolean;
  state: CommandConfirmGuardState;
}): { allowed: boolean; reason?: string } {
  const { idempotencyKey, offline, disableSubmit, state } = params;
  if (disableSubmit) {
    return { allowed: false, reason: 'SUBMIT_DISABLED' };
  }
  if (offline) {
    return { allowed: false, reason: 'OFFLINE_BLOCKED' };
  }
  if (!idempotencyKey || idempotencyKey.trim().length === 0) {
    return { allowed: false, reason: 'MISSING_IDEMPOTENCY_KEY' };
  }
  if (state.inFlight || state.outcome === 'submitting') {
    return { allowed: false, reason: 'IN_FLIGHT_DUPLICATE_BLOCKED' };
  }
  if (state.outcome === 'unverified') {
    return { allowed: false, reason: 'UNVERIFIED_MUST_CHECK_STATUS' };
  }
  return { allowed: true };
}
