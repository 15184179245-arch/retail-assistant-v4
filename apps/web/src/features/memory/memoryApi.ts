import { apiRequest } from '../../lib/api/client';
import { beginScopedRequest, getScopeVersion, getSession, sessionIsExpired, subscribe } from '../../lib/auth/session';
import type { Session } from '../../lib/auth/session';
import { hasCapability, subscribeCapabilities } from '../../lib/auth/capabilities';

export interface Memory {
  id: string; content: string; purpose: string; source: string; version: number;
  active: boolean; retain_until_withdrawn: boolean; expires_at: string | null;
  created_at: string; updated_at: string;
}
export interface Note { filename: string; markdown: string; memory_id: string; version: number; authority: 'reference_only' }
type Operation = 'save' | 'correct' | 'withdraw';
interface Pending { operation: Operation; key: string }
export interface Storage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }
interface View { items: Memory[]; busy: boolean; pending: boolean; notice: string; note?: Note; privacyEpoch: number }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('服务端返回格式异常。');
  return value as Record<string, unknown>;
}
export function parseMemory(value: unknown): Memory {
  const v = record(value);
  if (typeof v.id !== 'string' || !uuid.test(v.id) ||
      ['content', 'purpose', 'source'].some(k => typeof v[k] !== 'string') ||
      !Number.isSafeInteger(v.version) || Number(v.version) < 1 ||
      typeof v.active !== 'boolean' || typeof v.retain_until_withdrawn !== 'boolean' ||
      ['created_at', 'updated_at'].some(k => typeof v[k] !== 'string' || !Number.isFinite(Date.parse(v[k] as string))) ||
      (v.retain_until_withdrawn ? v.expires_at !== null :
        typeof v.expires_at !== 'string' || !Number.isFinite(Date.parse(v.expires_at)))) throw Error('记忆记录格式异常。');
  return v as unknown as Memory;
}
export class MemoryController {
  private view: View = { items: [], busy: false, pending: false, notice: '正在读取助理记忆…', privacyEpoch: 0 };
  private listeners = new Set<() => void>();
  private active = false;
  private generation = 0;
  private version: number;
  private unsub: (() => void)[] = [];
  private expiryTimer?: ReturnType<typeof setInterval>;
  readonly journalKey: string;
  constructor(readonly session: Session, private storage: Storage, private baseUrl = '') {
    this.version = getScopeVersion();
    this.journalKey = 'v4-memory-operation:' + JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
  }
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getSnapshot = () => this.view;
  isCurrent = () => this.active && this.version === getScopeVersion() && getSession() === this.session && !sessionIsExpired(this.session);
  private publish(next: Partial<View>) { this.view = { ...this.view, ...next }; this.listeners.forEach(fn => fn()); }
  private hide() { this.generation++; this.publish({ items: [], note: undefined, busy: false, privacyEpoch: this.view.privacyEpoch + 1, notice: '身份或权限已改变，请重新进入。' }); }
  start() {
    if (this.active) return;
    this.active = true;
    this.unsub = [subscribe(() => { if (!this.isCurrent()) this.hide(); }),
      subscribeCapabilities(() => { if (!hasCapability('memory.read')) this.hide(); })];
    this.expiryTimer = setInterval(() => {
      if (!this.isCurrent()) { this.hide(); clearInterval(this.expiryTimer); return; }
      const items = this.view.items.filter(x => x.expires_at === null || Date.parse(x.expires_at) > Date.now());
      if (items.length !== this.view.items.length) this.publish({ items, note: undefined, privacyEpoch: this.view.privacyEpoch + 1 });
    }, 1000);
    void this.refresh();
  }
  dispose() { this.active = false; this.generation++; clearInterval(this.expiryTimer); this.unsub.forEach(fn => fn()); this.unsub = []; this.publish({ busy: false }); }
  private async request(path: string, options: Parameters<typeof apiRequest>[1] = {}) {
    if (!this.isCurrent() || !hasCapability('memory.read')) throw Error('身份或权限已改变。');
    const generation = this.generation;
    const scoped = beginScopedRequest();
    try {
      const result = await apiRequest<unknown>('/api/v1/memory' + path, {
        ...options, token: this.session.accessToken, baseUrl: this.baseUrl, signal: scoped.signal,
      });
      if (!scoped.isCurrent() || !this.isCurrent() || generation !== this.generation || !hasCapability('memory.read')) throw Error('身份或权限已改变。');
      return result;
    } catch (error) {
      if (this.isCurrent() && error && typeof error === 'object' && 'status' in error && [401, 403].includes(Number(error.status))) this.hide();
      throw error;
    } finally { scoped.finish(); }
  }
  private pending(): Pending | null {
    const raw = this.storage.getItem(this.journalKey);
    if (raw === null) return null;
    const value = record(JSON.parse(raw));
    if (Object.keys(value).sort().join(',') !== 'key,operation' ||
        !['save', 'correct', 'withdraw'].includes(String(value.operation)) || typeof value.key !== 'string' || !uuid.test(value.key)) throw Error('操作记录无法核对，已停止写入。');
    return value as unknown as Pending;
  }
  private clearPending(p: Pending) {
    const current = this.pending();
    if (!current || current.key !== p.key || current.operation !== p.operation) throw Error('操作记录已改变。');
    this.storage.removeItem(this.journalKey);
    if (this.storage.getItem(this.journalKey) !== null) throw Error('无法保存操作核对结果。');
    this.publish({ pending: false });
  }
  private async reconcile() {
    const p = this.pending(); this.publish({ pending: !!p });
    if (!p) return true;
    if (!globalThis.navigator?.locks) throw Error('需支持 Web Locks 的浏览器核对原操作。');
    return navigator.locks.request(this.journalKey, async () => {
      const current = this.pending();
      if (!current) { this.publish({ pending: false }); return true; }
      const result = record(await this.request(`/commands/${current.operation}/${current.key}`));
      if (result.operation !== current.operation || result.key !== current.key || !['committed', 'unobserved'].includes(String(result.status))) throw Error('操作回执不匹配。');
      if (result.status === 'committed') { this.clearPending(current); return true; }
      this.publish({ notice: '原操作结果尚未确认。请稍后核对；不会重复提交。' });
      return false;
    });
  }
  private async list() {
    const result = record(await this.request(''));
    if (!Array.isArray(result.items)) throw Error('记忆列表格式异常。');
    const items = result.items.map(parseMemory);
    if (new Set(items.map(x => x.id)).size !== items.length || items.some(x => !x.active)) throw Error('记忆列表不一致。');
    return items.filter(x => x.expires_at === null || Date.parse(x.expires_at) > Date.now());
  }
  async refresh() {
    if (!this.isCurrent() || this.view.busy || !hasCapability('memory.read')) return;
    this.publish({ busy: true, note: undefined }); const gen = ++this.generation;
    try {
      const settled = await this.reconcile(); const items = await this.list();
      if (this.isCurrent() && gen === this.generation) this.publish({ items, ...(settled ? { notice: '已读取当前记忆。获授权智能体共用这些记录。' } : {}) });
    } catch (e) { if (this.isCurrent() && gen === this.generation) this.publish({ items: [], notice: String(e) }); }
    finally { if (this.isCurrent() && gen === this.generation) this.publish({ busy: false }); }
  }
  async exportNote(id: string) {
    if (!uuid.test(id) || this.view.busy || !this.isCurrent()) return;
    this.publish({ busy: true, note: undefined }); const gen = ++this.generation;
    try {
      const v = record(await this.request(`/${id}/note`));
      if (v.memory_id !== id || v.filename !== `memory-${id}.md` || v.authority !== 'reference_only' ||
          typeof v.markdown !== 'string' || new TextEncoder().encode(v.markdown).length > 32768 ||
          !Number.isSafeInteger(v.version) || Number(v.version) < 1) throw Error('笔记格式异常。');
      if (this.isCurrent() && gen === this.generation) this.publish({ note: v as unknown as Note, notice: '笔记已准备，可下载或编辑正文后回导。' });
    } catch(e) { if (this.isCurrent() && gen === this.generation) this.publish({ notice: String(e) }); }
    finally { if (this.isCurrent() && gen === this.generation) this.publish({ busy: false }); }
  }
  private async mutate(operation: Operation, path: string, method: 'POST' | 'PATCH' | 'DELETE', body?: unknown) {
    if (!this.isCurrent() || this.view.busy || !hasCapability('memory.write') || !hasCapability('memory.read')) return false;
    this.publish({ busy: true, note: undefined }); let success = false; const gen = ++this.generation;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法安全保存操作，请使用支持 Web Locks 的浏览器。');
      await navigator.locks.request(this.journalKey, async () => {
        if (!this.isCurrent() || gen !== this.generation) return;
        if (this.pending()) { this.publish({ pending: true, notice: '先核对上一次操作，不重复提交。' }); return; }
        const p: Pending = { operation, key: crypto.randomUUID() };
        this.storage.setItem(this.journalKey, JSON.stringify(p));
        if (JSON.stringify(this.pending()) !== JSON.stringify(p)) throw Error('无法保存操作记录，尚未发送。');
        this.publish({ pending: true });
        try {
          parseMemory(await this.request(path, { method, body, idempotencyKey: p.key }));
          this.clearPending(p); success = true;
        } catch (e) {
          const error = e as { status?: number; code?: string };
          // Only a structured, explicit rejection is terminal. No retry for
          // timeouts, malformed success payloads or uncertain server failures.
          if (this.isCurrent() && [400, 401, 403, 404, 409, 422].includes(error.status ?? 0) && error.code && error.code !== 'INVALID_ENVELOPE') {
            this.clearPending(p);
            if (error.status === 409) {
              const items = await this.list();
              if (this.isCurrent() && gen === this.generation) this.publish({ items });
            }
          }
          throw e;
        }
        const items = await this.list();
        if (this.isCurrent()) this.publish({ items, notice: '操作已保存，已重新读取当前记忆。' });
      });
    } catch (e) {
      if (this.isCurrent() && gen === this.generation) this.publish({ notice: this.view.pending ? '操作结果待核对；不会重复提交。' : String(e) });
    } finally { if (this.isCurrent() && gen === this.generation) this.publish({ busy: false }); }
    return success;
  }
  save(content: string, purpose: string) {
    if (!content.trim() || content.length > 5000 || !purpose.trim() || purpose.length > 200) return Promise.resolve(false);
    return this.mutate('save', '', 'POST', { content: content.trim(), purpose: purpose.trim(), persist: true, retain_until_withdrawn: true });
  }
  correct(item: Memory, content: string) {
    if (!uuid.test(item.id) || !content.trim() || content.length > 5000) return Promise.resolve(false);
    return this.mutate('correct', `/${item.id}`, 'PATCH', { content: content.trim(), expected_version: item.version });
  }
  withdraw(item: Memory) { return uuid.test(item.id) ? this.mutate('withdraw', `/${item.id}?expected_version=${item.version}`, 'DELETE') : Promise.resolve(false); }
  importNote(markdown: string) {
    if (!markdown || new TextEncoder().encode(markdown).length > 32768) return Promise.resolve(false);
    return this.mutate('correct', '/notes/import', 'POST', { markdown });
  }
}
