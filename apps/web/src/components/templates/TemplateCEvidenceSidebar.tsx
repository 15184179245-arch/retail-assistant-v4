import type { ReactNode } from 'react';
import { Button } from '../Button';
import { PageHeader } from '../PageHeader';
import { StatusBadge } from '../StatusBadge';
import type { StatusKind } from '../StatusBadge';
import { SixStateView } from '../primitives/SixStateView';
import type { SixState, SixStateError } from '../primitives/SixStateView';
import styles from './templates.module.css';

export interface ProvenanceSummary {
  source: string;
  observedAt?: string | null;
  verifiedBy?: string | null;
  statusKind: StatusKind;
  version?: number | string;
  receiptRef?: string | null;
}

export interface EvidenceCardItem {
  id: string;
  title: string;
  detail: string;
  timestamp?: string;
  statusKind?: StatusKind;
}

export interface AuditTrailEntry {
  id: string;
  actorType: 'human' | 'agent' | 'system';
  actorName: string;
  summary: string;
  timestamp: string;
  undoable?: boolean;
  onUndo?: () => void;
}

export interface TemplateCEvidenceSidebarProps {
  title: string;
  eyebrow?: string;
  actions?: ReactNode;
  provenance: ProvenanceSummary;
  evidenceItems?: readonly EvidenceCardItem[];
  auditTrail?: readonly AuditTrailEntry[];
  onUndoLatest?: () => void;
  undoLatestLabel?: string;
  undoConflictNotice?: string | null;
  onDismissConflictNotice?: () => void;
  state?: SixState;
  error?: SixStateError | null;
  onRetry?: () => void;
  children: ReactNode;
  className?: string;
}

export function TemplateCEvidenceSidebar({
  title,
  eyebrow = 'TEMPLATE C · 对象详情与证据审计侧栏',
  actions,
  provenance,
  evidenceItems = [],
  auditTrail = [],
  onUndoLatest,
  undoLatestLabel = '撤销本轮 AI 修改',
  undoConflictNotice,
  onDismissConflictNotice,
  state = 'ready',
  error,
  onRetry,
  children,
  className,
}: TemplateCEvidenceSidebarProps) {
  return (
    <section
      data-template="C"
      className={[styles.templateRoot, className].filter(Boolean).join(' ')}
    >
      <PageHeader
        title={title}
        eyebrow={eyebrow}
        source={provenance.source}
        timestamp={provenance.observedAt ?? undefined}
        statusKind={provenance.statusKind}
        actions={actions}
      />

      {undoConflictNotice ? (
        <div
          className={styles.conflictNotice}
          role="alert"
          data-undo-conflict-notice="true"
        >
          <span>{undoConflictNotice}</span>
          {onDismissConflictNotice ? (
            <Button
              size="sm"
              variant="secondary"
              onClick={onDismissConflictNotice}
            >
              知道了
            </Button>
          ) : null}
        </div>
      ) : null}

      <SixStateView
        state={state}
        statusKind={provenance.statusKind}
        source={provenance.source}
        timestamp={provenance.observedAt ?? undefined}
        error={error}
        onRetry={onRetry}
      >
        <div className={styles.evidenceGrid}>
          <div className={styles.evidenceMain}>{children}</div>

          <aside
            className={styles.evidenceSidebar}
            aria-label="来源、证据与操作审计侧栏"
            data-evidence-sidebar="true"
          >
            <div className={styles.sideBox}>
              <p className={styles.sideEyebrow}>来源与审计上下文（直显）</p>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
                <StatusBadge kind={provenance.statusKind} source={provenance.source} />
                {provenance.version !== undefined ? (
                  <code>v{String(provenance.version)}</code>
                ) : null}
              </div>
              <div>
                <strong>数据来源：</strong>
                <span>{provenance.source}</span>
              </div>
              {provenance.observedAt ? (
                <div>
                  <strong>同步/观测时点：</strong>
                  <span>{provenance.observedAt}</span>
                </div>
              ) : null}
              <div>
                <strong>核验人：</strong>
                <span>{provenance.verifiedBy ?? '尚无人工核验人'}</span>
              </div>
              <div>
                <strong>正式回执：</strong>
                <span>{provenance.receiptRef ?? '尚未正式提交（草稿层）'}</span>
              </div>
            </div>

            {evidenceItems.length > 0 ? (
              <div className={styles.sideBox}>
                <p className={styles.sideEyebrow}>证据对照</p>
                {evidenceItems.map((item) => (
                  <div key={item.id} className={styles.auditItem}>
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        gap: '6px',
                        alignItems: 'center',
                        flexWrap: 'wrap',
                      }}
                    >
                      <strong>{item.title}</strong>
                      {item.statusKind ? <StatusBadge kind={item.statusKind} /> : null}
                    </div>
                    <span>{item.detail}</span>
                    {item.timestamp ? (
                      <span style={{ color: 'var(--muted, #65736f)' }}>
                        时点：{item.timestamp}
                      </span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            <div className={styles.sideBox}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: '8px',
                  flexWrap: 'wrap',
                }}
              >
                <p className={styles.sideEyebrow}>操作历史与撤销</p>
                {onUndoLatest ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={onUndoLatest}
                    data-undo-latest="true"
                  >
                    {undoLatestLabel}
                  </Button>
                ) : null}
              </div>
              {auditTrail.length === 0 ? (
                <span style={{ color: 'var(--muted, #65736f)' }}>
                  暂无操作记录
                </span>
              ) : (
                auditTrail.map((entry) => (
                  <div
                    key={entry.id}
                    className={[
                      styles.auditItem,
                      entry.actorType === 'agent' ? styles.auditItemAgent : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    data-audit-actor={entry.actorType}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        gap: '6px',
                        alignItems: 'center',
                      }}
                    >
                      <strong>
                        {entry.actorType === 'agent' ? '✦ AI' : entry.actorName}
                      </strong>
                      <span style={{ color: 'var(--muted, #65736f)' }}>
                        {entry.timestamp}
                      </span>
                    </div>
                    <span>{entry.summary}</span>
                    {entry.undoable && entry.onUndo ? (
                      <div style={{ marginTop: '4px' }}>
                        <Button size="sm" variant="ghost" onClick={entry.onUndo}>
                          撤销此步
                        </Button>
                      </div>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          </aside>
        </div>
      </SixStateView>
    </section>
  );
}
