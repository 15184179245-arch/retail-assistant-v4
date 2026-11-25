import { useState } from 'react';
import { StatusBadge } from '../StatusBadge';
import { Tabs } from '../primitives/Tabs';
import { TemplateAListDetail } from './TemplateAListDetail';
import { TemplateBBatchReview } from './TemplateBBatchReview';
import { TemplateCEvidenceSidebar } from './TemplateCEvidenceSidebar';
import { TemplateDCommandConfirm } from './TemplateDCommandConfirm';

export const TEMPLATE_SYNTHETIC_DEMO_DATA = {
  workItems: [
    {
      id: 'SYN-EXC-001',
      title: '到货短少差异 #SYN-001',
      domain: '采购到货',
      source: '合成演示数据',
      detail: 'DN25 热镀锌内丝弯头实收 48 个，账面 50 个，差异 -2 个待复核。',
      statusKind: 'unverified' as const,
    },
    {
      id: 'SYN-KNW-002',
      title: '别名修订复核 #SYN-002',
      domain: '知识库',
      source: '合成演示数据',
      detail: '拟新增口语别名“6分镀锌弯头”，待真人审核确认。',
      statusKind: 'suggestion' as const,
    },
  ],
  stocktakeRows: [
    {
      id: 'SYN-STK-1',
      sku: 'GWP-DN25-90',
      name: 'DN25 热镀锌内丝弯头（合成样本）',
      location: 'A区-03架-2层',
      bookQty: 184,
      countedQty: 180,
    },
    {
      id: 'SYN-STK-2',
      sku: 'PVC-DN50-TEE',
      name: 'DN50 PVC 排水三通（合成样本）',
      location: 'B区-01架-1层',
      bookQty: 60,
      countedQty: 60,
    },
  ],
};

/**
 * Dev-only synthetic showcase for Templates A–D.
 * Always marked with StatusBadge kind="development" so synthetic data never masquerades as production.
 */
export function TemplateSyntheticDemo() {
  const [selectedWorkId, setSelectedWorkId] = useState<string>(
    TEMPLATE_SYNTHETIC_DEMO_DATA.workItems[0].id,
  );
  const [selectedRows, setSelectedRows] = useState<string[]>(['SYN-STK-1']);

  return (
    <section data-dev-template-demo="true" style={{ display: 'grid', gap: '16px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <StatusBadge kind="development" source="合成演示数据（非营业环境）" />
        <span style={{ fontSize: '13px', color: 'var(--muted, #65736f)' }}>
          WEB-011 模板 A–D 合成数据预览
        </span>
      </div>

      <Tabs
        ariaLabel="模板 A 到 D 合成演示切换"
        items={[
          {
            id: 'template-a',
            label: '模板 A · 列表详情',
            content: (
              <TemplateAListDetail
                title="异常与待办工作台（合成）"
                source="合成演示数据"
                statusKind="development"
                items={TEMPLATE_SYNTHETIC_DEMO_DATA.workItems}
                selectedId={selectedWorkId}
                getItemId={(item) => item.id}
                onSelectItem={(item) => setSelectedWorkId(item.id)}
                renderListItem={(item) => (
                  <>
                    <strong>{item.title}</strong>
                    <StatusBadge kind={item.statusKind} source={item.source} />
                  </>
                )}
                renderDetail={(item) =>
                  item ? (
                    <div style={{ display: 'grid', gap: '8px' }}>
                      <h4 style={{ margin: 0 }}>{item.title}</h4>
                      <p style={{ margin: 0 }}>{item.detail}</p>
                    </div>
                  ) : null
                }
              />
            ),
          },
          {
            id: 'template-b',
            label: '模板 B · 批量复核',
            content: (
              <TemplateBBatchReview
                title="部分库位盘点差异复核（合成）"
                source="合成演示数据"
                statusKind="unverified"
                columns={[
                  { key: 'sku', header: '编码' },
                  { key: 'name', header: '商品名称' },
                  { key: 'location', header: '库位' },
                  { key: 'bookQty', header: '账面', align: 'right' },
                  { key: 'countedQty', header: '实盘', align: 'right' },
                ]}
                rows={TEMPLATE_SYNTHETIC_DEMO_DATA.stocktakeRows}
                getRowId={(r) => r.id}
                selectedIds={selectedRows}
                onSelectionChange={setSelectedRows}
                onBatchAction={() => {}}
              />
            ),
          },
          {
            id: 'template-c',
            label: '模板 C · 证据侧栏',
            content: (
              <TemplateCEvidenceSidebar
                title="报价草稿 #SYN-QT-012"
                provenance={{
                  source: '合成演示快照',
                  observedAt: '2026-11-16 10:15',
                  verifiedBy: null,
                  statusKind: 'pending_sync',
                  version: 3,
                }}
                evidenceItems={[
                  {
                    id: 'ev-1',
                    title: '管家婆快照对照',
                    detail: '参考售价 ¥14.50，账面 184 个（合成）。',
                    statusKind: 'fact',
                  },
                ]}
                auditTrail={[
                  {
                    id: 'op-1',
                    actorType: 'agent',
                    actorName: 'Pi 助理',
                    summary: '调整数量为 20 个',
                    timestamp: '10:18',
                  },
                ]}
              >
                <div>报价草稿主编辑区（合成演示）</div>
              </TemplateCEvidenceSidebar>
            ),
          },
          {
            id: 'template-d',
            label: '模板 D · 命令确认',
            content: (
              <TemplateDCommandConfirm
                title="创建销售交接单（合成）"
                commandKey="sales.create_handoff"
                whatHappens="基于合成草稿创建待收银核对的交接单。"
                impactScope="stage 由 draft 转为 ready；status 保持 pending。"
                whatItWontDo="不直接修改管家婆账面库存、正式售价或自动收款。"
                approver="现场负责人"
                idempotencyKey="syn-idemp-demo-001"
                baseVersion={3}
                onConfirm={() => ({ status: 'verified', receiptRef: 'SYN-RCP-1' })}
              />
            ),
          },
        ]}
      />
    </section>
  );
}
