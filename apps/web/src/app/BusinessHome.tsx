import { useState } from 'react';
import { useCapabilities } from '../lib/auth/capabilities';
import { canUseQuoteWorkspace } from '../lib/auth/businessPermissions';
import { getPermittedWorkObjects } from '../shell/workObjects';
import { navigate } from './router';
export function BusinessHome() {
  const {capabilities}=useCapabilities();
  const [search, setSearch] = useState('');
  const [activeGroup, setActiveGroup] = useState('全部');
  const modules=getPermittedWorkObjects(capabilities).filter(m=>!['/','/assistant','/catalog','/pi-trial','/pi-probe'].includes(m.path));
  const groups=[...new Set(modules.map(m=>m.category))];
  const canOpenDrafts=canUseQuoteWorkspace(capabilities);
  const q = search.trim().toLowerCase();
  const effectiveGroup = activeGroup === '全部' || groups.includes(activeGroup as (typeof groups)[number]) ? activeGroup : '全部';
  const filteredModules = modules.filter(m =>
    (effectiveGroup === '全部' || m.category === effectiveGroup) &&
    (!q || [m.navLabel, m.title, m.summary, m.category].some(s => s.toLowerCase().includes(q))),
  );
  const visibleGroups = [...new Set(filteredModules.map(m => m.category))];
  return <div className="od-business-home" data-business-home="true">
    <header>
      <p>人工业务 · 已开放 {modules.length} 项模块</p>
      <h1>选择你要处理的工作</h1>
      <span>这里直接打开已授权的业务页面，独立于 AI 推理——即使模型离线，手工查货、草稿与复核仍可正常使用。</span>
    </header>
    {modules.length > 0 && (
      <div className="od-business-toolbar" aria-label="模块筛选">
        <input
          type="search"
          className="od-business-search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="按名称或用途筛选业务模块…"
          aria-label="筛选业务模块"
        />
        <div className="od-filter-pills" role="group" aria-label="按业务分类筛选">
          {['全部', ...groups].map(group => (
            <button
              key={group}
              type="button"
              className={`od-filter-pill${effectiveGroup === group ? ' is-active' : ''}`}
              aria-pressed={effectiveGroup === group}
              onClick={() => setActiveGroup(group)}
            >
              {group}
            </button>
          ))}
        </div>
      </div>
    )}
    {visibleGroups.map(group=><section key={group}><h2>{group}</h2><div className="od-business-grid">{filteredModules.filter(m=>m.category===group).map(m=><a key={m.path} href={m.path} onClick={e=>{e.preventDefault();navigate(m.path);}}><div className="od-card-head"><strong>{m.navLabel}</strong><small className="od-tier-badge">{m.tier}</small></div><span>{m.summary}</span><small className="od-card-foot">{m.offlineAvailable ? '离线可用' : '需在线服务'} · 进入模块 →</small></a>)}</div></section>)}
    {modules.length > 0 && filteredModules.length === 0 && <p role="status">没有匹配“{search}”的已开放业务模块。</p>}
    {!modules.length && <p role="status">当前身份没有其他已开放的人工业务入口。</p>}
    {canOpenDrafts && <section><h2>人工草稿</h2><p>独立工作区，与助理事项尚未自动绑定。保存草稿不等于提交、解锁或写入ERP。</p><button type="button" onClick={()=>navigate('/workspace')}>打开人工草稿工作区</button></section>}
  </div>;
}
