import { useState } from 'react';
import { Button } from '../components/Button';
import { StatusBadge } from '../components/StatusBadge';
import type { StatusKind } from '../components/StatusBadge';
import {
  validateUiBlock,
} from './schema';
import type {
  BlockRef,
  FormFieldSpec,
  ResolvedRefEntry,
  ValidatedUiBlock,
} from './schema';
import styles from './blocks.module.css';

export interface BlockRendererProps {
  block: unknown;
  streaming?: boolean;
  onPinToWorkbench?: (block: ValidatedUiBlock) => void;
  onOpenArtifact?: (artifactId: string, artifactKind?: string) => void;
  onTriggerActionCard?: (block: ValidatedUiBlock) => void;
  onSubmitForm?: (
    formId: string,
    values: Record<string, string | boolean>,
  ) => void;
  onSelectRef?: (ref: BlockRef) => void;
  className?: string;
}

const BLOCK_TYPE_LABELS: Record<ValidatedUiBlock['type'], { label: string; tier: 'L0' | 'L1' | 'L2' }> = {
  product_card: { label: '单品详情卡', tier: 'L0' },
  product_list: { label: '商品检索列表', tier: 'L0' },
  compare_table: { label: '多品对比表', tier: 'L0' },
  quote_draft: { label: '报价草稿块', tier: 'L1' },
  purchase_draft: { label: '采购计划草稿块', tier: 'L1' },
  stock_location: { label: '库位分布卡', tier: 'L0' },
  work_item_list: { label: '待办工作项清单', tier: 'L0' },
  evidence_panel: { label: '证据与回执对照', tier: 'L0' },
  action_card: { label: 'L2 待确认动作卡', tier: 'L2' },
  form_request: { label: '缺参追问表单', tier: 'L0' },
  artifact_link: { label: '工作成果焦点卡', tier: 'L1' },
};

export function BlockSkeleton({ title = '正在解析服务端 refs 并校验协议…' }: { title?: string }) {
  return (
    <article
      className={styles.blockCard}
      aria-busy="true"
      role="status"
      data-block-skeleton="true"
    >
      <div className={styles.blockHeader}>
        <div className={styles.blockTitleGroup}>
          <span className={[styles.tierTag, styles.tierL0].join(' ')}>
            流式生成中
          </span>
          <h4 className={styles.blockTitle}>{title}</h4>
        </div>
        <StatusBadge kind="waiting" source="流式骨架占位" />
      </div>
      <div className={styles.blockBody}>
        <div className={[styles.skeletonLine, styles.skeletonShort].join(' ')} />
        <div className={styles.skeletonLine} />
        <div className={[styles.skeletonLine, styles.skeletonMid].join(' ')} />
      </div>
    </article>
  );
}

export function BlockFallbackCard({
  reason,
  fallbackText,
}: {
  reason: string;
  fallbackText: string;
}) {
  return (
    <article
      className={[styles.blockCard, styles.blockCardFallback].join(' ')}
      data-block-fallback="true"
      role="region"
      aria-label="生成式块已安全降级"
    >
      <div className={styles.blockHeader}>
        <div className={styles.blockTitleGroup}>
          <h4 className={styles.blockTitle}>块校验未通过：已安全降级为纯文本</h4>
        </div>
        <StatusBadge kind="unverified" source="安全降级" />
      </div>
      <div className={styles.blockBody}>
        <div>原因：{reason}</div>
        <pre className={styles.plainTextPre}>{fallbackText}</pre>
      </div>
    </article>
  );
}

function getResolvedItems(block: ValidatedUiBlock): ResolvedRefEntry[] {
  if (Array.isArray(block.resolved_refs)) {
    return block.resolved_refs;
  }
  if (block.resolved_refs && typeof block.resolved_refs === 'object') {
    return Object.entries(block.resolved_refs).map(([key, val]) => ({
      id: val.id ?? key,
      ...val,
    }));
  }
  if (block.resolved_data && Array.isArray(block.resolved_data.items)) {
    return block.resolved_data.items as ResolvedRefEntry[];
  }
  if (block.resolved_data && typeof block.resolved_data === 'object') {
    return [block.resolved_data as ResolvedRefEntry];
  }
  return [];
}

