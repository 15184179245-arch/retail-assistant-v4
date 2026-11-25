import type { ReactNode } from "react";
import type { Session } from "../lib/auth/session";
import { businessPermission } from "../lib/auth/businessPermissions";
import { StatusBadge } from "../components/StatusBadge";
import { getActiveThread, isVerifiedArtifactSummaryCurrent, openWorkbenchTab, setLeftSection, useVerifiedArtifactSummaries, useWorkspaceState } from "../workspace/store";
import { getPermittedWorkObjects, openWorkObjectRouteTab } from "./workObjects";
import styles from "../styles/workbench.module.css";

export interface TodayViewProps { session: Session; capabilities: string[]; homeContent?: ReactNode; onCreateQuoteDraft: () => void }

/** A projection of loaded workspace facts, not invented store-wide operational totals. */
export function TodayView({ session, capabilities, onCreateQuoteDraft }: TodayViewProps) {
  const ws = useWorkspaceState(), thread = getActiveThread();
  const artifacts = Object.values(useVerifiedArtifactSummaries(ws)), permitted = getPermittedWorkObjects(capabilities);
  const locked = artifacts.reduce((total, artifact) => total + artifact.lockedCount, 0);
  const canDraft = capabilities.includes(businessPermission.sales.write);
  return <section className={styles.homeView} data-today-view="true">
    <header className={styles.homeHeading}><div><p>工作台 / 今日</p><h2>{session.username}，人工工作区</h2><span>在这里独立查看和核对人工草稿。此工作区尚未自动绑定助理事项；保存不代表业务提交。</span></div>
      {canDraft && <button type="button" className={styles.primaryBtn} onClick={onCreateQuoteDraft}>＋ 新建报价草稿</button>}</header>
    <div className={styles.summaryGrid} aria-label="本页已加载工作区摘要">
      {[{label:"已核对成果",value:artifacts.length,note:"当前授权已核对"},{label:"人工锁定字段",value:locked,note:"已核对成果中的保护字段"},{label:"可用业务入口",value:permitted.length,note:"按当前员工能力显示"},{label:"工作区保存",value:thread.version > 0 ? `v${thread.version}` : "未保存",note:"不是业务提交或执行结果"}].map(item => <article key={item.label}><span>{item.label}</span><strong>{item.value}</strong><small>{item.note}</small></article>)}
    </div>
    <section className={styles.homeSection}><div className={styles.homeSectionHeading}><h3>工作成果</h3><span>仅显示当前授权已核对的草稿，不代表全店待办总数</span></div>
      {artifacts.length ? <div className={styles.resultList}>{artifacts.map(artifact => <article key={artifact.id}><div><strong>{artifact.title}</strong><small>{artifact.kind} · v{artifact.version} · {artifact.lockedCount} 个人工锁</small></div><StatusBadge kind={artifact.status === "unverified" ? "unverified" : "fact"} source="工作区保存记录"/><button type="button" className={styles.secondaryBtn} onClick={() => { if (isVerifiedArtifactSummaryCurrent(artifact)) openWorkbenchTab({
        id:`artifact:${artifact.id}`,kind:"artifact",title:artifact.title,objectKind:artifact.kind,objectId:artifact.id,artifactId:artifact.id,closable:true,
      }); }}>打开成果</button></article>)}</div>
        : <div className={styles.homeEmpty}><span aria-hidden="true">▤</span><strong>成果会留在这里</strong><p>从对象库打开一项业务，或明确新建工作区草稿。尚未执行的事项不会伪装成已完成成果。</p></div>}
    </section>
    <section className={styles.homeSection}><div className={styles.homeSectionHeading}><h3>从业务对象继续</h3><button type="button" onClick={() => setLeftSection("objects")}>查看全部对象 →</button></div>
      <div className={styles.quickObjectGrid}>{permitted.filter(item => !['/assistant','/memory'].includes(item.path)).slice(0,6).map(item => <button type="button" key={item.path} onClick={() => {openWorkObjectRouteTab(item.path);}}><span>{item.category}</span><strong>{item.navLabel}</strong><small>{item.tier} · {item.summary}</small></button>)}</div>
    </section>
  </section>;
}
