import { useEffect, useRef, useState } from "react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import { ApiError } from "../../lib/api/client";
import type { Session } from "../../lib/auth/session";
import { authorizedRequest } from "../../lib/auth/session";
import { ActionConfirmation } from "./ActionConfirmation";
import { AnswerText } from "./AnswerText";
import { RunTimeline } from "./RunTimeline";
import { describeAgentError, isKnownErrorCode } from "./errorCopy";
import { checkedReceipt, allowsHumanDecision, executionNotice, terminalRun, ExecutionProtocolError } from "./execution";
import type { AgentExecution } from "./execution";
import { canSendRunCommands, prepareRunSubmission, readExistingRun, resolveCreationMode, shouldHideReceiptOnError, submitPreparedRun } from "./sdkRunCreation";

const MAX_AGENT_MESSAGE_CHARS = 4_200_000;

export interface AgentStep {
  id: string;
  sequence_no: number;
  kind: "model" | "tool" | "checkpoint";
  state: string;
  input_ref?: string | null;
  output_ref?: string | null;
  error_code?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  tool_key?: string;
  tool_version?: string;
  created_at?: string;
  updated_at?: string;
}

export interface PendingAction {
  tool_key: string;
  version: string;
  arguments: Record<string, unknown>;
  input_digest: string;
  expires_at: string;
  run_version: number;
  step_version: number;
}

export interface AnswerCitation {
  kind: "knowledge" | "catalog" | "memory" | "guanjia_observation";
  object_id: string;
  version_ref: string;
  version: string;
  label: string;
  source_ref: string | null;
  observed_at: string | null;
}

export interface AgentRunReceipt {
  run_id: string;
  state: string;
  execution?: AgentExecution | null;
  answer?: string | null;
  answer_status?: "available" | "not_recorded" | "unavailable";
  answer_unavailable_reason?: string | null;
  citations?: AnswerCitation[];
  error_code?: string | null;
  replayed?: boolean;
  pending_action?: PendingAction;
  task_id?: string;
  steps?: AgentStep[];
}

export interface AgentChatPageProps {
  session: Session;
  baseUrl?: string;
  initialRun?: AgentRunReceipt;
  initialActiveRunId?: string;
}

export function sessionScopeKey(session: Session): string {
  return [session.subjectId, session.organizationId, session.storeId,
          session.accessToken].join("\u0000");
}

export function buildRunRequest(text: string, runId?: string): { text: string; run_id?: string } {
  return runId ? { text: text.trim(), run_id: runId } : { text: text.trim() };
}

export function buildDecisionRequest(
  pending: PendingAction,
  decision: "approve" | "reject",
): Record<string, unknown> {
  return {
    decision,
    input_digest: pending.input_digest,
    reason: decision === "approve" ? "员工确认执行" : "员工拒绝执行",
    run_version: pending.run_version,
    step_version: pending.step_version,
  };
}

const SUGGESTIONS: Array<{ query: string; title: string; desc: string }> = [
  { query: "004.99.0183 是什么商品", title: "按条码查商品", desc: "输入条码，返回名称、规格与候选，需员工核对" },
  { query: "有哪些螺丝批头", title: "按类别搜门店", desc: "模糊词检索全部在售候选" },
  { query: "生料带", title: "查单一品名", desc: "看常用辅料的候选与来源版本" },
];

function CitationItem({ source }: { source: AnswerCitation }): ReactNode {
  return (
    <li className="citation">
      <p className="citation__label">{source.label}</p>
      <dl className="citation__meta">
        <div>
          <dt>来源</dt>
          <dd>{source.source_ref ?? source.object_id}</dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>{source.version}</dd>
        </div>
      </dl>
      <p className="citation__ref">版本标识：{source.version_ref}</p>
      {source.observed_at ? <p className="citation__ref">{source.kind === "guanjia_observation" ? "查询观察时间" : "来源时间"}：{source.observed_at}</p> : null}
    </li>
  );
}

type Tone = "neutral" | "info" | "success" | "warning" | "danger";
type Ticket = { scope: string; seq: number };
type Entry =
  | { kind: "user"; text: string; key: number }
  | { kind: "run"; run: AgentRunReceipt; key: number };

