import { apiRequest, ApiError } from '../../lib/api/client';
import { beginScopedRequest, getScopeVersion, getSession, sessionIsExpired, subscribe } from '../../lib/auth/session';
import type { Session } from '../../lib/auth/session';
import { hasCapability, subscribeCapabilities } from '../../lib/auth/capabilities';
import { parseProgress } from './workProgress';
import type { WorkProgress } from './workProgress';

export interface Storage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }
export interface Work {
  work_id: string; request_id: string; title: string; text: string; created_at: string;
  execution_status: string; run_id: string | null; answer?: string; answerUnavailable: boolean;
  citations: { kind: string; objectId: string; version: string; label: string }[];
  progress: WorkProgress;
  task_state?: string;
  root_work_id?: string;
  previous_turn_id?: string;
  sequence?: number;
  message_id?: string;
  converted_turn_id?: string;
  inbox_sequence?: number;
  previous_message_id?: string | null;
  inbox_intake?: 'received' | 'cancelled' | 'converted';
  authority_expires_at?: string;
}
interface View { items: Work[]; busy: boolean; mutating: boolean; messages: Work[]; pending: boolean; stoppingId: string | null; intakeAvailable: boolean; notice: string; privacyEpoch: number; hasMore: boolean;
  selectedId: string | null; selectionReady: boolean; conversation: Work[]; conversationLoaded: boolean;
  followupsSupported: boolean; followupsAvailable: boolean;
  inboxSupported: boolean; inboxEnabled: boolean; inboxAvailable: boolean; unavailableReason: string | null }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const statusLabels: Record<string, string> = {
  queued: '等待处理', starting: '正在启动', created: '已排队', running: '正在处理',
  waiting_for_human: '等待你的处理', waiting_for_external: '等待外部结果', completed: '已答复',
  failed: '未完成', cancelled: '已停止', blocked: '暂时无法执行', needs_attention: '结果待核对',
  authority_unrecorded: '需要接续', dispatch_unverified: '结果待核对',
  not_dispatched: '补充已保存，尚未执行',
};
function row(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('工作记录格式异常。');
  return value as Record<string, unknown>;
}
function text(value: unknown, max: number): string {
  if (typeof value !== 'string' || [...value].length > max) throw Error('工作记录格式异常。');
  return value;
}
function id(value: unknown): string {
  if (typeof value !== 'string' || !uuid.test(value)) throw Error('工作记录标识异常。');
  return value;
}
export function parseWork(value: unknown): Work {
  const v = row(value), status = text(v.execution_status, 40);
  if (!Object.hasOwn(statusLabels, status) || typeof v.created_at !== 'string' || !Number.isFinite(Date.parse(v.created_at))) throw Error('工作状态异常。');
  const result: Work = { work_id: id(v.work_id), request_id: id(v.request_id), title: text(v.title, 200),
    text: text(v.text, Infinity), created_at: v.created_at, execution_status: status,
    run_id: v.run_id === null ? null : id(v.run_id), citations: [], answerUnavailable: false,
    progress: parseProgress(status, null, v.dispatch_code) };
  if (v.task_state !== undefined) result.task_state = text(v.task_state, 40);
  if (v.run_receipt === null) {
    if (result.run_id !== null || status === 'completed') throw Error('工作结果缺少来源。');
    return result;
  }
  const receipt = row(v.run_receipt);
  if (result.run_id === null || receipt.run_id !== result.run_id || receipt.state !== status) throw Error('工作结果不匹配。');
  result.progress = parseProgress(status, receipt, v.dispatch_code);
  if (status === 'completed') {
    if (!['available', 'unavailable', 'not_recorded'].includes(String(receipt.answer_status))) throw Error('答复状态异常。');
    result.answerUnavailable = receipt.answer_status !== 'available';
    if (receipt.answer_status === 'available') {
      result.answer = text(receipt.answer, 100000);
      if (!Array.isArray(receipt.citations) || receipt.citations.length > 1000) throw Error('答复来源异常。');
      result.citations = receipt.citations.map(value => {
        const c = row(value);
        return { kind: text(c.kind, 100), objectId: text(c.object_id, 200),
          version: text(c.version_ref, 200), label: text(c.label, 1000) };
      });
    }
  }
  return result;
}

export function parseTurn(value: unknown, rootId: string): Work {
  const v = row(value);
  if (id(v.root_work_id) !== id(rootId) || v.intake_state !== 'received' ||
      !Number.isSafeInteger(v.sequence) || (v.sequence as number) < 2) throw Error('跟进记录不属于当前工作。');
  const turnId = id(v.turn_id), parent = id(v.previous_turn_id);
  if (turnId === rootId || turnId === parent) throw Error('跟进记录链异常。');
  const legacy = v.execution_status === 'not_dispatched';
  if (legacy && (v.run_id != null || v.run_receipt != null)) throw Error('未执行补充含不一致回执。');
  return { ...parseWork({ ...v, work_id: turnId, title: '补充',
    ...(legacy ? { run_id: null, run_receipt: null } : {}) }), root_work_id: rootId,
    previous_turn_id: parent, sequence: v.sequence as number };
}

