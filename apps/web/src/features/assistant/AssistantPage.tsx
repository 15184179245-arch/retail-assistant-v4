import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import type { Session } from '../../lib/auth/session';
import { getScopeVersion, getSession, logoutSession, sessionIsExpired, subscribe } from '../../lib/auth/session';
import { useCapabilities } from '../../lib/auth/capabilities';
import { navigate } from '../../app/router';
import { ConnectionBanner } from '../../app/ConnectionBanner';
import { AnswerText } from '../agents/AnswerText';
import { AssistantController, statusLabels } from './assistantApi';
import type { Work } from './assistantApi';
import { ExecutionTrace } from './ExecutionTrace';
import '../../styles/assistant-workspace.css';

function Icon({ name }: { name: 'sidebar' | 'new' | 'memory' | 'home' | 'send' | 'down' | 'refresh' | 'check' | 'logout' }) {
  const paths = { sidebar: <><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/></>,
    new: <><path d="M12 5H6a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-6M14 5l5 5M10 14l1-4 8-8 4 4-8 8z"/></>,
    memory: <><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 3v18M12 8h4M12 12h4"/></>,
    home: <><path d="m3 10 9-7 9 7v10H3zM9 20v-7h6v7"/></>, send: <path d="M12 20V4m-7 7 7-7 7 7"/>,
    down: <path d="M12 4v16m-7-7 7 7 7-7"/>, refresh: <><path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M18 18a8 8 0 0 1-13-2"/></>,
    check: <path d="m5 12 4 4L19 6"/>, logout: <><path d="M10 4H4v16h6M9 12h12m-4-4 4 4-4 4"/></> };
  return <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}
