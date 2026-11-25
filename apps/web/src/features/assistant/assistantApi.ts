import {browserReportsOffline,offlineBeforeSendNotice} from './assistantNetwork';
import {parseEvidenceCitation,type EvidenceCitation} from './evidenceCitation';
import {receiptPresence,type Presence} from './companionState';
import {consumePublicWatch,type WatchFrame} from './publicResultWire';
import {exceedsNewIntakeTextBudget,isNewIntakeValidationRejection} from './newIntakeDraftFeedback';
import {LEGACY_INPUT_POLICY,parseInputPolicy,parseImageInputs,parseImageReferences,sameImageInputs,type InputPolicy,type ImageInput,type ImageReference} from './assistantInput';
import { apiRequest, ApiError, createCategorizedApiError, parseEnvelope } from '../../lib/api/client';
import {productArchiveProof} from './productBarcodePage';
import {canOpenPurchaseQuantityComparison} from './purchaseQuantityComparison';
import {mayReadStoredSalesCoverage} from './localSalesCoverage';
import {canReadSavedStockStep} from './savedStockObservation';
import {canReadSavedStockLoss} from './savedStockLossDocuments';
import {canReadSavedWarehouseDirectory} from './savedWarehouseDirectory';
import {canReadSavedLegacyProduct} from './savedLegacyProduct';
import {canReadPurchaseOrderDetailPage} from './purchaseOrderDetailPage';
import { beginScopedRequest, getScopeVersion, getSession, sessionIsExpired, subscribe } from '../../lib/auth/session';
import type { Session } from '../../lib/auth/session';
import { getCapabilityVersion, hasCapability, subscribeCapabilities } from '../../lib/auth/capabilities';
import { parseProgress } from './workProgress';
import type { WorkProgress } from './workProgress';

import {snapshotMatchesDraftSource,validSourceRowRef,wireSourceRowRef,type DraftSourceRow} from './sourceRowReference';
import {parseResultSnapshot} from './publicResultSnapshot';
import {canDraftReceiptDetail} from './businessChoice';
import {isWorkReadDenial} from '../../lib/auth/businessPermissions';
import {AssistantRouteInterrupted,linkAssistantRead,type AssistantRouteVisibility} from './assistantRouteVisibility';
type DraftSubmission = {onJournaled:()=>void;onNotAccepted:()=>void;onConfirmedFollowup?:()=>void;sourceRow?:DraftSourceRow;
 closedSourceCheck?:{stillCurrent:()=>boolean;onChanged:()=>void}};
function notifyConfirmedFollowup(draft?:DraftSubmission) {
  try{draft?.onConfirmedFollowup?.();}catch{/* Local draft failure cannot change an accepted receipt. */}
}
export interface Storage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }
export interface Work {
  work_id: string; request_id: string; title: string; text: string; created_at: string;
  execution_status: string; run_id: string | null; answer?: string; answerUnavailable: boolean;
  citations: EvidenceCitation[];
  progress: WorkProgress;
  images?: ImageReference[];
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
interface View { historyPresence:Record<string,Presence>; recoveredDelivery:{rootId:string}|null; inputPolicy:InputPolicy; items: Work[]; busy: boolean; mutating: boolean; messages: Work[]; pending: boolean; stoppingId: string | null; intakeAvailable: boolean; notice: string; privacyEpoch: number; hasMore: boolean;
  selectedId: string | null; selectionReady: boolean; conversation: Work[]; conversationLoaded: boolean; conversationReadBusy: boolean;
  draftCheckpointFor?: {workId:string;capabilityVersion:string};
  followupsSupported: boolean; followupsAvailable: boolean; sourceRowReferencesAvailable: boolean;
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
  const result: Work = { images:parseImageReferences(v.images), work_id: id(v.work_id), request_id: id(v.request_id), title: text(v.title, 200),
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
      result.citations = receipt.citations.map(parseEvidenceCitation);
    }
  }
  if (result.images?.some(image=>!image.readable)) { delete result.answer; result.answerUnavailable=true; result.citations=[]; }
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
          JSON.stringify(turn.progress) !== JSON.stringify(message.progress) || !sameImageInputs(turn.images,message.images)) throw Error('补充转换回执不一致。');
      conversions.add(turn.work_id);
    } else if (turn) throw Error('补充转换记录缺失。');
  }
  return { chain, messages };
}

type PendingRequest = { images?:ImageInput[]; request_id: string; root_work_id?: string; previous_turn_id?: string; kind?: 'inbox' };
type StopRequest = { work_id: string; root_work_id?: string; kind?: 'inbox' };
const readingConversation = '正在读取这件工作…';
const interruptedRead = (error:unknown) => error instanceof ApiError && error.status===0 &&
  (error.code==='NETWORK_ERROR'||error.code==='TIMEOUT');
class ConversationReadBusy extends Error {}
const ACTIVE_READ_INTERVAL_MS = 1000;
const MAX_EARLY_TICK_MS = 200;
const waitsForAnswer = (work:Work) => ['queued', 'starting', 'created', 'running'].includes(work.execution_status);

