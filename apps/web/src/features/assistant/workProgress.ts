export interface TraceField { label: string; value: string }
export interface ProgressStep {
  sequence: number; label: string; state: string; updatedAt: string;
  kind?: 'model' | 'tool' | 'checkpoint'; id?: string; createdAt?: string;
  toolIdentity?: string; inputDigest?: string; outputDigest?: string;
  inputTokens?: number; outputTokens?: number; error?: string;
  safeTrace?: { state: 'available' | 'withheld'; acceptedAt?: string; resultAt?: string; arguments: TraceField[]; result: TraceField[] };
  tone?: 'neutral' | 'active' | 'success' | 'warning' | 'error';
}
/** Display only Host receipt facts; never infer effects or authorize a retry. */
export interface WorkProgress {
  activity?: string;
  explanation?: string;
  uncertain: boolean;
  stopRecorded?: boolean;
  traceState?: 'not_recorded' | 'available' | 'withheld';
  steps: ProgressStep[];
}
const phases: Record<string, string> = {
  queued: '等待处理', claimed: '正在准备执行', model_inflight: '正在思考',
  tool_inflight: '正在调用工具', waiting_for_human: '等待你的处理', terminal: '',
};
const stepStates: Record<string, string> = {
  pending: '等待处理', running: '进行中', succeeded: '已完成', failed: '未完成',
  unknown: '结果待核对', cancelled: '已停止',
};
const tools: Record<string, string> = {
  'research.search': '检索研究资料', 'research.read': '读取研究资料', 'research.describe': '查看候选合同', 'research.propose': '生成未执行提案',
  'runtime.tool_search': '查找可用工具', 'memory.search': '查找助理记忆', 'memory.save': '保存助理记忆',
  'memory.correct': '更正助理记忆', 'memory.withdraw': '撤回助理记忆',
  'catalog.search': '查找商品', 'knowledge.search': '查找知识', 'tasks.list_mine': '查看我的工作',
  'guanjia.read_prices': '读取商品价格', 'guanjia.read_stock': '读取商品库存',
  'guanjia.category_discovery': '查找商品分类', 'guanjia.category_products': '查询分类商品',
  'guanjia.category_products_status': '核对分类商品状态', 'guanjia.observe_prices': '观察商品价格',
  'guanjia.observe_stock': '观察商品库存', 'tasks.suggest_review': '提交任务建议',
  'sandbox.python_exec': '计算工具记录（旧入口）', 'tools.create_tool': '工具草稿记录（旧入口）',
  'stocktake.read_evidence': '读取盘点依据', 'purchasing.extract_candidates': '提取采购候选',
  'purchasing.explain_discrepancies': '核对采购差异',
};
const failures: Record<string, string> = {
  pi_provider_http_400: '模型服务拒绝了请求格式（HTTP 400），需要修复连接配置。原请求已保留。',
  pi_provider_http_401: '模型服务认证失败（HTTP 401），需要核对连接凭据。原请求已保留。',
  pi_provider_http_402: '模型服务返回付费或额度错误（HTTP 402），需要核对服务商账户。原请求已保留。',
  pi_provider_http_403: '模型服务拒绝访问（HTTP 403），需要核对账户权限或地区设置。原请求已保留。',
  pi_provider_http_404: '模型服务未找到请求的资源（HTTP 404），需要核对模型和接口配置。原请求已保留。',
  pi_provider_http_408: '模型服务报告请求超时（HTTP 408）。执行与用量仍需核对，未自动重发。',
  pi_provider_http_413: '模型服务拒绝了过大的请求（HTTP 413）。原文已保留，未自动截断或重发。',
  pi_provider_http_429: '模型服务限制了这次请求（HTTP 429）。具体是限流还是额度限制仍待核对；原请求已保留，未自动重发。',
  pi_provider_http_500: '模型服务发生错误（HTTP 500）。执行与用量仍需核对，未自动重发。',
  pi_provider_http_502: '模型服务网关出错（HTTP 502）。执行与用量仍需核对，未自动重发。',
  pi_provider_http_503: '模型服务暂时不可用（HTTP 503）。执行与用量仍需核对，未自动重发。',
  pi_provider_http_504: '模型服务网关超时（HTTP 504）。执行与用量仍需核对，未自动重发。',
  agent_budget_exceeded: '这次工作的用量达到当前执行额度，处理已停止。',
  agent_deadline_exceeded: '这次工作超过执行时限，处理已停止；已有执行结果需按记录核对。',
  tool_scope_denied: '这次工具操作不在当前授权范围内。',
  tool_agent_permission_denied: '助理没有执行这次工具操作的权限。',
  tool_requester_permission_denied: '当前账号没有执行这次工具操作的权限。',
};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('执行记录格式异常。');
  return value as Record<string, unknown>;
}
function member(value: unknown, values: Record<string, string>): string {
  if (typeof value !== 'string' || !Object.hasOwn(values, value)) throw Error('执行记录格式异常。');
  return value;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const isoTime = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
function recordedTime(value: unknown): string {
  if (typeof value !== 'string' || !isoTime.test(value) || !Number.isFinite(Date.parse(value))) throw Error('执行记录格式异常。');
  return value;
}
function optionalId(value: unknown): string | undefined {
  if (value == null) return undefined;
  if (typeof value !== 'string' || !uuid.test(value)) throw Error('执行记录标识异常。');
  return value;
}
function safeDigest(value: unknown): string | undefined {
  if (value == null) return undefined;
  if (typeof value !== 'string') throw Error('执行记录格式异常。');
  // Historical opaque references are not display-safe, and never echoed.
  if (!value.startsWith('sha256:')) return undefined;
  if (!/^sha256:[0-9a-f]{64}$/.test(value)) throw Error('执行记录摘要异常。');
  return value.slice(7);
}
function usage(value: unknown): number | undefined {
  if (value == null) return undefined;
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw Error('执行用量记录异常。');
  return value as number;
}
export function parseProgress(status: string, value: Record<string, unknown> | null, dispatchCode: unknown): WorkProgress {
  const result: WorkProgress = { uncertain: ['needs_attention', 'dispatch_unverified'].includes(status), steps: [] };
  const active = ['created', 'running'].includes(status);
  if (value?.execution != null) {
    const execution = object(value.execution);
    const phase = member(execution.phase, phases);
    if (execution.protocol_version !== 1 || typeof execution.cancel_requested !== 'boolean' ||
        typeof execution.cancel_resolution !== 'string' ||
        !['not_requested', 'requested', 'cancelled', 'too_late', 'unknown'].includes(execution.cancel_resolution) ||
        typeof execution.effect_status !== 'string' ||
        !['none', 'local_task_committed', 'external_unknown'].includes(execution.effect_status)) throw Error('执行记录格式异常。');
    if (execution.cancel_requested === (execution.cancel_resolution === 'not_requested')) throw Error('停止记录不一致。');
    if (active && phase !== 'terminal') result.activity = phases[phase];
    if (execution.cancel_requested) result.stopRecorded = true;
    if (execution.effect_status === 'external_unknown' || execution.cancel_resolution === 'unknown') result.uncertain = true;
  }
  let lastFailure: unknown;
  if (value?.steps !== undefined) {
    if (!Array.isArray(value.steps) || value.steps.length > 1000) throw Error('执行记录格式异常。');
    let previous = 0;
    const ids = new Set<string>();
    result.steps = value.steps.map(item => {
      const step = object(item);
      if (!Number.isSafeInteger(step.sequence_no) || (step.sequence_no as number) <= previous ||
          typeof step.updated_at !== 'string' || recordedTime(step.updated_at) !== step.updated_at) throw Error('执行记录格式异常。');
      previous = step.sequence_no as number;
      const kind = member(step.kind, { model: '模型', tool: '工具', checkpoint: '检查' });
      const state = member(step.state, stepStates);
      if (state === 'unknown') result.uncertain = true;
      if (state === 'failed' || state === 'unknown') lastFailure = step.error_code;
      const toolLabel = typeof step.tool_key === 'string' && Object.hasOwn(tools, step.tool_key) ? tools[step.tool_key] : '调用工具';
      const id = optionalId(step.id);
      if (id && ids.has(id)) throw Error('执行记录标识重复。');
      if (id) ids.add(id);
      if (step.run_id != null && step.run_id !== value.run_id) throw Error('执行记录不属于当前工作。');
      const createdAt = step.created_at == null ? undefined : recordedTime(step.created_at);
      if (createdAt && Date.parse(createdAt) > Date.parse(step.updated_at)) throw Error('执行记录时间不一致。');
      if (step.tool_version != null && typeof step.tool_version !== 'string') throw Error('执行记录版本异常。');
      const terminalPending = ['failed', 'completed', 'cancelled'].includes(status) && ['pending', 'running'].includes(state);
      const error = typeof step.error_code === 'string' && Object.hasOwn(failures, step.error_code)
        ? failures[step.error_code] : step.error_code === 'pi_attempt_response_unknown'
          ? '发送结果尚未确定；保留原记录，未自动重发。'
          : (state === 'failed' || state === 'unknown') ? '具体原因未记录为可展示的错误，请核对原回执。' : undefined;
      return { sequence: previous, label: kind === 'model' ? '助理思考' : kind === 'tool' ? toolLabel : '执行检查',
        kind: kind as ProgressStep['kind'], id, createdAt, updatedAt: step.updated_at,
        state: terminalPending ? '未记录终态' : stepStates[state],
        tone: (terminalPending || state === 'unknown' ? 'warning' : state === 'succeeded' ? 'success'
          : state === 'failed' ? 'error' : state === 'running' ? 'active' : 'neutral') as ProgressStep['tone'],
        toolIdentity: kind === 'tool' && typeof step.tool_key === 'string' && Object.hasOwn(tools, step.tool_key)
          && step.tool_version === '1' ? step.tool_key + '@1' : undefined,
        inputDigest: safeDigest(step.input_ref), outputDigest: safeDigest(step.output_ref),
        inputTokens: usage(step.input_tokens), outputTokens: usage(step.output_tokens), error };
    });
  }
  if (lastFailure === 'pi_attempt_response_unknown' || value?.error_code === 'pi_attempt_response_unknown' ||
      dispatchCode === 'PI_LAUNCH_OUTCOME_UNRESOLVED') result.uncertain = true;
  if (status === 'failed' || status === 'needs_attention') {
    const code = lastFailure ?? value?.error_code;
    result.explanation = typeof code === 'string' && Object.hasOwn(failures, code) ? failures[code]
      : '这次工作未能完成，现有回执尚不能确定具体原因。原请求与执行记录已保留。';
  } else if (status === 'blocked') {
    result.explanation = '执行前检查未通过，工作已保留；需要核对当前授权和运行条件。';
  } else if (status === 'authority_unrecorded') {
    result.explanation = '这项工作的执行授权记录尚未齐全，暂未接续。';
  }
  attachPublicTrace(result, value);
  return result;
}

const tracePins: Record<string, readonly string[]> = {"runtime.tool_search": ["d95ef5564ccc67f83884c97765ac146ee4a1a0b7faf4a7f08e66af513b3b86e6", "178b2797956d5cd84637239b9ccccb53a883fa8c615f85aec5e52cd9cb789e0c"], "memory.search": ["0aa302e63f58b49a1e6f5136c52718d357e4a59914dbd2e50738e4ffdb7995b0", "cdb00b817a89c7603b2104b0e9bffa29132e7d2666077a82a8acfcd65cbee8c5"], "research.search": ["342df1e22d1be98ddaed59d465c8a95a4a47654d65cfec38884e6c16ce40d45c", "af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58"], "research.read": ["41f22cf0b4f3f2ed46f0998cede81a6cc6317e44acc99b6b10dcd2387a599e49", "af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58"], "research.describe": ["ef059a398aa707e22923bba98568d2bfb694268dde3728c803319b68d1f41889", "af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58"], "research.propose": ["e9fd9b4a3ed0f2229e33efa7d2e42e68ef731465f7c0ac3719356cf013dd02d7", "af4413b44c2b1968b923b71b0da287e46cf1a4238e67addbec279ac4b5a07c58"]};

const nativeNames = new Set(['tool_search', 'v4_memory_search_v1', 'v4_tasks_list_mine_v1', 'v4_knowledge_search_v1',
  'v4_catalog_search_v1', 'v4_memory_save_v1', 'v4_memory_correct_v1', 'v4_memory_withdraw_v1',
  'v4_guanjia_read_prices_v1', 'v4_guanjia_read_stock_v1', 'v4_guanjia_category_discovery_v1',
  'v4_guanjia_category_products_v1', 'v4_guanjia_category_products_status_v1',
  'v4_research_search_v1', 'v4_research_read_v1', 'v4_research_describe_v1', 'v4_research_propose_v1']);
const hex = /^[0-9a-f]{64}$/;
function badTrace(): never { throw Error('安全投影不可用。'); }
function traceFields(value: unknown): Record<string, unknown> {
  const n = object(value);
  if (n.state !== 'recorded' || n.unlisted_fields !== 'withheld') badTrace();
  return object(n.fields);
}
function traceScalar(fields: Record<string, unknown>, key: string, kind: 'count' | 'boolean' | 'sha' | 'uuid', max = Number.MAX_SAFE_INTEGER): string {
  const n = object(fields[key]);
  if (n.state === 'absent') return '未记录';
  if (n.state === 'withheld') return '未展示';
  if (n.state !== 'recorded') return badTrace();
  const v = n.value;
  if (kind === 'count' && Number.isSafeInteger(v) && (v as number) >= 0 && (v as number) <= max) return String(v);
  if (kind === 'boolean' && typeof v === 'boolean') return v ? '是' : '否';
  if (kind === 'sha' && typeof v === 'string' && hex.test(v)) return v;
  if (kind === 'uuid' && typeof v === 'string' && uuid.test(v)) return v;
  return badTrace();
}
function traceArray(fields: Record<string, unknown>, key: string, limit: number): unknown[] {
  const n = object(fields[key]);
  if (n.state !== 'recorded' || !Array.isArray(n.items) || n.items.length > limit) badTrace();
  return n.items as unknown[];
}
function displayPart(key: string, part: 'arguments' | 'result', value: unknown): TraceField[] {
  const n = object(value);
  if (n.state === 'not_recorded') return [{ label: '记录状态', value: '未记录结果，不能认定为空或成功' }];
  if (n.state === 'withheld') return [{ label: '记录状态', value: '工具或字段未经当前展示规则核验，未展示' }];
  const f = traceFields(n), rows: TraceField[] = [];
  const add = (field: string, label: string, kind: 'count' | 'boolean' | 'sha' | 'uuid', max?: number) => rows.push({ label, value: traceScalar(f, field, kind, max) });
  if (part === 'arguments') {
    if (['memory.search', 'runtime.tool_search', 'research.search'].includes(key)) {
      rows.push({ label: '查询文本', value: '未捕获自由文本' });
      add('limit', '请求上限', 'count', key === 'memory.search' ? 5 : key === 'research.search' ? 20 : 100);
    } else {
      rows.push({ label: '文本与来源名称', value: '未在界面复制；仅展示已核摘要或范围' });
      add('corpus_digest', '资料包 SHA-256', 'sha');
      if (key === 'research.read') { add('start', '起始位置', 'count', 65536); add('limit', '读取上限', 'count', 4096); }
    }
    return rows;
  }
  if (key === 'memory.search') {
    const items = traceArray(f, 'items', 5); rows.push({ label: '本次返回条数', value: String(items.length) });
    items.forEach((item, i) => {
      const itemFields = traceFields(item);
      for (const [field, label, kind] of [['id', '记忆标识', 'uuid'], ['version', '来源版本', 'count'], ['active', '捕获时有效', 'boolean']] as const)
        rows.push({ label: `${i + 1}. ${label}`, value: traceScalar(itemFields, field, kind, field === 'version' ? 2147483647 : undefined) });
    });
    rows.push({ label: '记忆正文', value: '未捕获；当前来源权限与版本已由读取端重新核验' });
  } else if (key === 'runtime.tool_search') {
    const items = traceArray(f, 'loaded', 100);
    const names = items.map(item => { const n = object(item); if (n.state !== 'recorded' || typeof n.value !== 'string' || !nativeNames.has(n.value)) return badTrace(); return n.value; });
    rows.push({ label: '装载工具数', value: String(names.length) }, { label: '本次装载', value: names.length ? names.join('，') : '空列表' });
  } else {
    add('corpus_digest', '资料包 SHA-256', 'sha');
    const data = traceFields(f.data);
    if (key === 'research.search') {
      for (const [field, label] of [['total_matches', '本资料包匹配数'], ['offset', '页起点']] as const)
        rows.push({ label, value: traceScalar(data, field, 'count', 2147483647) });
      rows.push({ label: '是否截断', value: traceScalar(data, 'truncated', 'boolean') });
    } else if (key === 'research.read') rows.push({ label: '是否截断', value: traceScalar(data, 'truncated', 'boolean') });
    else rows.push({ label: '候选 SHA-256', value: traceScalar(data, 'candidate_digest', 'sha') });
    rows.push({ label: '结果性质', value: key === 'research.propose' ? '仅提案，未执行业务操作' : '仅参考资料，不是业务验收' });
  }
  return rows;
}
function attachPublicTrace(progress: WorkProgress, receipt: Record<string, unknown> | null) {
  if (receipt?.public_trace == null) return;
  progress.traceState = 'not_recorded';
  try {
    const trace = object(receipt.public_trace);
    if (trace.version !== 1 || !Array.isArray(trace.records) || trace.records.length > 128) badTrace();
    if (trace.state === 'not_recorded' && trace.records.length === 0) return;
    if (trace.state === 'withheld' && trace.records.length === 0) { progress.traceState = 'withheld'; return; }
    if (trace.state !== 'available' || !optionalId(receipt.run_id)) badTrace();
    const seen = new Set<string>();
    const updates: { step: ProgressStep; safe: NonNullable<ProgressStep['safeTrace']> }[] = [];
    for (const item of trace.records) {
      const r = object(item), id = optionalId(r.receipt_id), stepId = optionalId(r.step_id);
      if (!id || seen.has(id) || typeof r.native_state !== 'string' || !['accepted', 'dispatch_intent', 'succeeded', 'failed', 'unknown'].includes(r.native_state)) badTrace();
      seen.add(id);
      if (!stepId) continue; // Accepted-only native calls have not created a tool Step yet.
      const step = progress.steps.find(s => s.id === stepId && s.kind === 'tool');
      if (!step) badTrace();
      if (updates.some(u => u.step === step)) badTrace();
      if (r.state === 'withheld') { updates.push({ step, safe: { state: 'withheld', arguments: [], result: [] } }); continue; }
      if (r.state !== 'available' || r.accepted == null) badTrace();
      const phase = (body: unknown, name: 'accepted' | 'result') => {
        const p = object(body), key = p.tool_key;
        if (typeof key !== 'string' || !Object.hasOwn(tracePins, key) || p.tool_version !== '1' || step.toolIdentity !== key + '@1' ||
            p.version !== 1 || p.phase !== name || p.authority !== 'projection_only' || p.body_is_original_stream !== false ||
            p.run_id !== receipt.run_id || p.receipt_id !== id || p.tool_step_id !== (name === 'accepted' ? null : stepId) ||
            !progress.steps.some(s => s.id === p.model_step_id && s.kind === 'model') ||
            p.input_sha256 !== step.inputDigest || !step.inputDigest ||
            p.output_sha256 !== (name === 'accepted' ? null : step.outputDigest) ||
            (name === 'result' && (!step.outputDigest || r.native_state !== 'succeeded')) ||
            p.input_schema_sha256 !== tracePins[key][0] || p.output_schema_sha256 !== tracePins[key][1]) badTrace();
        return { at: recordedTime(p.captured_at), args: displayPart(key, 'arguments', p.arguments), result: displayPart(key, 'result', p.result) };
      };
      const a = phase(r.accepted, 'accepted'), out = r.result == null ? null : phase(r.result, 'result');
      updates.push({ step, safe: { state: 'available', acceptedAt: a.at, resultAt: out?.at, arguments: a.args,
        result: out?.result ?? [{ label: '记录状态', value: '未记录结果，不能认定为空或成功' }] } });
    }
    for (const update of updates) update.step.safeTrace = update.safe;
    progress.traceState = 'available';
  } catch {
    // A malformed projection must never make raw response content visible.
    progress.traceState = 'withheld';
    for (const step of progress.steps) delete step.safeTrace;
  }
}