function resolveBlockStatusKind(block: ValidatedUiBlock, items: ResolvedRefEntry[]): StatusKind {
  if (block.provenance?.stale_refs && block.provenance.stale_refs.length > 0) {
    return 'unverified';
  }
  if (items.some((item) => item.stale === true || item.verified === false)) {
    return 'unverified';
  }
  if (block.status_kind) {
    return block.status_kind;
  }
  if (block.provenance?.status_kind) {
    return block.provenance.status_kind;
  }
  if (block.type === 'action_card') {
    return 'waiting';
  }
  if (block.type === 'quote_draft' || block.type === 'purchase_draft') {
    return 'pending_sync';
  }
  if (items.length > 0) {
    return 'fact';
  }
  return 'unverified';
}

function formatRefValue(item: ResolvedRefEntry, field: 'price' | 'stock' | 'amount'): string {
  const val =
    field === 'price'
      ? item.price ?? item.unit_price
      : field === 'stock'
        ? item.stock ?? item.stock_qty
        : item.amount;
  if (val === undefined || val === null) {
    return '已按权限隐藏或待服务端解析';
  }
  return String(val);
}

function FormRequestBody({
  fields,
  formId,
  submitLabel,
  onSubmitForm,
}: {
  fields: FormFieldSpec[];
  formId: string;
  submitLabel: string;
  onSubmitForm?: (formId: string, values: Record<string, string | boolean>) => void;
}) {
  const [values, setValues] = useState<Record<string, string | boolean>>(() => {
    const init: Record<string, string | boolean> = {};
    for (const f of fields) {
      init[f.name] = f.field_type === 'boolean' ? false : '';
    }
    return init;
  });

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmitForm?.(formId, values);
      }}
      style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}
    >
      {fields.map((field) => {
        const inputId = `form-${formId}-${field.name}`;
        return (
          <div key={field.name} className={styles.formRequestField}>
            <label htmlFor={inputId} style={{ fontWeight: 600, fontSize: '12px' }}>
              {field.label}
              {field.required ? ' *' : ''}
            </label>
            {field.field_type === 'select' ? (
              <select
                id={inputId}
                className={styles.formRequestInput}
                value={String(values[field.name] ?? '')}
                required={field.required}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [field.name]: e.target.value }))
                }
              >
                <option value="">请选择…</option>
                {(field.options ?? []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : field.field_type === 'boolean' ? (
              <input
                id={inputId}
                type="checkbox"
                checked={Boolean(values[field.name])}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [field.name]: e.target.checked }))
                }
              />
            ) : (
              <input
                id={inputId}
                type={
                  field.field_type === 'number'
                    ? 'number'
                    : field.field_type === 'date'
                      ? 'date'
                      : 'text'
                }
                className={styles.formRequestInput}
                placeholder={field.placeholder}
                required={field.required}
                value={String(values[field.name] ?? '')}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [field.name]: e.target.value }))
                }
              />
            )}
          </div>
        );
      })}
      <div>
        <Button type="submit" size="sm" variant="primary">
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