export class AssistantController {
  private view: View = { historyPresence:{}, recoveredDelivery:null, inputPolicy:LEGACY_INPUT_POLICY, items: [], busy: false, mutating: false, messages: [], pending: false, stoppingId: null, intakeAvailable: false, notice: '正在读取你的工作…', privacyEpoch: 0, hasMore: false,
    selectedId: null, selectionReady: false, conversation: [], conversationLoaded: false, conversationReadBusy: false, followupsSupported: false, followupsAvailable: false, sourceRowReferencesAvailable:false, inboxSupported: false, inboxEnabled: false, inboxAvailable: false, unavailableReason: null };
  // Only this page's uncertain submission. No draft text or callback is stored in the durable journal.
  private localDelivery?: {record:PendingRequest; content:string; sending:boolean; recovered?:()=>void};
  private autoSelect = true;
  private selectionEpoch = 0;
  private listeners = new Set<() => void>();
  private active = false;
  private denied = false;
  private generation = 0;
  private scope = getScopeVersion();
  private unsub: (() => void)[] = [];
  private timer?: ReturnType<typeof setInterval>;
  private activeReadCatchUp?: ReturnType<typeof setTimeout>;
  private lastSteadyPollAt = 0;
  private conversationRead?: AbortController;
  private lastPollAt = 0;
  private lastListAt = 0;
  private resumeRequested = false;
  private lastResumeAt: number | null = null;
  readonly journalKey: string;
  readonly stopJournalKey: string;
  constructor(readonly session: Session, private storage: Storage, private baseUrl = '', initialWorkId?: string | null,
    private readonly route?: AssistantRouteVisibility) {
    this.journalKey = 'v4-assistant-request:' + JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
    this.stopJournalKey = this.journalKey + ':stop';
    if (initialWorkId !== undefined) {
      this.autoSelect = false; this.view.selectedId = typeof initialWorkId === 'string' && uuid.test(initialWorkId) ? initialWorkId : null;
    }
  }
  subscribe = (fn: () => void) => { this.listeners.add(fn); return () => { this.listeners.delete(fn); }; };
  getSnapshot = () => this.view;
  isCurrent = () => this.active && this.scope === getScopeVersion() && getSession() === this.session && !sessionIsExpired(this.session);
  private canRead = () => this.isCurrent() && !this.denied && hasCapability('agent.run') && hasCapability('tasks.read_self');
  private publish(next: Partial<View>) { this.view = { ...this.view, ...next }; this.listeners.forEach(fn => fn()); }
  private hide() {
    this.cancelActiveReadCatchUp();
    this.generation++;
    this.selectionEpoch++;
    this.conversationRead?.abort();
    this.localDelivery=undefined;
    this.publish({ historyPresence:{}, recoveredDelivery:null, inputPolicy:LEGACY_INPUT_POLICY, items: [], busy: false, mutating: false, messages: [], inboxSupported: false, inboxEnabled: false, inboxAvailable: false, stoppingId: null, intakeAvailable: false, privacyEpoch: this.view.privacyEpoch + 1,
      conversation: [], conversationLoaded: false, conversationReadBusy: false, draftCheckpointFor: undefined, followupsAvailable: false, followupsSupported: false, selectionReady: false,
      hasMore: false, notice: '身份或权限已改变，请重新进入助理。' });
  }
  private routeActive = () => this.route?.signal().aborted !== true;
  private pageCanPoll = () => this.routeActive() &&
    (typeof document === 'undefined' || document.visibilityState !== 'hidden') &&
    globalThis.navigator?.onLine !== false;
  private cancelActiveReadCatchUp() {
    clearTimeout(this.activeReadCatchUp);
    this.activeReadCatchUp = undefined;
  }
  private markAutoRead(now = Date.now()) {
    this.cancelActiveReadCatchUp();
    this.lastPollAt = now;
    this.lastSteadyPollAt = performance.now();
  }
  private expectsSelectedAnswer() {
    const v = this.view;
    return v.selectedId !== null && v.conversationLoaded && v.conversation[0]?.work_id === v.selectedId &&
      !v.pending && !v.mutating && v.stoppingId === null &&
      [...v.conversation, ...v.messages].some(waitsForAnswer);
  }
  private pauseAutomaticRead = () => {
    this.cancelActiveReadCatchUp();
    this.resumeRequested = false;
    this.lastResumeAt = null;
  };
  private queueResumeRead = () => {
    if (!this.canRead() || !this.pageCanPoll() ||
      (this.lastResumeAt !== null && Date.now() - this.lastResumeAt < 1000)) return;
    this.resumeRequested = true;
    this.readOnResume();
  };
  private readOnResume(): boolean {
    if (!this.resumeRequested || this.view.busy || !this.canRead() || !this.pageCanPoll()) return false;
    this.resumeRequested = false;
    this.lastResumeAt = Date.now();
    this.markAutoRead(this.lastResumeAt);
    // Refresh calls only read endpoints, including any existing exact-ID delivery journal.
    void this.refresh();
    return true;
  }
  start() {
    if (this.active) return;
    this.active = true;
    this.unsub = [subscribe(() => { if (!this.isCurrent()) this.hide(); }), subscribeCapabilities(() => {
      if (!this.canRead()) this.hide();
      else if (this.pageCanPoll()) void this.refresh();
    })];
    if (this.route) this.unsub.push(this.route.subscribe(() =>
      this.routeActive() ? this.queueResumeRead() : this.pauseAutomaticRead()));
    if (typeof document !== 'undefined') {
      const page = document;
      const visibility = () => page.visibilityState === 'hidden' ? this.pauseAutomaticRead() : this.queueResumeRead();
      page.addEventListener('visibilitychange', visibility);
      this.unsub.push(() => page.removeEventListener('visibilitychange', visibility));
    }
    if (typeof window !== 'undefined') {
      const browser = window;
      // Restoring a frozen page via back/forward can skip a fresh visibility event.
      const restored = (event: PageTransitionEvent) => { if (event.persisted) this.queueResumeRead(); };
      browser.addEventListener('pageshow', restored);
      browser.addEventListener('online', this.queueResumeRead);
      browser.addEventListener('offline', this.pauseAutomaticRead);
      this.unsub.push(() => {
        browser.removeEventListener('pageshow', restored);
        browser.removeEventListener('online', this.queueResumeRead);
        browser.removeEventListener('offline', this.pauseAutomaticRead);
      });
    }
    this.markAutoRead();
    const poll = (catchUp = false) => {
      if (!this.isCurrent() || this.denied) { this.hide(); clearInterval(this.timer); return; }
      // Login capabilities can arrive after this page mounts. Keep the local
      // timer alive but make no request until this identity has read access.
      if (!this.canRead() || !this.pageCanPoll()) return;
      // If a read or send was already in flight, wait rather than interrupting it.
      if (this.readOnResume()) return;
      const awaitingSelected = this.expectsSelectedAnswer();
      if ((catchUp && !awaitingSelected) || this.view.busy) return;
      const progressing = this.view.pending || this.view.stoppingId !== null || [...this.view.items, ...this.view.conversation, ...this.view.messages].some(
        work => waitsForAnswer(work) || work.execution_status === 'not_dispatched');
      const remaining = awaitingSelected
        ? ACTIVE_READ_INTERVAL_MS - (performance.now() - this.lastSteadyPollAt)
        : (progressing ? ACTIVE_READ_INTERVAL_MS : 5000) - (Date.now() - this.lastPollAt);
      if (remaining > 0) {
        // setInterval can arrive just short of a full second after an actual
        // read. Preserve the floor, but don't lose the whole next tick.
        if (!catchUp && awaitingSelected && remaining <= MAX_EARLY_TICK_MS && this.activeReadCatchUp === undefined) {
          const selection = this.selectionEpoch;
          this.activeReadCatchUp = setTimeout(() => {
            this.activeReadCatchUp = undefined;
            if (selection === this.selectionEpoch) poll(true);
          }, Math.ceil(remaining));
        }
        return;
      }
      this.markAutoRead();
      const currentWork = this.view.items.find(waitsForAnswer);
      if (this.view.selectedId && this.view.followupsSupported && !this.view.pending && !this.view.stoppingId && Date.now() - this.lastListAt < 5000) {
        void this.refreshSelected();
      } else if (currentWork && !this.view.pending && !this.view.stoppingId && Date.now() - this.lastListAt < 5000) {
        void this.refreshProgress(currentWork);
      } else void this.refresh();
    };
    this.timer = setInterval(poll, ACTIVE_READ_INTERVAL_MS);
    if (this.pageCanPoll()) void this.refresh();
  }
  dispose() { this.localDelivery=undefined; this.active = false; this.generation++; this.conversationRead?.abort(); this.pauseAutomaticRead(); clearInterval(this.timer); this.unsub.forEach(fn => fn()); this.unsub = []; this.publish({ busy: false }); }
  private async request(path: string, options: Parameters<typeof apiRequest>[1] = {}, independentRead = false) {
    if (!this.canRead()) throw Error('当前身份无法访问这些工作。');
    const permit = (options.method ?? 'GET') === 'GET' ? this.route?.signal() : undefined;
    if (permit?.aborted) throw new AssistantRouteInterrupted();
    const linked = linkAssistantRead(permit, options.signal ?? undefined);
    const gen = this.generation, capabilityVersion=getCapabilityVersion(), scoped = beginScopedRequest(linked.signal);
    try {
      const data = await apiRequest<unknown>('/api/v1/assistant/work' + path, {
        ...options, token: this.session.accessToken, baseUrl: this.baseUrl, signal: scoped.signal,
      });
      if (permit?.aborted) throw new AssistantRouteInterrupted();
      if (!scoped.isCurrent() || !this.canRead() || (!independentRead && gen !== this.generation) || capabilityVersion!==getCapabilityVersion()) throw Error('身份或权限已改变。');
      return data;
    } catch (error) {
      if (permit?.aborted) throw new AssistantRouteInterrupted();
      // The old selection's aborted GET can no longer update this view, including
      // deciding its read permissions. The next selection performs its own fresh check.
      if (!scoped.signal.aborted && this.isCurrent() && error instanceof ApiError) {
        if (isWorkReadDenial(error.status, options.method)) {
          this.denied = true;
          this.hide();
        } else if (error.status === 403 && this.canRead() && gen === this.generation &&
                   capabilityVersion === getCapabilityVersion()) {
          // An action refusal isn't proof of revoked read access. Recheck only the
          // read authority, without resubmitting the action or showing stale facts
          // when the recheck itself cannot reach the service.
          try { await this.request('/intake-status', { method: 'GET', signal: options.signal }); }
          catch (verificationError) {
            if (!(verificationError instanceof AssistantRouteInterrupted) &&
                this.canRead() && gen === this.generation && capabilityVersion === getCapabilityVersion())
              this.publish({ historyPresence: {}, items: [], conversation: [], messages: [], conversationLoaded: false });
          }
        }
      }
      throw error;
    } finally { scoped.finish(); linked.finish(); }
  }
  async readRunResults(root:string,run:string,signal:AbortSignal) {
    const epoch=this.selectionEpoch;
    const matches=()=>!signal.aborted && epoch===this.selectionEpoch && root===this.view.selectedId &&
      this.view.conversationLoaded && this.view.conversation.some(w=>w.run_id===run);
    if(!matches())throw Error('选择已改变。');
    // Routine list/progress refresh can advance generation while this independent
    // GET is in flight. Session, capability version and selection epoch still gate it.
    const order=canReadPurchaseOrderDetailPage(hasCapability)&&
      this.view.conversation.some(work=>work.run_id===run&&work.execution_status==='completed');
    const data=await this.request('/'+id(root)+'/public-results?run_id='+id(run)+'&operational_receipt_pages=1&operational_receipt_live=1&operational_receipt_schema=2&operational_purchase_return_detail=1&product_barcode_pages=1'+(order?'&purchase_order_detail_schema=2':''),{method:'GET',signal},true);
    if(!matches())throw Error('选择已改变。');
    return data;
  }
  private async readProductArchive(root:string,run:string,origin:string,relativePath:string,query:URLSearchParams,
    signal:AbortSignal,includeArchiveProof=false){
    const epoch=this.selectionEpoch;
    const matches=()=>!signal.aborted&&epoch===this.selectionEpoch&&root===this.view.selectedId&&this.canRead()&&
      this.view.conversationLoaded&&this.view.conversation.some(work=>work.run_id===run&&['completed','failed'].includes(work.execution_status));
    if(!matches()||origin==='.'||origin==='..')throw Error('来源或选择已改变。');
    query.set('run_id',id(run));
    const data=await this.request('/'+id(root)+'/product-barcode-pages/'+encodeURIComponent(origin)+relativePath+'?'+query,{
      method:'GET',cache:'no-store',signal,
      ...(includeArchiveProof?{readResponse: async (response:Response) => {
        const envelope=parseEnvelope(await response.json());
        if(!envelope.success)throw createCategorizedApiError(response.status,envelope.code,envelope.user_message,envelope.hint,envelope.error);
        return {data:envelope.data,archiveProof:productArchiveProof(response.headers.get('X-Product-Archive-Proof'))};
      }}:{}),
    },true);
    if(!matches())throw Error('选择已改变。');
    return data;
  }
  async readProductBarcodeRemainder(root:string,run:string,origin:string,after:string|null,signal:AbortSignal) {
    const query=new URLSearchParams();
    if(after!==null)query.set('after',after);
    return await this.readProductArchive(root,run,origin,'/remaining',query,signal,true) as {data:unknown;archiveProof:string|null};
  }
  async checkProductBarcodeArchive(root:string,run:string,origin:string,anchorId:string,signal:AbortSignal){
    return this.readProductArchive(root,run,origin,'/remaining/check',new URLSearchParams({anchor_block_id:anchorId}),signal);
  }
  async readPurchaseQuantityComparison(root:string,run:string,selection:{orderStep:string;receiptStep:string}|null,signal:AbortSignal){
    const epoch=this.selectionEpoch;
    const matches=()=>!signal.aborted&&epoch===this.selectionEpoch&&root===this.view.selectedId&&this.canRead()&&
      canOpenPurchaseQuantityComparison(hasCapability)&&this.view.conversationLoaded&&
      this.view.conversation.some(work=>work.run_id===run&&['completed','failed'].includes(work.execution_status));
    if(!matches())throw Error('当前事项或采购查阅权限已变化。');
    const query=new URLSearchParams({run_id:id(run)});
    if(selection!==null){
      if(!selection.orderStep||!selection.receiptStep||selection.orderStep===selection.receiptStep)throw Error('请各选一张已保存的单据。');
      query.set('order_step_id',selection.orderStep);query.set('receipt_step_id',selection.receiptStep);
    }
    const result=await this.request('/'+id(root)+'/purchase-quantity-comparison?'+query,{method:'GET',cache:'no-store',signal},true);
    if(!matches())throw Error('当前事项或采购查阅权限已变化。');
    return result;
  }
  async readSavedStockObservation(root:string,run:string,stepId:string,signal:AbortSignal){
    const epoch=this.selectionEpoch;
    const selected=()=>{
      const work=this.view.conversation[0];
      if(signal.aborted||browserReportsOffline()||epoch!==this.selectionEpoch||root!==this.view.selectedId||
         !this.canRead()||!this.view.conversationLoaded||work?.work_id!==root||work.run_id!==run||
         work.execution_status!=='completed')return undefined;
      return work.progress.savedStockObservationSteps?.find(step=>step.id===stepId&&canReadSavedStockStep(step,hasCapability));
    };
    const before=selected();
    if(!before)throw Error('当前事项或库存查看权限已变化。');
    const query=new URLSearchParams({run_id:id(run),step_id:id(stepId)});
    const value=await this.request('/'+id(root)+'/saved-stock-observations?'+query,{method:'GET',cache:'no-store',signal},true);
    const after=selected();
    if(!after||after.version!==before.version||after.sequence!==before.sequence||after.outputDigest!==before.outputDigest)throw Error('当前事项或库存查看权限已变化。');
    return value;
  }
  async readSavedStockLossPage(root:string,run:string,stepId:string,signal:AbortSignal){
    const epoch=this.selectionEpoch;
    const selected=()=>{
      const work=this.view.conversation[0];
      if(signal.aborted||browserReportsOffline()||epoch!==this.selectionEpoch||root!==this.view.selectedId||
         !this.canRead()||!this.view.conversationLoaded||work?.work_id!==root||work.run_id!==run||
         work.execution_status!=='completed'||!canReadSavedStockLoss(hasCapability))return undefined;
      return work.progress.savedStockLossSteps?.find(step=>step.id===stepId);
    };
    const before=selected();
    if(!before)throw Error('当前事项或报损单查看权限已变化。');
    const query=new URLSearchParams({run_id:id(run),step_id:id(stepId)});
    const value=await this.request('/'+id(root)+'/saved-stock-loss-documents?'+query,{method:'GET',cache:'no-store',signal},true);
    const after=selected();
    if(!after||after.sequence!==before.sequence||after.outputDigest!==before.outputDigest)throw Error('当前事项或报损单查看权限已变化。');
    return value;
  }
  async readSavedWarehouseDirectoryPage(root:string,run:string,stepId:string,signal:AbortSignal){
    const epoch=this.selectionEpoch;
    const selected=()=>{
      const work=this.view.conversation[0];
      if(signal.aborted||browserReportsOffline()||epoch!==this.selectionEpoch||root!==this.view.selectedId||
         !this.canRead()||!this.view.conversationLoaded||work?.work_id!==root||work.run_id!==run||
         work.execution_status!=='completed'||!canReadSavedWarehouseDirectory(hasCapability))return undefined;
      return work.progress.savedWarehouseDirectorySteps?.find(step=>step.id===stepId);
    };
    const before=selected();
    if(!before)throw Error('当前事项或仓库目录查看权限已变化。');
    const query=new URLSearchParams({run_id:id(run),step_id:id(stepId)});
    const value=await this.request('/'+id(root)+'/saved-warehouse-directory?'+query,{method:'GET',cache:'no-store',signal},true);
    const after=selected();
    if(!after||after.sequence!==before.sequence||after.outputDigest!==before.outputDigest)throw Error('当前事项或仓库目录查看权限已变化。');
    return value;
  }
  async readSavedLegacyProduct(root:string,run:string,stepId:string,signal:AbortSignal){
    const epoch=this.selectionEpoch;
    const selected=()=>{
      const work=this.view.conversation[0];
      if(signal.aborted||browserReportsOffline()||epoch!==this.selectionEpoch||root!==this.view.selectedId||
         !this.canRead()||!this.view.conversationLoaded||work?.work_id!==root||work.run_id!==run||
         work.execution_status!=='completed'||!canReadSavedLegacyProduct(hasCapability))return undefined;
      return work.progress.savedLegacyProductSteps?.find(step=>step.id===stepId);
    };
    const before=selected();
    if(!before)throw Error('当前事项或商品记录查看权限已变化。');
    const query=new URLSearchParams({run_id:id(run),step_id:id(stepId)});
    const value=await this.request('/'+id(root)+'/saved-legacy-product?'+query,{method:'GET',cache:'no-store',signal},true);
    const after=selected();
    if(!after||after.sequence!==before.sequence||after.version!==before.version||after.outputDigest!==before.outputDigest)
      throw Error('当前事项或商品记录查看权限已变化。');
    return value;
  }
  readStoredSalesCoverage(root:string,run:string,step:string|null,signal:AbortSignal){
    return this.readStoredSalesArchive('local-sales-archive-coverage',root,run,step,signal);
  }
  readStoredSalesRanking(root:string,run:string,step:string|null,signal:AbortSignal){
    return this.readStoredSalesArchive('local-sales-saved-ranking',root,run,step,signal);
  }
  private async readStoredSalesArchive(surface:'local-sales-archive-coverage'|'local-sales-saved-ranking',
      root:string,run:string,step:string|null,signal:AbortSignal){
    const epoch=this.selectionEpoch;
    const matches=()=>{
      const work=this.view.conversation[0];
      return !signal.aborted&&!browserReportsOffline()&&epoch===this.selectionEpoch&&root===this.view.selectedId&&
        this.canRead()&&this.view.conversationLoaded&&mayReadStoredSalesCoverage(hasCapability)&&
        work?.work_id===root&&work.run_id===run&&['completed','failed'].includes(work.execution_status)&&
        work.progress.hasStoredSalesCoverageCandidate===true;
    };
    if(!matches())throw Error('当前事项或销售查阅权限已变化。');
    const query=new URLSearchParams({run_id:id(run)});
    if(step!==null)query.set('step_id',id(step));
    const result=await this.request('/'+id(root)+'/'+surface+'?'+query,{method:'GET',cache:'no-store',signal},true);
    if(!matches())throw Error('当前事项或销售查阅权限已变化。');
    return result;
  }
  async readSavedReplyForExport(root:string,workId:string,run:string,signal:AbortSignal):Promise<Work> {
    const epoch=this.selectionEpoch,cap=getCapabilityVersion();
    const matches=()=>!signal.aborted&&epoch===this.selectionEpoch&&cap===getCapabilityVersion()&&
      this.canRead()&&root===this.view.selectedId&&this.view.conversationLoaded&&
      this.displayConversation().some(w=>w.work_id===workId&&w.run_id===run);
    if(!matches())throw Error('选择已改变。');
    let entries:Work[];
    if(this.view.inboxSupported){
      const loaded=parseSnapshot(await this.request('/'+id(root)+'/conversation',{method:'GET',signal},true),root);
      entries=[...loaded.chain,...loaded.messages];
    }else{
      const base=parseWork(await this.request('/'+id(root),{method:'GET',signal},true));
      if(base.work_id!==root)throw Error('答复来源已改变。');
      entries=workId===root?[base]:parseConversation(base,await this.request('/'+id(root)+'/turns',{method:'GET',signal},true));
    }
    const found=entries.filter(w=>w.work_id===workId&&w.run_id===run);
    if(!matches()||found.length!==1||found[0].answer===undefined||found[0].answerUnavailable||
       (entries.some(w=>w.images?.length)&&!hasCapability('media.read')))throw Error('答复目前不可读取。');
    return found[0];
  }
  async watchRunResults(root:string,run:string,signal:AbortSignal,receive:(f:WatchFrame)=>void) {
    const permit = this.route?.signal();
    if (permit?.aborted) throw new AssistantRouteInterrupted();
    const linked = linkAssistantRead(permit, signal);
    const epoch=this.selectionEpoch, cap=getCapabilityVersion(), scoped=beginScopedRequest(linked.signal);
    const live=()=>!scoped.signal.aborted && scoped.isCurrent() && this.canRead() && getCapabilityVersion()===cap &&
       this.selectionEpoch===epoch && this.view.selectedId===root && this.view.conversationLoaded && this.view.conversation.some(w=>w.run_id===run);
    try {
      if(!live())throw Error('选择已改变。');
      await apiRequest('/api/v1/assistant/work/'+id(root)+'/public-results/events?run_id='+id(run)+'&operational_receipt_pages=1&operational_receipt_live=1&operational_receipt_schema=2&operational_purchase_return_detail=1&product_barcode_pages=1',{
        method:'GET',token:this.session.accessToken,baseUrl:this.baseUrl,signal:scoped.signal,timeoutMs:22000,
        headers:{Accept:'text/event-stream'},readResponse:r=>consumePublicWatch(r,f=>{if(!live())throw Error('选择已改变。');receive(f);})
      });
    }catch(error){
      if (permit?.aborted) throw new AssistantRouteInterrupted();
      if(live() && error instanceof ApiError && [401,403].includes(error.status)){this.denied=true;this.hide();}
      throw error;
    }finally{scoped.finish();linked.finish();}
  }
  private pendingRequest(): PendingRequest | null {
    const raw = this.storage.getItem(this.journalKey); if (raw === null) return null;
    const value = row(JSON.parse(raw));
    const images = value.images === undefined ? undefined : parseImageInputs(value.images);
    const keys = Object.keys(value).filter(key=>key!=='images').sort().join();
    const imageFields = images === undefined ? {} : {images};
    if (keys === 'request_id') return {request_id:id(value.request_id),...imageFields};
    if (keys === 'previous_turn_id,request_id,root_work_id') return {request_id:id(value.request_id),root_work_id:id(value.root_work_id),previous_turn_id:id(value.previous_turn_id),...imageFields};
    if (keys === 'kind,previous_turn_id,request_id,root_work_id' && value.kind === 'inbox') return {request_id:id(value.request_id),root_work_id:id(value.root_work_id),previous_turn_id:id(value.previous_turn_id),kind:'inbox',...imageFields};
    throw Error('原请求记录无法核对。');
  }
  /**
   * Only an independent editor may checkpoint while another delivery is pending.
   * This does not relax sending or restore the original submitted text.
   */
  canCheckpointSelection(selectedId:string|null):boolean {
    if(!this.canRead()||!this.view.selectionReady||this.view.mutating||selectedId!==this.view.selectedId)return false;
    try {
      const local=this.localDelivery?.record, persisted=this.pendingRequest();
      if(!local&&!persisted)return !this.view.pending;
      // Both matter if another tab updated the journal; unknown root is the
      // independent new-work slot, never evidence of a different new draft.
      return (!local||(local.root_work_id??null)!==selectedId)&&
        (!persisted||(persisted.root_work_id??null)!==selectedId);
    }catch{return false;}
  }
  private pendingId() { return this.pendingRequest()?.request_id ?? null; }
  private clearPending(requestId: string) {
    if (!this.isCurrent()) throw Error('身份已改变。');
    const persisted=this.pendingId();
    // A sibling tab can finish its own authorized GET while this tab still has the same receipt in flight.
    if (persisted===requestId) this.storage.removeItem(this.journalKey);
    else if (this.localDelivery?.record.request_id!==requestId) throw Error('原请求记录已改变。');
    if(this.localDelivery?.record.request_id===requestId)this.localDelivery=undefined;
    this.publish({pending:this.pendingId()!==null});
  }
  dismissRecoveredDelivery(){this.publish({recoveredDelivery:null});}
  private finishRecovery(record:PendingRequest,work:Work){
    const local=this.localDelivery;
    if(local && (local.record.request_id!==record.request_id || local.content!==work.text))throw Error('原消息内容不匹配，保留草稿继续核对。');
    this.clearPending(record.request_id);
    // Never auto-navigate: the person may already be editing another conversation.
    this.publish({recoveredDelivery:{rootId:record.root_work_id??work.work_id}});
    local?.recovered?.();
    return !this.view.pending;
  }
  // A current-tab preview, not a Work/Run, receipt, or durable journal entry.
  localDeliveryPreview() {
    const local = this.localDelivery;
    if (!local || !this.canRead() || !this.view.selectionReady ||
        (local.record.root_work_id ?? null) !== this.view.selectedId ||
        [...this.view.conversation, ...this.view.messages].some(work => work.request_id === local.record.request_id)) return undefined;
    return { requestId: local.record.request_id, content: local.content, sending: local.sending,
      imageCount: hasCapability('media.read') ? local.record.images?.length ?? 0 : 0 };
  }
  private finishSending(requestId: string) {
    if (this.localDelivery?.record.request_id === requestId) this.localDelivery.sending = false;
  }
  private async reconcile(gen: number) {
    const record = this.localDelivery?.record ?? this.pendingRequest(), pending = record?.request_id;
    if (!record || !pending) { this.publish({ pending: false }); return true; }
    this.publish({ pending: true });
    if (record.kind === 'inbox' && record.root_work_id) {
      try {
        const message = parseInbox(await this.request('/' + record.root_work_id + '/inbox/by-request/' + pending, { method: 'GET' }), record.root_work_id);
        if (gen !== this.generation) return false;
        if (message.request_id !== pending || message.previous_turn_id !== record.previous_turn_id || !sameImageInputs(message.images,record.images)) throw Error('原补充收件不匹配。');
        return this.finishRecovery(record,message);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404 && error.code.toLowerCase() === 'assistant_inbox_not_found')) throw error;
        this.publish({ notice: '补充发送结果尚未核实，保留原请求继续查询，不会重复发送。' }); return false;
      }
    }
    if (record.root_work_id) {
      try {
        const turn = parseTurn(await this.request('/' + record.root_work_id + '/turns/by-request/' + pending, { method: 'GET' }), record.root_work_id);
        if (gen !== this.generation) return false;
        if (turn.request_id !== pending || turn.previous_turn_id !== record.previous_turn_id || !sameImageInputs(turn.images,record.images)) throw Error('原补充回执不匹配。');
        return this.finishRecovery(record,turn);
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
      if (work.request_id !== pending || !sameImageInputs(work.images,record.images)) throw Error('原工作不匹配。');
      return this.finishRecovery(record,work);
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
    // Cancel only the superseded read. Sends, stops, delivery reconciliation
    // and the work list never use this controller.
    this.conversationRead?.abort();
    this.publish({ selectedId: workId, selectionReady: true, conversation: [], messages: [], conversationLoaded: workId === null, conversationReadBusy: false, draftCheckpointFor: undefined, notice: workId === null ? '新工作：发送会创建独立事项，不会追加到上一件工作。' : readingConversation });
    if (workId !== null) await this.refreshSelected();
  }
  private async fetchSelectedConversation(selected:string, knownRoot?: Work):Promise<{chain:Work[];messages:Work[]}> {
    const read = new AbortController();
    this.conversationRead = read;
    const options = {method:'GET' as const, signal:read.signal};
    const get=async(path:string)=>{
      const raw=await this.request(path,options);
      read.signal.throwIfAborted();
      return raw;
    };
    try {
      if (this.view.inboxSupported) {
        try {
          return parseSnapshot(await get('/' + selected + '/conversation'), selected);
        } catch (error) {
          if (error instanceof ApiError && error.status === 503 && error.code === 'ASSISTANT_CONVERSATION_BUSY' &&
              error.serverError === 'assistant_conversation_busy') throw new ConversationReadBusy();
          throw error;
        }
      }
      const root = knownRoot?.work_id === selected ? knownRoot : parseWork(await get('/' + selected));
      if (root.work_id !== selected) throw Error('所选工作回执不匹配。');
      read.signal.throwIfAborted();
      const chain = this.view.followupsSupported ? parseConversation(root, await get('/' + selected + '/turns')) : [root];
      return {chain,messages:[]};
    } finally {
      if(this.conversationRead===read)this.conversationRead=undefined;
    }
  }
  private async loadConversation(knownRoot?: Work) {
    const selected = this.view.selectedId, selection = this.selectionEpoch, gen = this.generation;
    const checkpointVersion = getCapabilityVersion();
    const previous = this.view.conversation;
    if (selected === null) { this.publish({ conversation: [], messages: [], conversationLoaded: true, conversationReadBusy: false }); return; }
    const {chain,messages}=await this.fetchSelectedConversation(selected,knownRoot);
    const root = chain[0];
    if (gen !== this.generation || selection !== this.selectionEpoch || selected !== this.view.selectedId) return;
    if (previous[0]?.work_id === selected && (chain.length < previous.length || previous.some((old, index) => {
      const fresh = chain[index];
      return !fresh || old.work_id !== fresh.work_id || old.request_id !== fresh.request_id || old.text !== fresh.text ||
        old.previous_turn_id !== fresh.previous_turn_id || old.sequence !== fresh.sequence || !sameImageInputs(fresh.images,old.images) ||
        (old.run_id !== null && old.run_id !== fresh.run_id);
    }))) throw Error('原工作或跟进回执已改变，请核对后继续。');
    if (this.view.messages.some(old => {
      const fresh = messages.find(x => x.work_id === old.work_id);
      return !fresh || old.request_id !== fresh.request_id || old.text !== fresh.text || old.previous_turn_id !== fresh.previous_turn_id ||
        old.inbox_sequence !== fresh.inbox_sequence || old.previous_message_id !== fresh.previous_message_id ||
        old.authority_expires_at !== fresh.authority_expires_at || !sameImageInputs(fresh.images,old.images) ||
        (old.converted_turn_id !== undefined && fresh.converted_turn_id !== old.converted_turn_id) ||
        (old.inbox_intake === 'cancelled' && fresh.inbox_intake !== 'cancelled') ||
        (old.run_id !== null && old.run_id !== fresh.run_id);
    })) throw Error('已保存的补充记录发生变化，请核对后继续。');
    this.publish({ conversation: chain, messages, conversationLoaded: true, conversationReadBusy: false,
      draftCheckpointFor: checkpointVersion===getCapabilityVersion()?{workId:selected,capabilityVersion:checkpointVersion}:undefined,
      ...(this.view.notice === readingConversation ? {notice: '工作与结果已更新。'} : {}),
      historyPresence:{...this.view.historyPresence,[selected]:receiptPresence(chain.at(-1)!)},
      items: this.view.items.map(work => work.work_id === selected ? root : work) });
  }
  private failConversationRead(error: unknown) {
    if (error instanceof AssistantRouteInterrupted) return;
    const busy = error instanceof ConversationReadBusy;
    const prior=this.view.draftCheckpointFor;
    const sameScopeBusy=busy&&prior?.workId===this.view.selectedId&&prior.capabilityVersion===getCapabilityVersion();
    this.publish({ historyPresence:{}, conversation: [], messages: [], conversationLoaded: false, conversationReadBusy: busy,
      // Typed read contention and transport failure only preserve an existing
      // same-scope local draft anchor, never permission to read the conversation.
      draftCheckpointFor: sameScopeBusy||interruptedRead(error)?prior:undefined,
      ...(busy ? {} : { notice: String(error) }) });
  }
  private async refreshSelected() {
    if (!this.routeActive() || !this.canRead() || this.view.busy || this.view.selectedId === null) return;
    this.publish({ busy: true }); const gen = ++this.generation, selection = this.selectionEpoch;
    try { await this.loadConversation(); }
    catch (error) {
      if (this.canRead() && gen === this.generation && selection === this.selectionEpoch)
        this.failConversationRead(error);
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
  private prepareImages(value:ImageInput[]):ImageInput[] | null {
    try { const images=parseImageInputs(value);
      if(images.length && (!this.view.inputPolicy.imageAvailable || images.length>this.view.inputPolicy.maxImages || !hasCapability('media.read') || !hasCapability('media.write'))) throw Error('当前入口或身份尚不能接收图片，附件已保留，未发送。');
      return images;
    } catch(error) { if(this.canRead())this.publish({notice:String(error)});return null; }
  }
  private useInbox(): boolean {
    return this.view.inboxEnabled && ['queued', 'starting', 'created', 'running'].includes(this.view.conversation.at(-1)?.execution_status ?? '');
  }
  followupReason(): string | null {
    if (this.view.selectedId === null) return null;
    if (!this.view.conversationLoaded) return this.view.busy&&!this.view.conversationReadBusy
      ? '正在读取这件事，请稍候。' : '暂时无法核对当前事项。请刷新原记录，不要重发。';
    const root = this.view.conversation[0], last = this.view.conversation.at(-1);
    if (!root || !last || root.work_id !== this.view.selectedId) return '请先读取原工作。';
    if (!['accepted', 'assigned', 'in_progress'].includes(root.task_state ?? '')) return '这项工作已经关闭或不能继续，请新建一件工作。';
    if (last.progress.uncertain) return '上一轮结果尚未核实，核对后才能继续。原文会保留。';
    if (this.useInbox()) return this.view.inboxAvailable ? null : this.unavailableMessage();
    if (this.view.messages.some(x => x.inbox_intake === 'received')) return '已有消息已保存、尚未接续。请等待核对或新建工作，不会重复发送。';
    if (!this.view.followupsAvailable) return this.view.unavailableReason === 'trial_expired'
      ? this.unavailableMessage() : '当前服务暂未启用继续处理，已保存的工作仍可查看。';
    if (!['completed', 'failed', 'cancelled', 'blocked'].includes(last.execution_status))
      return last.execution_status === 'not_dispatched' ? '这条历史消息尚未执行，需要先核对。' : '正在处理这件事。你可以先写下要求，或新建另一件工作。';
    return null;
  }
  private hasConnectionBeforeSend():boolean {
    if(!browserReportsOffline())return true;
    if(this.canRead()&&!this.view.pending)this.publish({notice:offlineBeforeSendNotice});
    return false;
  }
  supportsDraftSourceRow():boolean {
    // First protocol version persists references only on the explicit /turns route.
    return this.view.sourceRowReferencesAvailable&&this.view.conversationLoaded&&!!this.view.selectedId&&
      !!this.view.conversation.at(-1)?.run_id&&!this.useInbox()&&canDraftReceiptDetail(hasCapability);
  }
  private currentDraftSourceRow(source:DraftSourceRow,text:string):boolean {
    return this.supportsDraftSourceRow()&&source.rootWorkId===this.view.selectedId&&
      source.reference.source_run_id===this.view.conversation.at(-1)?.run_id&&validSourceRowRef(source.reference)&&
      !!source.anchor&&text.includes(source.anchor);
  }
  private blockStaleDraftSource(source:DraftSourceRow|undefined,text:string):boolean {
    if(!source||this.currentDraftSourceRow(source,text))return false;
    if(this.canRead())this.publish({notice:'原单引用已变化或不可用，未发送。请重新选单或手动移除引用。'});
    return true;
  }
  async sendMessage(value: string, images:ImageInput[]=[], recovered?:()=>void, draft?:DraftSubmission): Promise<boolean> {
    if (!this.view.selectionReady || this.view.stoppingId !== null || !this.hasConnectionBeforeSend() || this.blockStaleDraftSource(draft?.sourceRow,value)) return false;
    return this.view.selectedId === null ? this.submit(value,images,recovered,draft) : this.useInbox() ? this.supplement(value,images,recovered,draft) : this.followup(value,images,recovered,draft);
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
    if (!this.routeActive() || !this.canRead() || this.view.busy) return;
    this.lastListAt = Date.now();
    this.publish({ busy: true }); const gen = ++this.generation, selection = this.selectionEpoch;
    try {
      const status = row(await this.request('/intake-status', { method: 'GET' }));
      const inputPolicy=parseInputPolicy(status);
      if (gen !== this.generation) return;
      if (typeof status.intake_available !== 'boolean') throw Error('入口状态异常。');
      const supported = status.followups_supported ?? false, available = status.followups_available ?? false;
      if (typeof supported !== 'boolean' || typeof available !== 'boolean' || available && !supported) throw Error('跟进入口状态异常。');
      const inboxSupported = status.inbox_supported ?? false, inboxEnabled = status.inbox_enabled ?? false, inboxAvailable = status.inbox_available ?? false;
      const unavailableReason = status.unavailable_reason ?? null;
      if (typeof inboxSupported !== 'boolean' || typeof inboxEnabled !== 'boolean' || typeof inboxAvailable !== 'boolean' ||
          inboxEnabled && !inboxSupported || inboxAvailable && !inboxEnabled ||
          (unavailableReason !== null && unavailableReason !== 'trial_expired' && unavailableReason !== 'unavailable')) throw Error('补充入口状态异常。');
      this.publish({ inputPolicy, followupsSupported: supported, followupsAvailable: available, inboxSupported, inboxEnabled, inboxAvailable, unavailableReason,
        sourceRowReferencesAvailable:status.authorized_source_row_ref_available===true });
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
          this.failConversationRead(error);
      }
    } catch (error) {
      if (error instanceof AssistantRouteInterrupted) return;
      if (this.canRead() && gen === this.generation) this.publish({ historyPresence:{}, items: [], conversation: [], messages: [], conversationLoaded: false,
        draftCheckpointFor: interruptedRead(error)?this.view.draftCheckpointFor:undefined, intakeAvailable: false, notice: String(error) });
    } finally {
      if (this.canRead() && gen === this.generation) {
        this.publish({ busy: false });
        if (!this.readOnResume() && selection !== this.selectionEpoch) void this.refreshSelected();
      }
    }
  }
  private async refreshProgress(original: Work) {
    if (!this.routeActive() || !this.canRead() || this.view.busy) return;
    this.publish({ busy: true }); const gen = ++this.generation;
    try {
      const work = parseWork(await this.request('/' + original.work_id, { method: 'GET' }));
      if (work.work_id !== original.work_id || work.request_id !== original.request_id || work.text !== original.text ||
          (original.run_id !== null && work.run_id !== original.run_id)) throw Error('工作进度回执不匹配。');
      if (this.canRead() && gen === this.generation) this.publish({ items: this.view.items.map(
        item => item.work_id === work.work_id ? work : item), conversation: this.view.conversation.map(
        item => item.work_id === work.work_id ? work : item) });
    } catch (error) {
      if (error instanceof AssistantRouteInterrupted) return;
      if (this.canRead() && gen === this.generation) this.publish({ notice: String(error) });
    } finally { if (this.canRead() && gen === this.generation) this.publish({ busy: false }); }
  }
  async loadMore() {
    if (!this.routeActive() || !this.canRead() || this.view.busy || !this.view.hasMore || !this.view.items.length) return;
    this.publish({ busy: true }); const gen = ++this.generation;
    try {
      const items = await this.list(this.view.items.at(-1)!.work_id);
      if (this.canRead() && gen === this.generation) {
        const combined = [...this.view.items, ...items];
        if (new Set(combined.map(x => x.work_id)).size !== combined.length) throw Error('往期工作发生变化，请刷新。');
        this.publish({ items: combined, hasMore: items.length === 50 });
      }
    } catch (error) { if (!(error instanceof AssistantRouteInterrupted) && this.canRead() && gen === this.generation) this.publish({ notice: String(error) }); }
    finally { if (this.canRead() && gen === this.generation) this.publish({ busy: false }); }
  }
  async supplement(textValue: string, imageInputs:ImageInput[]=[], recovered?:()=>void, draft?:DraftSubmission): Promise<boolean> {
    const permit = this.route?.signal();
    if (permit?.aborted) return false;
    const content = textValue.trim(), reason = this.followupReason();
    if (!content || !this.canRead() || !hasCapability('tasks.manage') || this.view.mutating || !this.view.selectedId || !this.view.inboxAvailable || !this.useInbox()) return false;
    if (reason) { this.publish({ notice: reason }); return false; }
    if(this.blockStaleDraftSource(draft?.sourceRow,content))return false;
    const images=this.prepareImages(imageInputs);if(images===null)return false;
    const imageFields=images.length?{images}:{};
    if (new TextEncoder().encode(JSON.stringify(content)).length > this.view.inputPolicy.maxTextJsonBytes) {
      this.publish({ notice: '这条消息超出当前传输容量，请分段发送；原文已保留，尚未发送。' }); return false;
    }
    const root = this.view.selectedId, parent = this.view.conversation.at(-1)!, selection = this.selectionEpoch;
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation; let success = false, changed = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复发送，请使用支持 Web Locks 的浏览器。');
      const run = async () => {
        if (permit?.aborted || !this.canRead() || gen !== this.generation || selection !== this.selectionEpoch || !hasCapability('tasks.manage')) return;
        if (this.localDelivery || this.pendingId()) { this.publish({ pending: true, notice: '先核对上次发送的补充。' }); return; }
        if(!this.hasConnectionBeforeSend())return;
        const record: PendingRequest = { request_id: crypto.randomUUID(), root_work_id: root, previous_turn_id: parent.work_id, kind: 'inbox', ...imageFields };
        this.storage.setItem(this.journalKey, JSON.stringify(record));
        if (JSON.stringify(this.pendingRequest()) !== JSON.stringify(record)) throw Error('无法保存请求标识，尚未发送。');
        this.localDelivery={record,content,sending:true,recovered:()=>{notifyConfirmedFollowup(draft);recovered?.();}};
        this.publish({ pending: true,recoveredDelivery:null });
        draft?.onJournaled();
        try {
          const message = parseInbox(await this.request('/' + root + '/inbox', { method: 'POST',
            body: { text: content, request_id: record.request_id, parent_id: parent.work_id, ...imageFields }, idempotencyKey: record.request_id }), root);
          if (message.request_id !== record.request_id || message.text !== content || !sameImageInputs(message.images,images) || message.previous_turn_id !== parent.work_id ||
              this.view.messages.some(x => x.work_id === message.work_id || x.request_id === message.request_id)) throw Error('补充收件回执不匹配。');
          this.clearPending(record.request_id); success = true;
          if (selection === this.selectionEpoch) this.publish({ messages: [...this.view.messages, message] });
          this.publish({ notice: '补充已保存，进展会显示在这条消息下面。' });
          notifyConfirmedFollowup(draft);
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError && error.code !== 'INVALID_ENVELOPE' &&
              ([400, 401, 403, 404, 409, 422].includes(error.status) || error.status === 503 && error.code.toLowerCase() === 'assistant_inbox_unavailable')) {
            this.clearPending(record.request_id);
            if(error.status===503)draft?.onNotAccepted();
            changed = error.status === 409;
            this.publish({ notice: changed ? '工作状态已更新，请查看最新进展后发送；补充原文已保留。' : error.userMessage }); return;
          }
          throw error;
        } finally { this.finishSending(record.request_id); }
      };
      // Web Locks cancels a queued gesture, not a callback that already dispatched.
      if (permit) await navigator.locks.request(this.journalKey, { signal: permit }, run);
      else await navigator.locks.request(this.journalKey, run);
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
  async followup(textValue: string, imageInputs:ImageInput[]=[], recovered?:()=>void, draft?:DraftSubmission): Promise<boolean> {
    const permit = this.route?.signal();
    if (permit?.aborted) return false;
    const content = textValue.trim(), reason = this.followupReason();
    if (!content || !this.canRead() || !hasCapability('tasks.manage') || this.view.mutating || !this.view.selectedId || this.useInbox()) return false;
    if (reason) { this.publish({ notice: reason }); return false; }
    const source=draft?.sourceRow;
    if(this.blockStaleDraftSource(source,content))return false;
    const sourceFields=source?{source_row_ref:wireSourceRowRef(source.reference)}:{};
    const images=this.prepareImages(imageInputs);if(images===null)return false;
    const imageFields=images.length?{images}:{};
    if (new TextEncoder().encode(JSON.stringify(content)).length > this.view.inputPolicy.maxTextJsonBytes) {
      this.publish({ notice: '这条消息超出当前传输容量，请分段发送；原文已保留，尚未发送。' }); return false;
    }
    const root = this.view.selectedId, parent = this.view.conversation.at(-1)!;
    const chain = this.view.conversation, selection = this.selectionEpoch;
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation; let success = false, changed = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复发送，请使用支持 Web Locks 的浏览器。');
      const run = async () => {
        if (permit?.aborted || !this.canRead() || gen !== this.generation || selection !== this.selectionEpoch || !hasCapability('tasks.manage')) return;
        if(this.blockStaleDraftSource(source,content))return;
        if (this.localDelivery || this.pendingId()) { this.publish({ pending: true, notice: '先核对上次交代的工作。' }); return; }
        if(!this.hasConnectionBeforeSend())return;
        if(source&&draft?.closedSourceCheck){
          // A closed panel never polls. Explicit Send rechecks in this existing
          // lock before journaling, so reading cannot create or replay a Work.
          const check=draft.closedSourceCheck, run=source.reference.source_run_id;
          const current=()=>!permit?.aborted&&gen===this.generation&&selection===this.selectionEpoch&&
            this.canRead()&&hasCapability('tasks.manage')&&this.currentDraftSourceRow(source,content)&&check.stillCurrent();
          if(!current())return;
          let fresh;
          try{
            fresh=parseResultSnapshot(await this.readRunResults(root,run,new AbortController().signal),
              {workId:root,runId:run,executionStatus:parent.execution_status});
          }catch{
            if(current())this.publish({notice:'暂未核对到原单引用，尚未发送。你可以手动再核对，或移除引用后发送。'});
            return;
          }
          if(!current()){
            if(this.canRead()&&selection===this.selectionEpoch)
              this.publish({notice:'草稿或权限已变化，尚未发送。请查看后手动发送。'});
            return;
          }
          if(!snapshotMatchesDraftSource(fresh,source)){
            check.onChanged();
            return;
          }
          if (this.localDelivery || this.pendingId()) { this.publish({ pending: true, notice: '先核对上次交代的工作。' }); return; }
        }
        if(permit?.aborted||!this.hasConnectionBeforeSend())return;
        const record = { request_id: crypto.randomUUID(), root_work_id: root, previous_turn_id: parent.work_id, ...imageFields };
        this.storage.setItem(this.journalKey, JSON.stringify(record));
        if (JSON.stringify(this.pendingRequest()) !== JSON.stringify(record)) throw Error('无法保存请求标识，尚未发送。');
        this.localDelivery={record,content,sending:true,recovered:()=>{notifyConfirmedFollowup(draft);recovered?.();}};
        this.publish({ pending: true,recoveredDelivery:null });
        draft?.onJournaled();
        try {
          const turn = parseTurn(await this.request('/' + root + '/turns', { method: 'POST',
            body: { text: content, request_id: record.request_id, previous_turn_id: parent.work_id, ...imageFields, ...sourceFields }, idempotencyKey: record.request_id }), root);
          if (turn.request_id !== record.request_id || turn.text !== content || !sameImageInputs(turn.images,images) || turn.previous_turn_id !== parent.work_id ||
              turn.sequence !== chain.length + 1 || chain.some(item => item.work_id === turn.work_id || (turn.run_id && item.run_id === turn.run_id))) throw Error('补充回执不匹配。');
          this.clearPending(record.request_id); success = true;
          if (selection === this.selectionEpoch) this.publish({ conversation: [...chain, turn], conversationLoaded: true });
          this.publish({ notice: '补充已保存，正在继续这件事。' });
          notifyConfirmedFollowup(draft);
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError && error.code !== 'INVALID_ENVELOPE' &&
              ([400, 401, 403, 404, 409, 422].includes(error.status) ||
                error.status === 503 && error.code.toLowerCase() === 'assistant_followup_unavailable')) {
            this.clearPending(record.request_id);
            if(error.status===503)draft?.onNotAccepted();
            changed = error.status === 409;
            this.publish({ notice: changed ? '这件事的状态已更新。请查看最新进展再发送，补充原文已保留。' : error.userMessage });
            return;
          }
          throw error;
        } finally { this.finishSending(record.request_id); }
      };
      // Web Locks cancels a queued gesture, not a callback that already dispatched.
      if (permit) await navigator.locks.request(this.journalKey, { signal: permit }, run);
      else await navigator.locks.request(this.journalKey, run);
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
  async submit(textValue: string, imageInputs:ImageInput[]=[], recovered?:()=>void, draft?:DraftSubmission): Promise<boolean> {
    const permit = this.route?.signal();
    if (permit?.aborted) return false;
    const content = textValue.trim();
    if(draft?.sourceRow){
      if(this.canRead())this.publish({notice:'已有原单引用只能在原事项里使用，未发送。'});
      return false;
    }
    if (!content || !this.canRead() || !hasCapability('tasks.manage') || this.view.mutating || !this.view.intakeAvailable) return false;
    const images=this.prepareImages(imageInputs);if(images===null)return false;
    const imageFields=images.length?{images}:{};
    if (exceedsNewIntakeTextBudget(content,this.view.inputPolicy.maxTextJsonBytes)) {
      this.publish({ notice: '这条消息超出当前传输容量，请分段发送；原文已保留，尚未发送。' }); return false;
    }
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation, selection = this.selectionEpoch; let success = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复发送，请使用支持 Web Locks 的浏览器。');
      const run = async () => {
        if (permit?.aborted || !this.canRead() || gen !== this.generation || selection !== this.selectionEpoch || !hasCapability('tasks.manage')) return;
        if (this.localDelivery || this.pendingId()) { this.publish({ pending: true, notice: '先核对上次交代的工作。' }); return; }
        if(!this.hasConnectionBeforeSend())return;
        const requestId = crypto.randomUUID();
        this.storage.setItem(this.journalKey, JSON.stringify({ request_id: requestId, ...imageFields }));
        if (this.pendingId() !== requestId) throw Error('无法保存请求标识，尚未发送。');
        this.localDelivery={record:{request_id:requestId,...imageFields},content,sending:true,recovered};
        this.publish({ pending: true,recoveredDelivery:null });
        draft?.onJournaled();
        try {
          const work = parseWork(await this.request('', { method: 'POST', body: { text: content, request_id: requestId, ...imageFields }, idempotencyKey: requestId }));
          if (work.request_id !== requestId || work.text !== content || !sameImageInputs(work.images,images)) throw Error('工作回执不匹配。');
          this.clearPending(requestId); success = true;
          this.publish({ items: [work, ...this.view.items.filter(x => x.work_id !== work.work_id)].slice(0, Math.max(50, this.view.items.length)), notice: '工作已保存，进度与结果会显示在这里。' });
          if (selection === this.selectionEpoch) {
            this.autoSelect = false;
            this.publish({ selectedId: work.work_id, selectionReady: true, conversation: [work], messages: [], conversationLoaded: true });
          }
        } catch (error) {
          if (this.isCurrent() && error instanceof ApiError && error.code !== 'INVALID_ENVELOPE' &&
              ([400, 401, 403, 404, 409].includes(error.status) ||
                isNewIntakeValidationRejection(error.status,error.code) ||
                error.status === 503 && ['ASSISTANT_INTAKE_UNAVAILABLE', 'assistant_intake_unavailable'].includes(error.code))) {
            const rejected=isNewIntakeValidationRejection(error.status,error.code);
            this.clearPending(requestId);
            if(error.status===503||rejected)draft?.onNotAccepted();
            if(rejected){
              this.publish({notice:'这条消息未通过校验，未被受理。原文仍可编辑，修改后请手动发送。'});
              return;
            }
          }
          throw error;
        } finally { this.finishSending(requestId); }
      };
      // Web Locks cancels a queued gesture, not a callback that already dispatched.
      if (permit) await navigator.locks.request(this.journalKey, { signal: permit }, run);
      else await navigator.locks.request(this.journalKey, run);
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
    const permit = this.route?.signal();
    if (permit?.aborted) return false;
    if (!this.canRead() || !hasCapability('tasks.manage') || this.view.mutating) return false;
    const original = [...this.view.messages, ...this.view.conversation, ...this.view.items].find(w => w.work_id === workId);
    if (!original || !(['queued', 'starting', 'created', 'running', 'waiting_for_human', 'waiting_for_external'].includes(original.execution_status) ||
        original.execution_status === 'not_dispatched' && original.message_id && original.inbox_intake === 'received')) return false;
    this.publish({ busy: true, mutating: true }); const gen = ++this.generation; let success = false;
    try {
      if (!globalThis.navigator?.locks) throw Error('此浏览器无法保护重复操作，请使用支持 Web Locks 的浏览器。');
      const run = async () => {
        if (permit?.aborted || !this.canRead() || gen !== this.generation || !hasCapability('tasks.manage')) return;
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
              work.previous_turn_id !== original.previous_turn_id || work.sequence !== original.sequence || !sameImageInputs(work.images,original.images) ||
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
      };
      // Web Locks cancels a queued gesture, not a callback that already dispatched.
      if (permit) await navigator.locks.request(this.journalKey, { signal: permit }, run);
      else await navigator.locks.request(this.journalKey, run);
    } catch (error) {
      if (this.canRead() && gen === this.generation) this.publish({ notice: this.view.stoppingId
        ? '停止结果待核对，已保留原请求。刷新只查询原工作，不会重复执行。' : String(error) });
    } finally { if (this.canRead() && gen === this.generation) this.publish({ busy: false, mutating: false }); }
    if (success && this.canRead() && this.view.selectedId) void this.refreshSelected();
    return success;
  }
}