export function parseConversation(root: Work, value: unknown): Work[] {
  if (!Array.isArray(value)) throw Error('跟进列表格式异常。');
  const result = [root], requests = new Set([root.request_id]), ids = new Set([root.work_id]);
  const runs = new Set(root.run_id ? [root.run_id] : []);
  for (const item of value) {
    const turn = parseTurn(item, root.work_id);
    if (turn.sequence !== result.length + 1 || turn.previous_turn_id !== result.at(-1)!.work_id ||
        ids.has(turn.work_id) || requests.has(turn.request_id) || (turn.run_id !== null && runs.has(turn.run_id))) throw Error('跟进记录链不一致。');
    ids.add(turn.work_id); requests.add(turn.request_id); if (turn.run_id) runs.add(turn.run_id);
    result.push(turn);
  }
  return result;
}

export function parseInbox(value: unknown, rootId: string): Work {
  const v = row(value), state = v.intake_state;
  if (id(v.root_work_id) !== id(rootId) || !['received', 'cancelled', 'converted'].includes(String(state)) ||
      !Number.isSafeInteger(v.sequence) || (v.sequence as number) < 1 ||
      typeof v.authority_expires_at !== 'string' || !Number.isFinite(Date.parse(v.authority_expires_at))) throw Error('补充收件记录异常。');
  const messageId = id(v.message_id), parent = id(v.parent_id);
  if (messageId === rootId || messageId === parent) throw Error('补充收件标识异常。');
  const converted = state === 'converted';
  if ((!converted && (v.execution_status !== 'not_dispatched' || v.run_id != null || v.run_receipt != null || v.turn_id != null)) ||
      (converted && v.execution_status === 'not_dispatched')) throw Error('补充执行状态不一致。');
  const work = parseWork({ ...v, work_id: messageId, title: '补充',
    ...(!converted ? { execution_status: state === 'cancelled' ? 'cancelled' : 'not_dispatched', run_id: null, run_receipt: null } : {}) });
  if (state === 'received' && Date.parse(v.authority_expires_at) <= Date.now())
    work.progress = { ...work.progress, activity: '补充已保存，但处理授权已到期。' };
  return { ...work, message_id: messageId, root_work_id: rootId, previous_turn_id: parent,
    previous_message_id: v.previous_message_id === null ? null : id(v.previous_message_id),
    inbox_sequence: v.sequence as number, inbox_intake: state as Work['inbox_intake'],
    authority_expires_at: v.authority_expires_at, ...(converted ? { converted_turn_id: id(v.turn_id) } : {}) };
}

export function parseSnapshot(value: unknown, rootId: string): { chain: Work[]; messages: Work[] } {
  const v = row(value), root = parseWork(v.root);
  if (root.work_id !== rootId || !Array.isArray(v.messages)) throw Error('对话快照不匹配。');
  const chain = parseConversation(root, v.turns), messages = v.messages.map(x => parseInbox(x, rootId));
  const ids = new Set(chain.map(x => x.work_id)), requests = new Set<string>(), conversions = new Set<string>();
  for (const [index, message] of messages.entries()) {
    if (ids.has(message.work_id) || requests.has(message.request_id) || message.request_id === root.request_id ||
        message.inbox_sequence !== index + 1 || message.previous_message_id !== (messages[index - 1]?.work_id ?? null) ||
        Date.parse(message.created_at) < Date.parse(root.created_at) || !chain.some(x => x.work_id === message.previous_turn_id)) throw Error('补充顺序或来源不一致。');
    ids.add(message.work_id); requests.add(message.request_id);
    const turn = chain.find(x => x.request_id === message.request_id);
    if (message.inbox_intake === 'converted') {
      if (!turn || turn.work_id !== message.converted_turn_id || conversions.has(turn.work_id) || turn.text !== message.text ||
          turn.run_id !== message.run_id || turn.execution_status !== message.execution_status || turn.answer !== message.answer ||
          turn.answerUnavailable !== message.answerUnavailable || JSON.stringify(turn.citations) !== JSON.stringify(message.citations) ||
          JSON.stringify(turn.progress) !== JSON.stringify(message.progress)) throw Error('补充转换回执不一致。');
      conversions.add(turn.work_id);
    } else if (turn) throw Error('补充转换记录缺失。');
  }
  return { chain, messages };
}

type PendingRequest = { request_id: string; root_work_id?: string; previous_turn_id?: string; kind?: 'inbox' };
type StopRequest = { work_id: string; root_work_id?: string; kind?: 'inbox' };