function runStatus(run: AgentRunReceipt | null, pending: boolean): { label: string; tone: Tone } {
  if (pending) return { label: "处理中", tone: "info" };
  if (!run) return { label: "待命", tone: "neutral" };
  switch (run.state) {
    case "created":
      return { label: "已创建", tone: "info" };
    case "running":
      return { label: "处理中", tone: "info" };
    case "waiting_for_human":
      return { label: "待你确认", tone: "warning" };
    case "waiting_for_external":
      return { label: "等待外部系统", tone: "warning" };
    case "completed":
      return { label: "已完成", tone: "success" };
    case "failed":
      return { label: "未成功", tone: "danger" };
    case "cancelled":
      return { label: "已取消", tone: "neutral" };
    default:
      return { label: run.state, tone: "neutral" };
  }
}

function CopyAnswer({ text }: { text: string }): ReactNode {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" className="copy-answer"
      onClick={() => {
        setCopied(true);
        void navigator.clipboard?.writeText(text).catch(() => {});
        window.setTimeout(() => setCopied(false), 2000);
      }}>
      {copied ? "已复制" : "复制"}
    </button>
  );
}

function RunCard({ run, pending, onDecision, commandsEnabled }: {
  run: AgentRunReceipt;
  pending: boolean;
  onDecision: (decision: "approve" | "reject") => void;
  commandsEnabled: boolean;
}): ReactNode {
  const meta = runStatus(run, false);
  const errorCopy = run.error_code ? describeAgentError(run.error_code) : null;
  const hasAnswer = run.state === "completed" && !!run.answer &&
    run.answer_status !== "unavailable" && run.answer_status !== "not_recorded";
  return (
    <article className="msg msg--assistant">
      <div className={`run-card run-card--${meta.tone}`} data-run-state={run.state}>
        <header className="run-card__head">
          <span className={`run-card__dot run-card__dot--${meta.tone}`} aria-hidden="true" />
          <span className="run-card__status">{meta.label}</span>
          <span className="run-card__id" title={run.run_id}>{run.run_id}</span>
        </header>

        {executionNotice(run) ? (
          <p className="run-card__notice" role="status">{executionNotice(run)}</p>
        ) : null}

        {errorCopy ? (
          <div className="run-card__failure" role="alert">
            <p className="run-card__failure-title">{errorCopy.title}</p>
            {errorCopy.hint ? <p className="run-card__failure-hint">{errorCopy.hint}</p> : null}
            {run.error_code && !isKnownErrorCode(run.error_code) ? (
              <p className="run-card__failure-code">错误码：{run.error_code}</p>
            ) : null}
          </div>
        ) : null}

        {hasAnswer ? (
          <div className="answer-block">
            <AnswerText text={run.answer as string} />
            <CopyAnswer text={run.answer as string} />
            {run.citations?.length &&
              !((run.steps ?? []).length > 0 && (run.steps ?? []).every((step) => step.kind === "model")) ? (
              <>
                <ul className="citations" aria-label="回答引用">
                  {run.citations.slice(0, 6).map((source) => (
                    <CitationItem key={`${source.kind}:${source.object_id}:${source.version_ref}`} source={source} />
                  ))}
                </ul>
                {run.citations.length > 6 ? (
                  <details className="citations__more">
                    <summary>查看其余 {run.citations.length - 6} 条引用</summary>
                    <ul className="citations">
                      {run.citations.slice(6).map((source) => (
                        <CitationItem key={`${source.kind}:${source.object_id}:${source.version_ref}`} source={source} />
                      ))}
                    </ul>
                  </details>
                ) : null}
              </>
            ) : (
              <p className="answer-block__warn">暂无结构化引用，不能据此确认业务事实。</p>
            )}
            <p className="answer-block__foot">模型回答仅供参考，业务事实以系统记录为准。</p>
          </div>
        ) : null}

        {run.answer_status === "unavailable" ? (
          <p className="run-card__notice" role="status">
            回答或来源已失效、被撤回，或当前权限不足；正文与引用已隐藏。
          </p>
        ) : run.state === "completed" && run.answer_status === "not_recorded" ? (
          <p className="run-card__notice" role="status">此运行没有可恢复的回答收据，不能补造旧回答。</p>
        ) : null}

        <RunTimeline steps={run.steps ?? []} runState={run.state}
          errorCode={errorCopy?.title ?? null} />

        {commandsEnabled && run.pending_action ? (
          <ActionConfirmation
            pending={run.pending_action}
            disabled={pending || !allowsHumanDecision(run)}
            onDecision={onDecision}
          />
        ) : null}

        {run.task_id ? <p className="run-card__task">任务建议编号：{run.task_id}</p> : null}
      </div>
    </article>
  );
}

