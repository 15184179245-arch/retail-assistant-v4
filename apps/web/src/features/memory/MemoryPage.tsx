import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { Session } from '../../lib/auth/session';
import { getSession, getScopeVersion, sessionIsExpired, subscribe } from '../../lib/auth/session';
import { useCapabilities } from '../../lib/auth/capabilities';
import { MemoryController } from './memoryApi';
import type { Memory } from './memoryApi';

export function MemoryPage({ session, baseUrl = '' }: { session: Session; baseUrl?: string }) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const caps = useCapabilities();
  const controller = useMemo(() => new MemoryController(session, {
    getItem: k => localStorage.getItem(k), setItem: (k, v) => localStorage.setItem(k, v), removeItem: k => localStorage.removeItem(k),
  }, baseUrl), [session, scope, baseUrl]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState('');
  const [purpose, setPurpose] = useState('工作偏好与经验');
  const [editing, setEditing] = useState<Memory | null>(null);
  const [markdown, setMarkdown] = useState('');
  useEffect(() => { controller.start(); return () => controller.dispose(); }, [controller]);
  useEffect(() => { setDraft(''); setEditing(null); setMarkdown(''); setQuery(''); }, [controller, view.privacyEpoch]);
  useEffect(() => { if (view.note) setMarkdown(view.note.markdown); }, [view.note]);
  if (getSession() !== session || sessionIsExpired(session) || !caps.hasCapability('memory.read')) return <p role="alert">请使用有权账号重新进入助理记忆。</p>;
  const canWrite = caps.hasCapability('memory.write');
  const disabled = view.busy || view.pending;
  const items = view.items.filter(x => `${x.content} ${x.purpose}`.includes(query.trim()));
  function resetDraft() { setDraft(''); setEditing(null); }
  async function save() {
    const ok = editing ? await controller.correct(editing, draft) : await controller.save(draft, purpose);
    if (ok && controller.isCurrent()) resetDraft();
  }
  function download() {
    const note = controller.getSnapshot().note;
    if (!controller.isCurrent() || !note) return;
    const url = URL.createObjectURL(new Blob([note.markdown], { type: 'text/markdown;charset=utf-8' }));
    const a = document.createElement('a'); a.href = url; a.download = note.filename; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section style={{ maxWidth: 960, margin: '24px auto', padding: 24 }} aria-labelledby="memory-title">
    <h2 id="memory-title">助理记忆</h2>
    <p>主助理与获授权的执行智能体共用当前账号、门店的记忆。你可以查看、纠正或撤回。</p>
    <p role="status">{view.notice}</p>
    <button type="button" disabled={view.busy} onClick={() => { void controller.refresh(); }}>刷新并核对原操作</button>
    <p><label>查找记忆 <input value={query} onChange={e => setQuery(e.target.value)} placeholder="在已加载的记忆中查找" /></label></p>
    {!items.length && <p>当前没有匹配的记忆。</p>}
    <ul style={{ paddingLeft: 20 }}>{items.map(item => <li key={item.id} style={{ marginBottom: 24 }}>
      <p style={{ whiteSpace: 'pre-wrap' }}>{item.content}</p>
      <p>{item.purpose} · 版本 {item.version} · {item.retain_until_withdrawn ? '保留至你撤回' : `有效至 ${item.expires_at}`} · 来源：{item.source}</p>
      <button type="button" disabled={view.busy} onClick={() => { void controller.exportNote(item.id); }}>导出笔记</button>{' '}
      {canWrite && <>
        <button type="button" disabled={disabled} onClick={() => { setEditing(item); setDraft(item.content); }}>编辑这条记忆</button>{' '}
        <button type="button" disabled={disabled} onClick={() => { void controller.withdraw(item); }}>撤回这条记忆</button>
      </>}
    </li>)}</ul>
    {canWrite && <form onSubmit={e => { e.preventDefault(); void save(); }}>
      <h3>{editing ? '纠正记忆' : '添加长期记忆'}</h3>
      {editing ? <p>正在编辑版本 {editing.version}。版本冲突时保留此草稿，请核对最新内容后重新选择记录。</p> :
        <p><label>记忆用途 <input required maxLength={200} value={purpose} onChange={e => setPurpose(e.target.value)} /></label></p>}
      <label htmlFor="memory-content">记忆内容</label>
      <textarea id="memory-content" required maxLength={5000} rows={5} style={{ display: 'block', width: '100%' }} value={draft} onChange={e => setDraft(e.target.value)} />
      {!editing && <p>点击保存会将这条记忆保留至你撤回；它不会授予智能体业务操作权限。</p>}
      <button disabled={disabled || !draft.trim()} type="submit">{editing ? '保存更正' : '保存长期记忆'}</button>{' '}
      {editing && <button type="button" disabled={view.busy} onClick={resetDraft}>取消编辑</button>}
    </form>}
    <details style={{ marginTop: 24 }} open={!!view.note}>
      <summary>Markdown 笔记交换</summary>
      <p>可以用 Obsidian 等编辑器编辑导出的笔记。保留文件头，只修改正文，再粘贴回导；此处尚不自动同步文件夹。</p>
      <label htmlFor="memory-markdown">笔记全文</label>
      <textarea id="memory-markdown" rows={10} style={{ display: 'block', width: '100%' }} value={markdown} onChange={e => setMarkdown(e.target.value)} readOnly={!canWrite} />
      <button type="button" disabled={!view.note || view.busy} onClick={download}>下载原笔记</button>{' '}
      {canWrite && <button type="button" disabled={disabled || !markdown} onClick={() => { void controller.importNote(markdown); }}>回导编辑后的笔记</button>}
    </details>
    <p>撤回后助理不再检索这条记忆；已经下载的文件需另行处理。库存、价格等实时数据以业务系统查询为准。</p>
  </section>;
}