export class AssistantController {
  private view: View = { items: [], busy: false, mutating: false, messages: [], pending: false, stoppingId: null, intakeAvailable: false, notice: '正在读取你的工作…', privacyEpoch: 0, hasMore: false,
    selectedId: null, selectionReady: false, conversation: [], conversationLoaded: false, followupsSupported: false, followupsAvailable: false, inboxSupported: false, inboxEnabled: false, inboxAvailable: false, unavailableReason: null };
  private autoSelect = true;
  private selectionEpoch = 0;
  private listeners = new Set<() => void>();
  private active = false;
  private denied = false;
  private generation = 0;
  private scope = getScopeVersion();
  private unsub: (() => void)[] = [];
  private timer?: ReturnType<typeof setInterval>;
  private lastPollAt = 0;
  private lastListAt = 0;
  readonly journalKey: string;
  readonly stopJournalKey: string;
  constructor(readonly session: Session, private storage: Storage, private baseUrl = '', initialWorkId?: string | null) {
    this.journalKey = 'v4-assistant-request:' + JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
    this.stopJournalKey = this.journalKey + ':stop';
    if (initialWorkId === null || (typeof initialWorkId === 'string' && uuid.test(initialWorkId))) {
      this.autoSelect = false; this.view.selectedId = initialWorkId;
    }
  }
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getSnapshot = () => this.view;
  isCurrent = () => this.active && this.scope === getScopeVersion() && getSession() === this.session && !sessionIsExpired(this.session);
  private canRead = () => this.isCurrent() && !this.denied && hasCapability('agent.run') && hasCapability('tasks.read_self');
  private publish(next: Partial<View>) { this.view = { ...this.view, ...next }; this.listeners.forEach(fn => fn()); }
  private hide() {
    this.generation++;
    this.selectionEpoch++;
    this.publish({ items: [], busy: false, mutating: false, messages: [], inboxSupported: false, inboxEnabled: false, inboxAvailable: false, stoppingId: null, intakeAvailable: false, privacyEpoch: this.view.privacyEpoch + 1,
      conversation: [], conversationLoaded: false, followupsAvailable: false, followupsSupported: false, selectionReady: false,
      hasMore: false, notice: '身份或权限已改变，请重新进入助理。' });
  }
  start() {
    if (this.active) return;
    this.active = true;
    this.unsub = [subscribe(() => { if (!this.isCurrent()) this.hide(); }), subscribeCapabilities(() => {
      if (!this.canRead()) this.hide();
      else void this.refresh();
    })];
    this.lastPollAt = Date.now();
    this.timer = setInterval(() => {
      if (!this.isCurrent() || this.denied) { this.hide(); clearInterval(this.timer); return; }
      // Login capabilities can arrive after this page mounts. Keep the local
      // timer alive but make no request until this identity has read access.
      if (!this.canRead()) return;
      const progressing = this.view.pending || this.view.stoppingId !== null || [...this.view.items, ...this.view.conversation, ...this.view.messages].some(
        work => ['queued', 'starting', 'created', 'running', 'not_dispatched'].includes(work.execution_status));
      if (this.view.busy || Date.now() - this.lastPollAt < (progressing ? 1000 : 5000)) return;
      this.lastPollAt = Date.now();
      const currentWork = this.view.items.find(work => ['queued', 'starting', 'created', 'running'].includes(work.execution_status));
      if (this.view.selectedId && this.view.followupsSupported && !this.view.pending && !this.view.stoppingId && Date.now() - this.lastListAt < 5000) {
        void this.refreshSelected();
      } else if (currentWork && !this.view.pending && !this.view.stoppingId && Date.now() - this.lastListAt < 5000) {
        void this.refreshProgress(currentWork);
      } else void this.refresh();
    }, 1000);
    void this.refresh();
  }
  dispose() { this.active = false; this.generation++; clearInterval(this.timer); this.unsub.forEach(fn => fn()); this.unsub = []; this.publish({ busy: false }); }
  private async request(path: string, options: Parameters<typeof apiRequest>[1] = {}) {
    if (!this.canRead()) throw Error('当前身份无法访问这些工作。');
    const gen = this.generation, scoped = beginScopedRequest();
    try {
      const data = await apiRequest<unknown>('/api/v1/assistant/work' + path, {
        ...options, token: this.session.accessToken, baseUrl: this.baseUrl, signal: scoped.signal,
      });
      if (!scoped.isCurrent() || !this.canRead() || gen !== this.generation) throw Error('身份或权限已改变。');
      return data;
    } catch (error) {
      if (this.isCurrent() && error instanceof ApiError && [401, 403].includes(error.status)) { this.denied = true; this.hide(); }
      throw error;
    } finally { scoped.finish(); }
  }
  private pendingRequest(): PendingRequest | null {
    const raw = this.storage.getItem(this.journalKey);
    if (raw === null) return null;
    const value = row(JSON.parse(raw));
    const keys = Object.keys(value).sort().join();
    if (keys === 'request_id') return { request_id: id(value.request_id) };
    if (keys === 'previous_turn_id,request_id,root_work_id') return {
      request_id: id(value.request_id), root_work_id: id(value.root_work_id), previous_turn_id: id(value.previous_turn_id) };
    if (keys === 'kind,previous_turn_id,request_id,root_work_id' && value.kind === 'inbox') return {
      request_id: id(value.request_id), root_work_id: id(value.root_work_id), previous_turn_id: id(value.previous_turn_id), kind: 'inbox' };
    throw Error('原请求记录无法核对。');
  }
  private pendingId() { return this.pendingRequest()?.request_id ?? null; }
  private clearPending(requestId: string) {
    if (!this.isCurrent() || this.pendingId() !== requestId) throw Error('原请求记录已改变。');
    this.storage.removeItem(this.journalKey);
    this.publish({ pending: false });
  }
  private async reconcile(gen: number) {
    const record = this.pendingRequest(), pending = record?.request_id;
    if (!record || !pending) { this.publish({ pending: false }); return true; }
    this.publish({ pending: true });
    if (record.kind === 'inbox' && record.root_work_id) {
      try {
        const message = parseInbox(await this.request('/' + record.root_work_id + '/inbox/by-request/' + pending, { method: 'GET' }), record.root_work_id);
        if (gen !== this.generation) return false;
        if (message.request_id !== pending || message.previous_turn_id !== record.previous_turn_id) throw Error('原补充收件不匹配。');
        this.clearPending(pending); return true;
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404 && error.code.toLowerCase() === 'assistant_inbox_not_found')) throw error;
        this.publish({ notice: '补充发送结果尚未核实，保留原请求继续查询，不会重复发送。' }); return false;
      }
    }
    if (record.root_work_id) {
      try {
        const turn = parseTurn(await this.request('/' + record.root_work_id + '/turns/by-request/' + pending, { method: 'GET' }), record.root_work_id);
        if (gen !== this.generation) return false;
        if (turn.request_id !== pending || turn.previous_turn_id !== record.previous_turn_id) throw Error('原补充回执不匹配。');
        this.clearPending(pending); return true;
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404 && error.code.toLowerCase() === 'assistant_turn_not_found')) throw error;
        this.publish({ notice: '上次补充尚未核实，保留原请求继续查询，不会重复发送。' }); return false;
      }
    }
    const receipt = row(await this.request('/by-request/' + pending, { method: 'GET' }));
    if (gen !== this.generation) return false;
    if (receipt.request_id !== pending) throw Error('原请求回执不匹配。');
    if (receipt.status === 'received') {
      const work = parseWork(receipt.work);
      if (work.request_id !== pending) throw Error('原工作不匹配。');
      this.clearPending(pending);
      return true;
    }
    if (receipt.status !== 'unobserved' || receipt.work !== null) throw Error('原请求回执异常。');
    this.publish({ notice: '上次交代的工作尚未核实，正在保留原请求；请勿重复发送。' });
    return false;
  }
  private async list(before?: string): Promise<Work[]> {
    const result = await this.request(before ? '?before_work_id=' + before : '', { method: 'GET' });
    if (!Array.isArray(result) || result.length > 50) throw Error('工作列表格式异常。');
    const items = result.map(parseWork);
    if (new Set(items.map(x => x.work_id)).size !== items.length) throw Error('工作列表重复。');
    return items;
  }
  async selectWork(workId: string | null) {
    if (workId !== null) id(workId);
    this.autoSelect = false; this.selectionEpoch++;
    this.publish({ selectedId: workId, selectionReady: true, conversation: [], messages: [], conversationLoaded: workId === null });
    if (workId !== null) await this.refreshSelected();
  }
  private async loadConversation(knownRoot?: Work) {
    const selected = this.view.selectedId, selection = this.selectionEpoch, gen = this.generation;
    const previous = this.view.conversation;
    if (selected === null) { this.publish({ conversation: [], messages: [], conversationLoaded: true }); return; }
    let chain: Work[], messages: Work[] = [];
    if (this.view.inboxSupported) {
      ({ chain, messages } = parseSnapshot(await this.request('/' + selected + '/conversation', { method: 'GET' }), selected));
    } else {
      const root = knownRoot?.work_id === selected ? knownRoot : parseWork(await this.request('/' + selected, { method: 'GET' }));
      if (root.work_id !== selected) throw Error('所选工作回执不匹配。');
      chain = this.view.followupsSupported ? parseConversation(root, await this.request('/' + selected + '/turns', { method: 'GET' })) : [root];
    }
    const root = chain[0];
    if (gen !== this.generation || selection !== this.selectionEpoch || selected !== this.view.selectedId) return;
    if (previous[0]?.work_id === selected && (chain.length < previous.length || previous.some((old, index) => {
      const fresh = chain[index];
      return !fresh || old.work_id !== fresh.work_id || old.request_id !== fresh.request_id || old.text !== fresh.text ||
        old.previous_turn_id !== fresh.previous_turn_id || old.sequence !== fresh.sequence ||
        (old.run_id !== null && old.run_id !== fresh.run_id);
    }))) throw Error('原工作或跟进回执已改变，请核对后继续。');
    if (this.view.messages.some(old => {
      const fresh = messages.find(x => x.work_id === old.work_id);
      return !fresh || old.request_id !== fresh.request_id || old.text !== fresh.text || old.previous_turn_id !== fresh.previous_turn_id ||
        old.inbox_sequence !== fresh.inbox_sequence || old.previous_message_id !== fresh.previous_message_id ||
        old.authority_expires_at !== fresh.authority_expires_at ||
        (old.converted_turn_id !== undefined && fresh.converted_turn_id !== old.converted_turn_id) ||
        (old.inbox_intake === 'cancelled' && fresh.inbox_intake !== 'cancelled') ||
        (old.run_id !== null && old.run_id !== fresh.run_id);
    })) throw Error('已保存的补充记录发生变化，请核对后继续。');
    this.publish({ conversation: chain, messages, conversationLoaded: true,
      items: this.view.items.map(work => work.work_id === selected ? root : work) });
  }
  private async refreshSelected() {
    if (!this.canRead() || this.view.busy || this.view.selectedId === null) return;
    this.publish({ busy: true }); const gen = ++this.generation, selection = this.selectionEpoch;
    try { await this.loadConversation(); }
    catch (error) {
      if (this.canRead() && gen === this.generation && selection === this.selectionEpoch)
        this.publish({ conversation: [], messages: [], conversationLoaded: false, notice: String(error) });
    } finally {
      if (this.canRead() && gen === this.generation) {
        this.publish({ busy: false });
        if (selection !== this.selectionEpoch) void this.refreshSelected();
      }
    }
  }
  displayConversation(): Work[] {
    const converted = new Set(this.view.messages.map(x => x.converted_turn_id));
    const chain = this.view.conversation.filter(x => !converted.has(x.work_id));
    return [...chain, ...this.view.messages].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at));
  }
  private unavailableMessage() {
    return this.view.unavailableReason === 'trial_expired' ? '本次试用时段已结束，已保存的工作和答复仍可查看。' : '服务暂时无法接收消息，已保存的工作仍可查看。';
  }
  followupReason(): string | null {
    if (this.view.selectedId === null) return null;
    if (!this.view.conversationLoaded) return '正在读取这件事，请稍候。';
    if (this.view.inboxEnabled && !this.view.inboxAvailable) return this.unavailableMessage();
    if (!this.view.inboxEnabled && !this.view.followupsAvailable) return this.view.unavailableReason === 'trial_expired'
      ? this.unavailableMessage() : '当前服务暂未启用继续处理，已保存的工作仍可查看。';
    const root = this.view.conversation[0], last = this.view.conversation.at(-1);
    if (!root || !last || root.work_id !== this.view.selectedId) return '请先读取原工作。';
    if (!['accepted', 'assigned', 'in_progress'].includes(root.task_state ?? '')) return '这项工作当前不能继续接收补充。';
    if (this.view.inboxEnabled && this.view.inboxAvailable) return null;
    if (this.view.messages.some(x => x.inbox_intake === 'received')) return '已保存的补充尚未处理，当前服务暂未启用接续。';
    if (last.progress.uncertain) return '上一轮结果尚未核实，核对后才能继续。';
    if (!['completed', 'failed', 'cancelled', 'blocked'].includes(last.execution_status))
      return last.execution_status === 'not_dispatched' ? '这条历史补充尚未执行，需要先核对。' : '正在处理这件事。你可以先写下补充，完成后发送。';
    return null;
  }
  async sendMessage(value: string): Promise<boolean> {
    if (!this.view.selectionReady || this.view.stoppingId !== null) return false;
    return this.view.selectedId === null ? this.submit(value) : this.view.inboxEnabled ? this.supplement(value) : this.followup(value);
  }
  private stopRequest(): StopRequest | null {
    const raw = this.storage.getItem(this.stopJournalKey);
    if (raw === null) return null;
    const value = row(JSON.parse(raw));
    const keys = Object.keys(value).sort().join();
    if (keys === 'work_id') return { work_id: id(value.work_id) };
    if (keys === 'root_work_id,work_id') return { work_id: id(value.work_id), root_work_id: id(value.root_work_id) };
    if (keys === 'kind,root_work_id,work_id' && value.kind === 'inbox') return { work_id: id(value.work_id), root_work_id: id(value.root_work_id), kind: 'inbox' };
    throw Error('原停止请求无法核对。');
  }
  private stopId() { return this.stopRequest()?.work_id ?? null; }
  private workPath(record: StopRequest) {
    if (record.kind === 'inbox') return '/' + record.root_work_id + '/inbox/' + record.work_id;
    return record.root_work_id ? '/' + record.root_work_id + '/turns/' + record.work_id : '/' + record.work_id;
  }
  private readStopped(value: unknown, record: StopRequest) {
    const work = record.kind === 'inbox' ? parseInbox(value, record.root_work_id!) : record.root_work_id ? parseTurn(value, record.root_work_id) : parseWork(value);
    if (work.work_id !== record.work_id) throw Error('停止请求回执不匹配。');
    return work;
  }
  private clearStop(workId: string) {
    if (!this.isCurrent() || this.stopId() !== workId) throw Error('停止请求已改变。');
    this.storage.removeItem(this.stopJournalKey); this.publish({ stoppingId: null });
  }
  private stopMessage(work: Work): string {
    if (work.progress.uncertain) return '已收到停止结果，但部分执行结果尚未核实；原工作已保留，请勿重复发送。';
    if (work.execution_status === 'cancelled') return '这项工作已停止。此前已经完成的操作仍会保留。';
    if (['completed', 'failed', 'blocked'].includes(work.execution_status)) return '这项工作已经结束，原结果已保留，没有重新执行。';
    return '停止请求已记录，正在核对执行结果。';
  }
  private async reconcileStop(gen: number): Promise<string | undefined> {
    const record = this.stopRequest(), workId = record?.work_id ?? null;
    this.publish({ stoppingId: workId });
    if (record === null || workId === null) return undefined;
    const work = this.readStopped(await this.request(this.workPath(record), { method: 'GET' }), record);
    if (gen !== this.generation) return undefined;
    if (work.progress.stopRecorded || ['cancelled', 'completed', 'failed', 'blocked', 'needs_attention', 'dispatch_unverified'].includes(work.execution_status)) {
      this.clearStop(workId); return this.stopMessage(work);
    }
    return '停止请求的结果尚未核实，正在查询原工作；不会自动重复提交停止或重新执行。';
  }
  async refresh() {
    if (!this.canRead() || this.view.busy) return;
    this.lastListAt = Date.now();
    this.publish({ busy: true }); const gen = ++this.generation, selection = this.selectionEpoch;
    try {
      const status = row(await this.request('/intake-status', { method: 'GET' }));
      if (gen !== this.generation) return;
      if (typeof status.intake_available !== 'boolean') throw Error('入口状态异常。');
      const supported = status.followups_supported ?? false, available = status.followups_available ?? false;
      if (typeof supported !== 'boolean' || typeof available !== 'boolean' || available && !supported) throw Error('跟进入口状态异常。');
      const inboxSupported = status.inbox_supported ?? false, inboxEnabled = status.inbox_enabled ?? false, inboxAvailable = status.inbox_available ?? false;
      const unavailableReason = status.unavailable_reason ?? null;
      if (typeof inboxSupported !== 'boolean' || typeof inboxEnabled !== 'boolean' || typeof inboxAvailable !== 'boolean' ||
          inboxEnabled && !inboxSupported || inboxAvailable && !inboxEnabled ||
          (unavailableReason !== null && unavailableReason !== 'trial_expired' && unavailableReason !== 'unavailable')) throw Error('补充入口状态异常。');
      this.publish({ followupsSupported: supported, followupsAvailable: available, inboxSupported, inboxEnabled, inboxAvailable, unavailableReason });
      const resolved = await this.reconcile(gen);
      if (gen !== this.generation) return;
      const stopNotice = await this.reconcileStop(gen);
      if (gen !== this.generation) return;
      const targetCount = Math.max(50, this.view.items.length);
      let page = await this.list(); const items = [...page];
      if (gen !== this.generation) return;
      while (page.length === 50 && items.length < targetCount) {
        page = await this.list(items.at(-1)!.work_id); items.push(...page);
        if (gen !== this.generation) return;
        if (new Set(items.map(x => x.work_id)).size !== items.length) throw Error('工作列表发生变化，请刷新。');
      }
      if (this.canRead() && gen === this.generation) this.publish({ items, hasMore: page.length === 50,
        intakeAvailable: status.intake_available,
        ...(resolved ? { notice: stopNotice ?? (status.intake_available ? '工作与结果已更新。' : this.unavailableMessage()) } : {}) });
      if (this.autoSelect) { this.autoSelect = false; this.publish({ selectedId: items[0]?.work_id ?? null }); }
      this.publish({ selectionReady: true });
      try { await this.loadConversation(items.find(work => work.work_id === this.view.selectedId)); }
      catch (error) {
        if (this.canRead() && gen === this.generation && selection === this.selectionEpoch)
          this.publish({ conversation: [], messages: [], conversationLoaded: false, notice: String(error) });
      }
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ items: [], conversation: [], messages: [], conversationLoaded: false, intakeAvailable: false, notice: String(error) });
    } finally {
      if (this.canRead() && gen === this.generation) {
        this.publish({ busy: false });
        if (selection !== this.selectionEpoch) void this.refreshSelected();
      }
    }
  }
  private async refreshProgress(original: Work) {
    if (!this.canRead() || this.view.busy) return;
    this.publish({ busy: true }); const gen = ++this.generation;
    try {
      const work = parseWork(await this.request('/' + original.work_id, { method: 'GET' }));
      if (work.work_id !== original.work_id || work.request_id !== original.request_id || work.text !== original.text ||
          (original.run_id !== null && work.run_id !== original.run_id)) throw Error('工作进度回执不匹配。');
      if (this.canRead() && gen === this.generation) this.publish({ items: this.view.items.map(
        item => item.work_id === work.work_id ? work : item), conversation: this.view.conversation.map(
        item => item.work_id === work.work_id ? work : item) });
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ notice: String(error) });
    } finally { if (this.canRead() && gen === this.generation) this.publish({ busy: false }); }
  }
  async loadMore() {
    if (!this.canRead() || this.view.busy || !this.view.hasMore || !this.view.items.length) return;
    this.publish({ busy: true }); const gen = ++this.generation;
    try {
      const items = await this.list(this.view.items.at(-1)!.work_id);
      if (this.canRead() && gen === this.generation) {
        const combined = [...this.view.items, ...items];
        if (new Set(combined.map(x => x.work_id)).size !== combined.length) throw Error('往期工作发生变化，请刷新。');
        this.publish({ items: combined, hasMore: items.length === 50 });
      }
    } catch (error) { if (this.canRead() && gen === this.generation) this.publish({ notice: String(error) }); }
    finally { if (this.canRead() && gen === this.generation) this.publish({ busy: false }); }
  }
  async supplement(textValue: string): Promise<boolean> {
    const content = textValue.trim(), reason = this.followupReason();
    if (!content || !this.canRead() || !hasCapability('tasks.manage') || this.view.mutating || !this.view.selectedId || !this.view.inboxAvailable) return false;
    if (reason) { this.publish({ notice: reason }); return false; }
    if (new TextEncoder().encode(JSON.stringify(content)).length > 131072) {
      this.publish({ notice: '这条消息超出当前传输容量，请分段发送；原文已保留，尚未发送。' }); return false;
    }
    const root = this.view.selectedId, parent = this.view.conversation.at(-1)!, selection = this.selectionEpoch;
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation; let success = false, changed = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复发送，请使用支持 Web Locks 的浏览器。');
      await navigator.locks.request(this.journalKey, async () => {
        if (!this.canRead() || gen !== this.generation || selection !== this.selectionEpoch || !hasCapability('tasks.manage')) return;
        if (this.pendingId()) { this.publish({ pending: true, notice: '先核对上次发送的补充。' }); return; }
        const record: PendingRequest = { request_id: crypto.randomUUID(), root_work_id: root, previous_turn_id: parent.work_id, kind: 'inbox' };
        this.storage.setItem(this.journalKey, JSON.stringify(record));
        if (JSON.stringify(this.pendingRequest()) !== JSON.stringify(record)) throw Error('无法保存请求标识，尚未发送。');
        this.publish({ pending: true });
        try {
          const message = parseInbox(await this.request('/' + root + '/inbox', { method: 'POST',
            body: { text: content, request_id: record.request_id, parent_id: parent.work_id }, idempotencyKey: record.request_id }), root);
          if (message.request_id !== record.request_id || message.text !== content || message.previous_turn_id !== parent.work_id ||
              this.view.messages.some(x => x.work_id === message.work_id || x.request_id === message.request_id)) throw Error('补充收件回执不匹配。');
          this.clearPending(record.request_id); success = true;
          if (selection === this.selectionEpoch) this.publish({ messages: [...this.view.messages, message] });
          this.publish({ notice: '补充已保存，进展会显示在这条消息下面。' });
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError && error.code !== 'INVALID_ENVELOPE' &&
              ([400, 401, 403, 404, 409, 422].includes(error.status) || error.status === 503 && error.code.toLowerCase() === 'assistant_inbox_unavailable')) {
            this.clearPending(record.request_id); changed = error.status === 409;
            this.publish({ notice: changed ? '工作状态已更新，请查看最新进展后发送；补充原文已保留。' : error.userMessage }); return;
          }
          throw error;
        }
      });
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ notice: this.view.pending
        ? '补充发送结果待核对，保留原请求继续查询，不会重复发送。' : String(error) });
    } finally {
      if (this.canRead() && gen === this.generation) {
        this.publish({ busy: false, mutating: false });
        if (success || changed || selection !== this.selectionEpoch) void this.refreshSelected();
      }
    }
    return success;
  }
  async followup(textValue: string): Promise<boolean> {
    const content = textValue.trim(), reason = this.followupReason();
    if (!content || !this.canRead() || !hasCapability('tasks.manage') || this.view.mutating || !this.view.selectedId) return false;
    if (reason) { this.publish({ notice: reason }); return false; }
    if (new TextEncoder().encode(JSON.stringify(content)).length > 131072) {
      this.publish({ notice: '这条消息超出当前传输容量，请分段发送；原文已保留，尚未发送。' }); return false;
    }
    const root = this.view.selectedId, parent = this.view.conversation.at(-1)!;
    const chain = this.view.conversation, selection = this.selectionEpoch;
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation; let success = false, changed = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复发送，请使用支持 Web Locks 的浏览器。');
      await navigator.locks.request(this.journalKey, async () => {
        if (!this.canRead() || gen !== this.generation || selection !== this.selectionEpoch || !hasCapability('tasks.manage')) return;
        if (this.pendingId()) { this.publish({ pending: true, notice: '先核对上次交代的工作。' }); return; }
        const record = { request_id: crypto.randomUUID(), root_work_id: root, previous_turn_id: parent.work_id };
        this.storage.setItem(this.journalKey, JSON.stringify(record));
        if (JSON.stringify(this.pendingRequest()) !== JSON.stringify(record)) throw Error('无法保存请求标识，尚未发送。');
        this.publish({ pending: true });
        try {
          const turn = parseTurn(await this.request('/' + root + '/turns', { method: 'POST',
            body: { text: content, request_id: record.request_id, previous_turn_id: parent.work_id }, idempotencyKey: record.request_id }), root);
          if (turn.request_id !== record.request_id || turn.text !== content || turn.previous_turn_id !== parent.work_id ||
              turn.sequence !== chain.length + 1 || chain.some(item => item.work_id === turn.work_id || (turn.run_id && item.run_id === turn.run_id))) throw Error('补充回执不匹配。');
          this.clearPending(record.request_id); success = true;
          if (selection === this.selectionEpoch) this.publish({ conversation: [...chain, turn], conversationLoaded: true });
          this.publish({ notice: '补充已保存，正在继续这件事。' });
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError && error.code !== 'INVALID_ENVELOPE' &&
              ([400, 401, 403, 404, 409, 422].includes(error.status) ||
                error.status === 503 && error.code.toLowerCase() === 'assistant_followup_unavailable')) {
            this.clearPending(record.request_id);
            changed = error.status === 409;
            this.publish({ notice: changed ? '这件事的状态已更新。请查看最新进展再发送，补充原文已保留。' : error.userMessage });
            return;
          }
          throw error;
        }
      });
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ notice: this.view.pending
        ? '补充发送结果待核对，已保留原请求，不会重复发送。' : String(error) });
    } finally {
      if (this.canRead() && gen === this.generation) {
        this.publish({ busy: false, mutating: false });
        if (changed || selection !== this.selectionEpoch) void this.refreshSelected();
      }
    }
    return success;
  }
  async submit(textValue: string): Promise<boolean> {
    const content = textValue.trim();
    if (!content || !this.canRead() || !hasCapability('tasks.manage') || this.view.mutating || !this.view.intakeAvailable) return false;
    if (new TextEncoder().encode(JSON.stringify(content)).length > 131072) {
      this.publish({ notice: '这条消息超出当前传输容量，请分段发送；原文已保留，尚未发送。' }); return false;
    }
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation, selection = this.selectionEpoch; let success = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复发送，请使用支持 Web Locks 的浏览器。');
      await navigator.locks.request(this.journalKey, async () => {
        if (!this.canRead() || gen !== this.generation || !hasCapability('tasks.manage')) return;
        if (this.pendingId()) { this.publish({ pending: true, notice: '先核对上次交代的工作。' }); return; }
        const requestId = crypto.randomUUID();
        this.storage.setItem(this.journalKey, JSON.stringify({ request_id: requestId }));
        if (this.pendingId() !== requestId) throw Error('无法保存请求标识，尚未发送。');
        this.publish({ pending: true });
        try {
          const work = parseWork(await this.request('', { method: 'POST', body: { text: content, request_id: requestId }, idempotencyKey: requestId }));
          if (work.request_id !== requestId || work.text !== content) throw Error('工作回执不匹配。');
          this.clearPending(requestId); success = true;
          this.publish({ items: [work, ...this.view.items.filter(x => x.work_id !== work.work_id)].slice(0, Math.max(50, this.view.items.length)), notice: '工作已保存，进度与结果会显示在这里。' });
          if (selection === this.selectionEpoch) {
            this.autoSelect = false;
            this.publish({ selectedId: work.work_id, selectionReady: true, conversation: [work], messages: [], conversationLoaded: true });
          }
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError && error.code !== 'INVALID_ENVELOPE' &&
              ([400, 401, 403, 404, 409, 422].includes(error.status) ||
                error.status === 503 && ['ASSISTANT_INTAKE_UNAVAILABLE', 'assistant_intake_unavailable'].includes(error.code))) this.clearPending(requestId);
          throw error;
        }
      });
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ notice: this.view.pending ? '发送结果待核对，已保留原请求，不会重复发送。' : String(error) });
    } finally {
      if (this.canRead() && gen === this.generation) {
        this.publish({ busy: false, mutating: false });
        if (selection !== this.selectionEpoch) void this.refreshSelected();
      }
    }
    return success;
  }
  async stop(workId: string): Promise<boolean> {
    if (!this.canRead() || !hasCapability('tasks.manage') || this.view.mutating) return false;
    const original = [...this.view.messages, ...this.view.conversation, ...this.view.items].find(w => w.work_id === workId);
    if (!original || !(['queued', 'starting', 'created', 'running', 'waiting_for_human', 'waiting_for_external'].includes(original.execution_status) ||
        original.execution_status === 'not_dispatched' && original.message_id && original.inbox_intake === 'received')) return false;
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation; let success = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复操作，请使用支持 Web Locks 的浏览器。');
      await navigator.locks.request(this.journalKey, async () => {
        if (!this.canRead() || gen !== this.generation || !hasCapability('tasks.manage')) return;
        const pending = this.stopId();
        if (pending !== null) { this.publish({ stoppingId: pending, notice: '先核对上次停止请求的结果。' }); return; }
        const record: StopRequest = { work_id: workId, ...(original.root_work_id ? { root_work_id: original.root_work_id } : {}), ...(original.message_id ? { kind: 'inbox' as const } : {}) };
        this.storage.setItem(this.stopJournalKey, JSON.stringify(record));
        if (JSON.stringify(this.stopRequest()) !== JSON.stringify(record)) throw Error('无法保存停止请求标识，尚未发送。');
        this.publish({ stoppingId: workId });
        try {
          const response = row(await this.request(this.workPath(record) + '/stop', { method: 'POST' }));
          const work = this.readStopped(record.kind === 'inbox' ? response.message : response.work, record);
          if (work.work_id !== workId || work.request_id !== original.request_id || work.text !== original.text ||
              work.previous_turn_id !== original.previous_turn_id || work.sequence !== original.sequence ||
              (original.run_id !== null && work.run_id !== original.run_id) ||
              !['stopped', 'stopped_before_conversion', 'already_finished', 'outcome_unresolved', 'stop_requested'].includes(String(response.stop_result)) ||
              (['stopped', 'stopped_before_conversion'].includes(String(response.stop_result)) && work.execution_status !== 'cancelled') ||
              (response.stop_result === 'already_finished' && !['completed', 'failed', 'blocked'].includes(work.execution_status)) ||
              (response.stop_result === 'outcome_unresolved' && !work.progress.uncertain) ||
              (response.stop_result === 'stop_requested' && !work.progress.stopRecorded)) throw Error('停止请求回执不匹配。');
          this.clearStop(workId); success = true;
          this.publish({ items: this.view.items.map(w => w.work_id === workId ? work : w),
            conversation: this.view.conversation.map(w => w.work_id === workId ? work : w),
            messages: this.view.messages.map(w => w.work_id === workId ? work : w), notice: this.stopMessage(work) });
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError &&
              [404, 409].includes(error.status) && ['assistant_stop_starting', 'assistant_stop_authority_unrecorded',
                'assistant_stop_dispatch_conflict', 'assistant_work_not_found', 'assistant_inbox_not_found'].includes(error.code.toLowerCase())) {
            this.clearStop(workId);
            this.publish({ notice: '本次尚未提交停止。工作可能仍在启动或状态已变化，请刷新核对后再操作。' });
            return;
          }
          throw error;
        }
      });
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ notice: this.view.stoppingId
        ? '停止结果待核对，已保留原请求。刷新只查询原工作，不会重复执行。' : String(error) });
    } finally { if (this.canRead() && gen === this.generation) this.publish({ busy: false, mutating: false }); }
    if (success && this.canRead() && this.view.selectedId) void this.refreshSelected();
    return success;
  }
}