export function BlockRenderer({
  block,
  streaming = false,
  onPinToWorkbench,
  onOpenArtifact,
  onTriggerActionCard,
  onSubmitForm,
  onSelectRef,
  className,
}: BlockRendererProps) {
  const isStreaming =
    streaming ||
    (typeof block === 'object' &&
      block !== null &&
      (block as { streaming?: unknown }).streaming === true);

  if (isStreaming) {
    return <BlockSkeleton />;
  }

  const parsed = validateUiBlock(block);
  if (!parsed.success) {
    return (
      <BlockFallbackCard
        reason={parsed.error.message}
        fallbackText={parsed.error.fallbackText}
      />
    );
  }

  const valid = parsed.data;
  const meta = BLOCK_TYPE_LABELS[valid.type];
  const resolvedItems = getResolvedItems(valid);
  const statusKind = resolveBlockStatusKind(valid, resolvedItems);
  const sourceLabel =
    valid.provenance?.source ?? resolvedItems[0]?.source ?? '服务端实体引用';
  const syncedAt = valid.provenance?.synced_at ?? resolvedItems[0]?.synced_at;
  const verifiedBy = valid.provenance?.verified_by;
  const props = valid.props;
  const blockTitle =
    typeof props.title === 'string' && props.title.trim().length > 0
      ? props.title
      : meta.label;

  const tierClass =
    meta.tier === 'L2'
      ? styles.tierL2
      : meta.tier === 'L1'
        ? styles.tierL1
        : styles.tierL0;

  return (
    <article
      data-block-type={valid.type}
      data-block-version={valid.version}
      data-block-tier={meta.tier}
      className={[
        styles.blockCard,
        valid.type === 'action_card' ? styles.blockCardAction : '',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <header className={styles.blockHeader}>
        <div className={styles.blockTitleGroup}>
          <div className={styles.blockTierRow}>
            <span className={[styles.tierTag, tierClass].join(' ')}>
              {meta.tier} · {meta.label}
            </span>
            <StatusBadge kind={statusKind} source={sourceLabel} />
          </div>
          <h4 className={styles.blockTitle}>{blockTitle}</h4>
        </div>
      </header>

      <div className={styles.provenanceBar}>
        <span>来源：{sourceLabel}</span>
        {syncedAt ? <span>时点：{syncedAt}</span> : null}
        {verifiedBy ? <span>核验人：{verifiedBy}</span> : null}
        <span>引用数：{valid.refs.length}</span>
      </div>

      <div className={styles.blockBody}>
        {valid.type === 'product_card' ? (
          <div className={styles.kvGrid}>
            <div className={styles.kvCell}>
              <span className={styles.kvLabel}>商品标识</span>
              <span className={styles.kvValue}>
                {resolvedItems[0]?.name ?? valid.refs[0]?.label ?? valid.refs[0]?.id}
              </span>
            </div>
            <div className={styles.kvCell}>
              <span className={styles.kvLabel}>参考价（服务端注入）</span>
              <span className={styles.kvValue}>
                {resolvedItems[0] ? formatRefValue(resolvedItems[0], 'price') : '待服务端注入'}
              </span>
            </div>
            <div className={styles.kvCell}>
              <span className={styles.kvLabel}>账面库存（服务端注入）</span>
              <span className={styles.kvValue}>
                {resolvedItems[0] ? formatRefValue(resolvedItems[0], 'stock') : '待服务端注入'}
              </span>
            </div>
            {resolvedItems[0]?.location ? (
              <div className={styles.kvCell}>
                <span className={styles.kvLabel}>库位</span>
                <span className={styles.kvValue}>{String(resolvedItems[0].location)}</span>
              </div>
            ) : null}
          </div>
        ) : null}

        {valid.type === 'product_list' ? (
          <ul className={styles.itemList}>
            {(resolvedItems.length > 0 ? resolvedItems : valid.refs).map((item, idx) => {
              const refObj = valid.refs[idx];
              const label =
                ('name' in item && item.name ? String(item.name) : undefined) ??
                refObj?.label ??
                ('id' in item && item.id ? String(item.id) : `商品 ${idx + 1}`);
              return (
                <li key={`${label}-${idx}`} className={styles.itemRow}>
                  <div>
                    <strong>{label}</strong>
                    {'sku' in item && item.sku ? (
                      <div style={{ fontSize: '12px', color: 'var(--muted, #65736f)' }}>
                        编码：{String(item.sku)}
                      </div>
                    ) : null}
                  </div>
                  {'price' in item || 'stock' in item ? (
                    <div style={{ fontSize: '12px' }}>
                      参考价：{formatRefValue(item as ResolvedRefEntry, 'price')} · 库存：
                      {formatRefValue(item as ResolvedRefEntry, 'stock')}
                    </div>
                  ) : null}
                  {onSelectRef && refObj ? (
                    <Button size="sm" variant="secondary" onClick={() => onSelectRef(refObj)}>
                      选择
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}

        {valid.type === 'compare_table' ? (
          <div className={styles.tableWrap}>
            <table className={styles.blockTable}>
              <thead>
                <tr>
                  {(props.columns as string[]).map((col) => (
                    <th key={col}>{col}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(resolvedItems.length > 0 ? resolvedItems : valid.refs).map((row, idx) => (
                  <tr key={idx}>
                    {(props.columns as string[]).map((col, colIdx) => {
                      const record = row as Record<string, unknown>;
                      const val =
                        record[col] ??
                        (colIdx === 0
                          ? record.name ?? record.label ?? record.id
                          : '—');
                      return <td key={col}>{String(val ?? '—')}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        {valid.type === 'quote_draft' || valid.type === 'purchase_draft' ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {valid.type === 'quote_draft' ? (
              <div style={{ fontSize: '12px', color: 'var(--muted, #65736f)' }}>
                stage: <code>{String(props.stage ?? 'draft')}</code> · status:{' '}
                <code>{String(props.status ?? 'pending')}</code>
                {props.customer_label ? ` · 客户：${String(props.customer_label)}` : ''}
              </div>
            ) : (
              <div style={{ fontSize: '12px', color: 'var(--muted, #65736f)' }}>
                草稿意向（严禁直接生成正式采购单）
                {props.supplier_label ? ` · 供应商：${String(props.supplier_label)}` : ''}
              </div>
            )}
            {Array.isArray(props.line_refs) && props.line_refs.length > 0 ? (
              <ul className={styles.itemList}>
                {(props.line_refs as Array<{ ref_id: string; qty: number; unit?: string; note?: string }>).map(
                  (line, idx) => (
                    <li key={`${line.ref_id}-${idx}`} className={styles.itemRow}>
                      <span>
                        <strong>{line.ref_id}</strong> × {line.qty} {line.unit ?? ''}
                      </span>
                      {line.note ? <span>{line.note}</span> : null}
                    </li>
                  ),
                )}
              </ul>
            ) : null}
            {typeof props.artifact_id === 'string' && onOpenArtifact ? (
              <div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => onOpenArtifact(String(props.artifact_id), valid.type)}
                >
                  在工作台打开草稿
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}

        {valid.type === 'stock_location' ? (
          <ul className={styles.itemList}>
            {(resolvedItems.length > 0 ? resolvedItems : valid.refs).map((loc, idx) => (
              <li key={idx} className={styles.itemRow}>
                <span>
                  库位：
                  <strong>
                    {('location' in loc && loc.location ? String(loc.location) : undefined) ??
                      ('label' in loc && loc.label ? String(loc.label) : undefined) ??
                      ('id' in loc ? String(loc.id) : `库位 ${idx + 1}`)}
                  </strong>
                </span>
                {'stock' in loc || 'stock_qty' in loc ? (
                  <span>账面：{formatRefValue(loc as ResolvedRefEntry, 'stock')}</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}

        {valid.type === 'work_item_list' ? (
          <ul className={styles.itemList}>
            {(resolvedItems.length > 0 ? resolvedItems : valid.refs).map((item, idx) => {
              const refObj = valid.refs[idx];
              const label =
                ('name' in item && item.name ? String(item.name) : undefined) ??
                refObj?.label ??
                ('id' in item ? String(item.id) : `工作项 ${idx + 1}`);
              return (
                <li key={idx} className={styles.itemRow}>
                  <span>{label}</span>
                  {onSelectRef && refObj ? (
                    <Button size="sm" variant="secondary" onClick={() => onSelectRef(refObj)}>
                      打开处理
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}

        {valid.type === 'evidence_panel' ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {typeof props.summary === 'string' ? <p style={{ margin: 0 }}>{props.summary}</p> : null}
            <ul className={styles.itemList}>
              {valid.refs.map((ref, idx) => (
                <li key={`${ref.id}-${idx}`} className={styles.itemRow}>
                  <span>
                    {ref.kind}: <code>{ref.id}</code>
                  </span>
                  {ref.label ? <span>{ref.label}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {valid.type === 'action_card' ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {typeof props.description === 'string' ? (
              <p style={{ margin: 0 }}>{props.description}</p>
            ) : null}
            <div style={{ fontSize: '12px', color: 'var(--muted, #65736f)' }}>
              动作标识：<code>{String(props.action_key)}</code>（须经人工 L2 CommandConfirm 确认后执行）
            </div>
            <div>
              <Button
                size="sm"
                variant="primary"
                onClick={() => onTriggerActionCard?.(valid)}
                data-trigger-action-card="true"
              >
                打开 L2 命令确认
              </Button>
            </div>
          </div>
        ) : null}

        {valid.type === 'form_request' ? (
          <FormRequestBody
            formId={String(props.form_id)}
            fields={props.fields as FormFieldSpec[]}
            submitLabel={
              typeof props.submit_label === 'string' && props.submit_label.trim()
                ? props.submit_label
                : '提交补充参数'
            }
            onSubmitForm={onSubmitForm}
          />
        ) : null}

        {valid.type === 'artifact_link' ? (
          <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px' }}>
            <span>
              目标对象：<code>{String(props.artifact_kind)}</code> /{' '}
              <code>{String(props.artifact_id)}</code>
            </span>
            <Button
              size="sm"
              variant="primary"
              onClick={() =>
                onOpenArtifact?.(
                  String(props.artifact_id),
                  String(props.artifact_kind),
                )
              }
              data-open-artifact-link={String(props.artifact_id)}
            >
              {typeof props.action_label === 'string' && props.action_label.trim()
                ? props.action_label
                : '在工作台打开聚焦'}
            </Button>
          </div>
        ) : null}
      </div>

      {onPinToWorkbench ? (
        <footer className={styles.blockActions}>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => onPinToWorkbench(valid)}
            data-pin-to-workbench="true"
          >
            固定到工作台
          </Button>
        </footer>
      ) : null}
    </article>
  );
}