export function AgentChatPage({
  session,
  baseUrl = "",
  initialRun,
  initialActiveRunId,
}: AgentChatPageProps) {
  // Build-time opt-in is a routing preference, never an authorization or readiness signal.
  const creationMode = resolveCreationMode(import.meta.env.VITE_AGENT_SDK_READONLY_CREATE);
  const commandsEnabled = canSendRunCommands(creationMode);
  function initialReceipt(): AgentRunReceipt | null {
    try { return initialRun ? checkedReceipt(initialRun) : null; } catch { return null; }
  }
  const initial = initialReceipt();
  const [text, setText] = useState("");
  const [locator, setLocator] = useState(initialRun?.run_id ?? initialActiveRunId ?? "");
  const [entries, setEntries] = useState<Entry[]>(initial ? [{ kind: "run", run: initial, key: 0 }] : []);
  const [error, setError] = useState<string | null>(initialRun && !initial ? new ExecutionProtocolError().message : null);
  const [pending, setPending] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(
    initialActiveRunId ?? (initialRun && (!initial || !terminalRun(initialRun.state)) ? initialRun.run_id : null));
  const scope = sessionScopeKey(session);
  const scopeRef = useRef(scope);
  const busyRef = useRef(false);
  const operationRef = useRef(0);
  const entryKeyRef = useRef(1);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const composingRef = useRef(false);
  const [displayScope, setDisplayScope] = useState(scope);
  scopeRef.current = scope;

  useEffect(() => {
    operationRef.current += 1;
    busyRef.current = false;
    setDisplayScope(scope);
    setEntries(initial ? [{ kind: "run", run: initial, key: 0 }] : []);
    entryKeyRef.current = 1;
    setText("");
    setLocator(initialRun?.run_id ?? initialActiveRunId ?? "");
    setPending(false);
    setError(initialRun && !initialReceipt() ? new ExecutionProtocolError().message : null);
    setActiveRunId(initialActiveRunId ?? (initialRun && (!initialReceipt() || !terminalRun(initialRun.state)) ? initialRun.run_id : null));
    return () => { operationRef.current += 1; busyRef.current = false; };
  }, [scope, initialRun, initialActiveRunId]);

  const transcript = displayScope === scope ? entries : [];
  const lastRun = [...transcript].reverse().find((entry) => entry.kind === "run");
  const visibleRun = lastRun && lastRun.kind === "run" ? lastRun.run : null;

  useEffect(() => {
    const node = streamRef.current;
    if (node) node.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [transcript, pending, error]);

  function nextKey(): number { return entryKeyRef.current++; }

  function begin(id: string) {
    if (scopeRef.current !== scope || displayScope !== scope || busyRef.current) return null;
    busyRef.current = true;
    const ticket: Ticket = { scope, seq: ++operationRef.current };
    setPending(true);
    setLocator(id);
    setActiveRunId(id);
    setError(null);
    return ticket;
  }
  function current(ticket: Ticket) {
    return scopeRef.current === ticket.scope && operationRef.current === ticket.seq;
  }
  function finish(ticket: Ticket) {
    if (current(ticket)) { busyRef.current = false; setPending(false); }
  }
  function report(caught: unknown, ticket: Ticket) {
    if (!current(ticket)) return;
    if (shouldHideReceiptOnError(creationMode, caught)) setEntries([]);
    // Keep the original activeRunId/locator. Even GET 404 cannot authorize re-creation.
    setError(caught instanceof ApiError ? caught.userMessage :
      caught instanceof ExecutionProtocolError ? caught.message : "结果暂未核实，请读取原运行，勿重复操作。");
  }
  function adopt(next: AgentRunReceipt, ticket: Ticket) {
    if (!current(ticket)) return;
    setError(null);
    setActiveRunId(terminalRun(next.state) ? null : next.run_id);
    setEntries((prev) => {
      const index = prev.findIndex((entry) => entry.kind === "run" && entry.run.run_id === next.run_id);
      if (index === -1) return [...prev, { kind: "run", run: next, key: nextKey() }];
      const copy = [...prev];
      copy[index] = { kind: "run", run: next, key: copy[index].key };
      return copy;
    });
  }
  async function fetchRun(id: string, ticket: Ticket) {
    if (!current(ticket)) return;
    const next = await readExistingRun(id, baseUrl);
    if (current(ticket)) adopt(checkedReceipt(next, id), ticket);
  }
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (displayScope !== scope || busyRef.current || activeRunId || !text.trim()) return;
    const id = crypto.randomUUID();
    const question = text.trim();
    const submission = prepareRunSubmission(creationMode, buildRunRequest(question, id));
    const ticket = begin(id);
    if (!ticket) return;
    if (current(ticket)) setEntries((prev) => [...prev, { kind: "user", text: question, key: nextKey() }]);
    try {
      const next = await submitPreparedRun(submission, baseUrl);
      if (current(ticket)) { adopt(checkedReceipt(next, id), ticket); setText(""); }
    } catch (caught) { report(caught, ticket); }
    finally { finish(ticket); }
  }
  async function readRun(id: string): Promise<void> {
    if (displayScope !== scope || !id.trim()) return;
    const ticket = begin(id);
    if (!ticket) return;
    try { await fetchRun(id, ticket); } catch (caught) { report(caught, ticket); }
    finally { finish(ticket); }
  }
  async function recover(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (locator.trim()) await readRun(locator.trim());
  }
  async function command(id: string, suffix: string, body?: Record<string, unknown>) {
    // SDK and manually recovered runs in SDK mode remain read-only; cancel is not verified.
    if (!commandsEnabled) return;
    const ticket = begin(id);
    if (!ticket) return;
    try {
      try {
        await authorizedRequest(`/api/v1/agent-runs/${encodeURIComponent(id)}/${suffix}`, {
          method: "POST", baseUrl, ...(body ? { body } : {}),
        });
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 401) throw caught;
        // A lost command response is resolved by GET, never by repeating POST.
        report(caught, ticket);
      }
      await fetchRun(id, ticket);
    } catch (caught) { report(caught, ticket); }
    finally { finish(ticket); }
  }
  async function decide(run: AgentRunReceipt, decision: "approve" | "reject"): Promise<void> {
    // The clicked card owns the reviewed digest/versions, not the latest transcript Run.
    if (!run.pending_action || !allowsHumanDecision(run) || busyRef.current) return;
    await command(run.run_id, "confirmation", buildDecisionRequest(run.pending_action, decision));
  }
  async function stop(): Promise<void> {
    const id = activeRunId ?? visibleRun?.run_id;
    if (!visibleRun || !id || busyRef.current || visibleRun.execution?.cancel_requested) return;
    await command(id, "cancel");
  }
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    if (composingRef.current || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (pending || activeRunId || !text.trim()) return;
    event.currentTarget.form?.requestSubmit();
  }
  function pick(item: { query: string }) {
    setText(item.query);
    inputRef.current?.focus();
  }

  const meta = runStatus(visibleRun, pending);
  const showStop = commandsEnabled && displayScope === scope && (!!activeRunId ||
    (!!visibleRun && ["running", "waiting_for_human", "waiting_for_external", "created"].includes(visibleRun.state)));
  const refreshTarget = activeRunId ?? visibleRun?.run_id ?? locator.trim();

  return (
    <section className="agent-chat" aria-labelledby="agent-title">
      <header className="agent-chat__head">
        <div className="agent-chat__intro">
          <p className="eyebrow">管理端 · Agent 工作台</p>
          <h2 id="agent-title">门店智能助手</h2>
          <p className="agent-chat__lede">只在已登记的工具范围内查询门店数据；涉及写入时会停下来等你确认。</p>
          {!commandsEnabled ? (
            <p className="run-card__notice" role="status">
              SDK 只读试用：仅选择独立创建入口，不代表后台已启用。结果未核实时请读取原运行，勿重新提交；此模式不提供取消或确认操作。
            </p>
          ) : null}
        </div>
        <span className={`status-chip status-chip--${meta.tone}`}>{meta.label}</span>
      </header>

      <div className="agent-chat__stream" ref={streamRef} aria-live="polite">
        {transcript.length === 0 && !pending ? (
          <div className="agent-chat__welcome">
            <h3 className="agent-chat__welcome-title">你好，今天要查什么？</h3>
            <p className="agent-chat__welcome-sub">零售门店助手 · 只在已登记工具范围内查门店数据，写入前会先请你确认。</p>
            <div className="ag-suggest">
              {SUGGESTIONS.map((item) => (
                <button key={item.query} type="button" className="ag-suggest__item" onClick={() => pick(item)}>
                  <span className="ag-suggest__title">{item.title}</span>
                  <span className="ag-suggest__desc">{item.desc}</span>
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {transcript.map((entry) => entry.kind === "user" ? (
          <div className="msg msg--user" key={`u${entry.key}`}>
            <p>{entry.text}</p>
          </div>
        ) : (
          <RunCard key={`r${entry.key}`} run={entry.run} pending={pending}
            onDecision={(decision) => void decide(entry.run, decision)} commandsEnabled={commandsEnabled} />
        ))}

        {pending ? (
          <div className="msg msg--assistant msg--thinking" key="thinking">
            <span className="thinking-dots" aria-hidden="true"><i /><i /><i /></span>
            <span>正在思考并检索门店数据…</span>
          </div>
        ) : null}

        {error && displayScope === scope ? (
          <p className="agent-chat__error" role="alert">{error}</p>
        ) : null}
      </div>

      <form className="composer" onSubmit={submit}>
        <textarea
          ref={inputRef}
          className="composer__input"
          name="agent-input"
          rows={1}
          maxLength={MAX_AGENT_MESSAGE_CHARS}
          value={displayScope === scope ? text : ""}
          onChange={(event) => { setText(event.target.value); const el = event.target; el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 200)}px`; }}
          onKeyDown={onKeyDown}
          onCompositionStart={() => { composingRef.current = true; }}
          onCompositionEnd={() => { composingRef.current = false; }}
          placeholder="问一句，比如：004.99.0183 是什么商品"
          aria-label="你要做什么"
        />
        <div className="composer__bar">
          <span className="composer__hint">Enter 发送 · Shift + Enter 换行</span>
          <div className="composer__buttons">
            {showStop ? (
              <button className="btn btn--ghost" type="button"
                disabled={pending || !visibleRun || !!visibleRun.execution?.cancel_requested}
                onClick={() => void stop()}>
                停止运行
              </button>
            ) : null}
            <button className="btn btn--ghost" type="button"
              disabled={pending || !refreshTarget}
              onClick={() => void readRun(refreshTarget)}>
              刷新运行状态
            </button>
            <button className="composer__send" type="submit" aria-label="发送"
              disabled={pending || !!activeRunId || !text.trim()}>
              ↑
            </button>
          </div>
        </div>
      </form>

      <details className="agent-chat__recover">
        <summary>读取已有运行</summary>
        <form className="agent-chat__recover-row" onSubmit={recover} aria-label="按编号读取已有运行">
          <input value={displayScope === scope ? locator : ""} maxLength={36}
            onChange={(event) => setLocator(event.target.value)}
            placeholder="输入本人的运行编号" aria-label="运行编号" />
          <button className="btn btn--ghost" type="submit" disabled={pending || !locator.trim()}>读取</button>
        </form>
      </details>
    </section>
  );
}