function UserMessage({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const long = text.length > 600;
  return <div className="assistant-user-message"><div className="assistant-user-bubble">
    <p className={long && !expanded ? 'is-collapsed' : undefined}>{text}</p>
    {long && <button type="button" className="assistant-expand-message" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起全文' : '展开全文'}</button>}
  </div></div>;
}
const activeStates = new Set(['queued', 'starting', 'created', 'running']);
function Reply({ work, stop, stopping, busy }: { work: Work; stop?: () => void; stopping: boolean; busy: boolean }) {
  return <div className="assistant-reply">
    <div className="assistant-reply-mark" aria-hidden="true">三</div>
    <div className="assistant-reply-content">
      {work.answer !== undefined ? <>
        <AnswerText text={work.answer} />
        <div className="assistant-message-meta"><Icon name="check"/><span>已答复</span>
          {!!work.citations.length && <details className="assistant-sources"><summary>查看依据 · {work.citations.length}</summary><ul>{work.citations.map((c, i) => <li key={`${c.kind}:${c.objectId}:${i}`}>{c.label} · 版本 {c.version}</li>)}</ul></details>}
        </div>
      </> : <div className={`assistant-progress ${activeStates.has(work.execution_status) ? 'is-working' : ''}`}>
        {activeStates.has(work.execution_status) && <span className="assistant-thinking" aria-hidden="true"><i/><i/><i/></span>}
        <span>{work.answerUnavailable ? '这条答复当前不可用，需核对保存结果或依据。' : work.progress.activity ?? statusLabels[work.execution_status]}</span>
        {work.progress.explanation && <p>{work.progress.explanation}</p>}
        {work.progress.uncertain && <p>部分执行结果尚未核实，请勿重复发送同一项工作。刷新只核对原记录，不会重新执行。</p>}
      </div>}
      {stop && <button type="button" className="assistant-stop" disabled={busy || stopping} onClick={stop}>
        {stopping ? '正在核对停止结果…' : work.inbox_intake === 'received' ? '取消补充' : '停止工作'}</button>}
      <ExecutionTrace progress={work.progress} workId={work.work_id} runId={work.run_id}/>
    </div>
  </div>;
}

export function AssistantPage({ session, baseUrl = '' }: { session: Session; baseUrl?: string }) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const caps = useCapabilities();
  const selectionKey = 'v4-assistant-selected:' + JSON.stringify([session.organizationId, session.storeId, session.subjectId]);
  const controller = useMemo(() => {
    let initial: string | null | undefined;
    try { const saved = localStorage.getItem(selectionKey); initial = saved === 'new' ? null : saved ?? undefined; } catch { /* Optional selection only. */ }
    return new AssistantController(session, {
      getItem: key => localStorage.getItem(key), setItem: (key, value) => localStorage.setItem(key, value), removeItem: key => localStorage.removeItem(key),
    }, baseUrl, initial);
  }, [session, scope, baseUrl, selectionKey]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [draftState, setDraftState] = useState<{ owner: AssistantController; epoch: number; values: Record<string, string> }>(
    () => ({ owner: controller, epoch: view.privacyEpoch, values: {} }));
  const draftKey = view.selectedId ?? 'new';
  const draftsCurrent = draftState.owner === controller && draftState.epoch === view.privacyEpoch;
  const draft = draftsCurrent ? draftState.values[draftKey] ?? '' : '';
  function setDraft(value: string) {
    setDraftState(current => ({ owner: controller, epoch: view.privacyEpoch,
      values: { ...(current.owner === controller && current.epoch === view.privacyEpoch ? current.values : {}), [draftKey]: value } }));
  }
  const [sidebarOpen, setSidebarOpen] = useState(() => typeof window === 'undefined' || window.innerWidth > 760);
  const [atBottom, setAtBottom] = useState(true);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const stream = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const timeline = controller.displayConversation();
  const updateKey = `${view.selectedId}:${timeline.map(x => `${x.work_id}:${x.execution_status}:${x.answer?.length}`).join('|')}:${view.privacyEpoch}`;
  useEffect(() => { controller.start(); return () => controller.dispose(); }, [controller]);
  useEffect(() => { setDraftState({ owner: controller, epoch: view.privacyEpoch, values: {} }); }, [controller, view.privacyEpoch]);
  useEffect(() => {
    if (!view.selectionReady || !controller.isCurrent()) return;
    try { localStorage.setItem(selectionKey, view.selectedId ?? 'new'); } catch { /* Reads and in-memory drafts still work. */ }
  }, [controller, selectionKey, view.selectionReady, view.selectedId]);
  useLayoutEffect(() => { const el = textarea.current; if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 200)}px`; } }, [draft]);
  useLayoutEffect(() => { if (followLatest.current && stream.current) stream.current.scrollTop = stream.current.scrollHeight; }, [updateKey]);
  useEffect(() => { const close = (e: KeyboardEvent) => { if (e.key === 'Escape') setSidebarOpen(false); }; window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close); }, []);
  if (getSession() !== session || sessionIsExpired(session) || !caps.hasCapability('agent.run') || !caps.hasCapability('tasks.read_self')) {
    return <p role="alert">请使用有权账号重新进入助理。</p>;
  }
  const followupReason = controller.followupReason();
  const disabled = view.mutating || view.pending || !view.selectionReady || view.stoppingId !== null ||
    (view.selectedId === null ? !view.intakeAvailable : followupReason !== null) || !caps.hasCapability('tasks.manage');
  const routineNotice = ['工作与结果已更新。', '工作已保存，进度与结果会显示在这里。'].includes(view.notice);
  function bottom() { followLatest.current = true; setAtBottom(true); stream.current?.scrollTo({ top: stream.current.scrollHeight, behavior: 'smooth' }); }
  function focusComposer() { void controller.selectWork(null); bottom(); textarea.current?.focus(); if (window.innerWidth <= 760) setSidebarOpen(false); }
  function locate(id: string) {
    followLatest.current = true; setAtBottom(true); void controller.selectWork(id);
    if (window.innerWidth <= 760) setSidebarOpen(false);
  }
  async function submit() {
    if (disabled || !draft.trim()) return;
    const sent = draft, key = draftKey, epoch = view.privacyEpoch;
    if (await controller.sendMessage(sent) && controller.isCurrent()) {
      setDraftState(current => current.owner === controller && current.epoch === epoch && current.values[key] === sent
        ? { ...current, values: { ...current.values, [key]: '' } } : current);
      followLatest.current = true;
      requestAnimationFrame(() => { if (stream.current) stream.current.scrollTop = stream.current.scrollHeight; });
    }
  }
  return <section className={`assistant-layout ${sidebarOpen ? 'is-sidebar-open' : ''}`} aria-label="零售助理对话工作台">
    {sidebarOpen && <button className="assistant-sidebar-scrim" aria-label="关闭侧栏" onClick={() => setSidebarOpen(false)} />}
    <aside className="assistant-sidebar" aria-label="助理导航" inert={!sidebarOpen}>
      <div className="assistant-sidebar-brand"><span className="assistant-brand-symbol">零</span><strong>零售助理</strong>
        <button className="assistant-icon-button" aria-label="收起侧栏" onClick={() => setSidebarOpen(false)}><Icon name="sidebar"/></button>
      </div>
      <button className="assistant-nav-button" onClick={focusComposer}><Icon name="new"/>交代新工作</button>
      {caps.hasCapability('memory.read') && <button className="assistant-nav-button" onClick={() => navigate('/memory')}><Icon name="memory"/>助理记忆</button>}
      <button className="assistant-nav-button" onClick={() => navigate('/')}><Icon name="home"/>经营工作台</button>
      <div className="assistant-history-heading">最近工作 <span>{view.items.length || ''}</span></div>
      <nav className="assistant-history" aria-label="最近工作">
        {view.items.map(work => <button key={work.work_id} className={view.selectedId === work.work_id ? 'is-selected' : ''} onClick={() => locate(work.work_id)} title={work.title}>
          <span>{work.title}</span>{activeStates.has(work.execution_status) && <i aria-label={statusLabels[work.execution_status]} className="assistant-work-dot"/>}
        </button>)}
        {!view.items.length && <p className="assistant-sidebar-empty">你的工作会保存在这里</p>}
        {view.hasMore && <button disabled={view.busy} onClick={() => { followLatest.current = false; void controller.loadMore(); }}>查看更早的工作</button>}
      </nav>
      <div className="assistant-account"><span className="assistant-avatar">{session.username.slice(0, 1).toUpperCase()}</span><div><strong>{session.username}</strong><small>本机开发试用</small></div>
        <button className="assistant-icon-button" title="退出登录" aria-label="退出登录" onClick={() => { void logoutSession().then(() => navigate('/login')); }}><Icon name="logout"/></button>
      </div>
    </aside>
    <div className="assistant-main">
      <header className="assistant-topbar">
        {!sidebarOpen && <button className="assistant-icon-button" aria-label="展开侧栏" onClick={() => setSidebarOpen(true)}><Icon name="sidebar"/></button>}
        <h1>我的助理</h1><span className="assistant-mode">工作与记忆持续保留</span>
        <button className="assistant-icon-button assistant-refresh" aria-label="刷新工作" title="刷新工作" disabled={view.busy} onClick={() => { void controller.refresh(); }}><Icon name="refresh"/></button>
      </header>
      <ConnectionBanner session={session}/>
      <div ref={stream} className="assistant-stream" aria-label="对话记录" tabIndex={0} onScroll={() => {
        const el = stream.current; if (!el) return; const near = el.scrollHeight - el.scrollTop - el.clientHeight < 100; followLatest.current = near; setAtBottom(near);
      }}>
        <div className="assistant-conversation">
          {view.selectionReady && view.selectedId === null ? <div className="assistant-welcome"><span className="assistant-welcome-mark" aria-hidden="true">三</span><h2>今天想一起做点什么？</h2><p>直接告诉我。工作会留下来，记忆也会。</p>
            <div className="assistant-suggestions">{['帮我整理今天的工作', '我之前让你记住了什么？', '帮我梳理一下这个想法'].map(text => <button key={text} onClick={() => { setDraft(text); textarea.current?.focus(); }}>{text}</button>)}</div>
          </div> : <>
            {!view.conversationLoaded && <p role="status">{view.busy ? '正在读取这件事…' : '暂时无法读取，请刷新重试。'}</p>}
            {timeline.map(work => <article id={'work-' + work.work_id} key={work.work_id} className="assistant-turn" aria-label={work.title}>
              <UserMessage text={work.text} />
              <time className="assistant-message-time" dateTime={work.created_at}>{new Date(work.created_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time>
              <Reply work={work} stopping={view.stoppingId === work.work_id} busy={view.mutating}
                stop={caps.hasCapability('tasks.manage') && (['queued', 'starting', 'created', 'running', 'waiting_for_human', 'waiting_for_external'].includes(work.execution_status) ||
                  work.execution_status === 'not_dispatched' && work.message_id && work.inbox_intake === 'received')
                  ? () => { void controller.stop(work.work_id); } : undefined}/>
            </article>)}
          </>}
        </div>
      </div>
      <div className="assistant-composer-dock">
        {!atBottom && <button type="button" className="assistant-jump" onClick={bottom}><Icon name="down"/>回到最新</button>}
        {!routineNotice && <p className="assistant-notice" role="status">{view.notice}</p>}
        {followupReason && <p className="assistant-notice" role="status">{followupReason}</p>}
        <form className="assistant-composer" onSubmit={event => { event.preventDefault(); void submit(); }}>
          <label className="assistant-sr-only" htmlFor="assistant-request">给助理发消息</label>
          <textarea ref={textarea} id="assistant-request" rows={1} value={draft} onChange={event => setDraft(event.target.value)}
            placeholder={view.selectedId ? '继续这件事…' : '交代一件新工作…'} onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
                event.preventDefault(); if (!disabled && draft.trim()) void submit();
              }
            }}/>
          <div className="assistant-composer-toolbar"><span><span className="assistant-composer-dot"/>零售助理</span>
            <span className="assistant-key-hint">Enter 发送 · Shift + Enter 换行</span>
            <button className="assistant-send" type="submit" aria-label="发送消息" disabled={disabled || !draft.trim()}><Icon name="send"/></button>
          </div>
        </form>
        <p className="assistant-composer-footnote">{view.pending ? '正在核对发送结果，请勿重复发送' : '工作与结果会保存，关闭页面后可回来继续查看'}</p>
      </div>
    </div>
  </section>;
}
