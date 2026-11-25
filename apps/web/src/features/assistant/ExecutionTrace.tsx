import type { WorkProgress } from './workProgress';
function RecordTime({ value }: { value?: string }) {
  return value ? <time dateTime={value}>{new Date(value).toLocaleString('zh-CN', { hour12: false })}</time> : <>未记录</>;
}
/** Receives only the allowlisted parseProgress projection, never raw receipts. */
export function ExecutionTrace({ progress, workId, runId }: { progress: WorkProgress; workId: string; runId: string | null }) {
  return <details className="assistant-execution-details assistant-trace">
    <summary><span>执行记录</span><span className="assistant-trace-count">{progress.steps.length} 步</span></summary>
    <div className="assistant-trace-body">
      <p className="assistant-trace-note">以下是已保存的执行事实，不是模型私有思考。步骤完成不等于业务目标已验收。</p>
      {progress.uncertain && <p className="assistant-trace-warning" role="note">存在待核对的结果。查看或刷新只读取原记录，不会重新执行。</p>}
      <dl className="assistant-trace-availability">
        <dt>公开计划 / 决策摘要</dt><dd>未记录。不会从工具顺序推测模型的理由。</dd>
        <dt>工具参数与结果正文</dt><dd>{progress.traceState === 'available'
          ? '以下仅为当前权限核验后的有限字段。自由文本未捕获，不是完整原始流，也不能还原正文。'
          : progress.traceState === 'withheld' ? '记录绑定或当前来源无法核验，暂不展示。不会补造或重新执行。'
          : '当前回执未记录可展示内容；有摘要指纹时可核对记录，不能还原正文。'}</dd>
      </dl>
      {progress.steps.length ? <ol className="assistant-trace-steps">{progress.steps.map(step => <li key={step.sequence}>
        <details className="assistant-trace-step">
          <summary><span className="assistant-trace-order">{step.sequence}</span><span className="assistant-trace-title">{step.label}</span>
            <span className={`assistant-trace-state is-${step.tone ?? 'neutral'}`}>{step.state}</span></summary>
          <dl className="assistant-trace-facts">
            {step.kind === 'tool' && <><dt>工具</dt><dd><code>{step.toolIdentity ?? '名称或版本未记录 / 不在展示清单'}</code></dd></>}
            <dt>记录创建</dt><dd><RecordTime value={step.createdAt}/></dd>
            <dt>记录更新</dt><dd><RecordTime value={step.updatedAt}/></dd>
            <dt>记录说明</dt><dd>创建和更新时间不是实际执行起止时间，不据此计算耗时。</dd>
            <dt>步骤标识</dt><dd><code>{step.id ?? '未记录'}</code></dd>
            <dt>输入 SHA-256</dt><dd><code>{step.inputDigest ?? '未记录可展示的指纹'}</code></dd>
            <dt>输出 SHA-256</dt><dd><code>{step.outputDigest ?? '未记录可展示的指纹'}</code></dd>
            <dt>输入 / 输出 token</dt><dd>{step.inputTokens ?? '未记录'} / {step.outputTokens ?? '未记录'}</dd>
            {step.safeTrace && <><dt>安全字段记录</dt><dd>{step.safeTrace.state === 'withheld' ? '当前来源或权限不可用，未展示' : <>
              <p>接受记录：<RecordTime value={step.safeTrace.acceptedAt}/></p>
              <p>结果记录：<RecordTime value={step.safeTrace.resultAt}/></p>
              <strong>调用参数（有限字段）</strong><dl>{step.safeTrace.arguments.map((field, i) => <div key={i}><dt>{field.label}</dt><dd><code>{field.value}</code></dd></div>)}</dl>
              <strong>调用结果（有限字段）</strong><dl>{step.safeTrace.result.map((field, i) => <div key={i}><dt>{field.label}</dt><dd><code>{field.value}</code></dd></div>)}</dl>
            </>}</dd></>}
            {step.error && <><dt>错误 / 待核对</dt><dd>{step.error}</dd></>}
          </dl>
        </details>
      </li>)}</ol> : <p className="assistant-trace-empty">尚无可展示的执行步骤；不能据此认定没有执行过。</p>}
      <dl className="assistant-trace-reference"><dt>工作编号</dt><dd><code>{workId}</code></dd><dt>Run 编号</dt><dd><code>{runId ?? '未记录'}</code></dd></dl>
    </div>
  </details>;
}
