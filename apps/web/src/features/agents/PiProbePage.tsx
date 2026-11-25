import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { Session } from "../../lib/auth/session";
import { getScopeVersion, subscribe } from "../../lib/auth/session";
import { PiProbeController, PROBE_SAMPLE_SHA256, PROBE_TEXT, probeSessionIsCurrent } from "./piProbeApi";
import type { Observation } from "./piProbeApi";

function Times({ value }: { value: Pick<Observation<unknown>, "startedAt" | "collectedAt"> }) {
  return <p>请求时间：{value.startedAt}；采集时间：{value.collectedAt}</p>;
}

export function PiProbePage({ session, baseUrl = "" }: { session: Session; baseUrl?: string }) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const controller = useMemo(() => new PiProbeController(session, {
    getItem: key => window.localStorage.getItem(key),
    setItem: (key, value) => window.localStorage.setItem(key, value),
  }, baseUrl), [session, baseUrl, scope]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [input, setInput] = useState("");
  useEffect(() => { controller.start(); return () => controller.dispose(); }, [controller]);
  useEffect(() => { setInput(""); }, [controller]);
  if (!probeSessionIsCurrent(session)) return <p role="alert">身份已改变，旧诊断内容已隐藏。请重新进入。</p>;
  return <section aria-labelledby="pi-probe-title" style={{ maxWidth: 900, margin: "24px auto", padding: 24 }}>
    <h2 id="pi-probe-title">Pi 单次诊断</h2>
    <p>仅固定匿名软件样本。首响应后故意停止，无工具续推、业务答案或写入入口。</p>
    <p>真实请求须由操作者另行核准 Go 账户及预配置窗口。页面回执不证明真实 Go 成功或营业可用。</p>
    <blockquote>{PROBE_TEXT}</blockquote>
    <p>样本 SHA-256：<code>{PROBE_SAMPLE_SHA256}</code></p>
    <label>预配置的原 Run UUID <input aria-label="预配置的原 Run UUID" value={view.runId || input}
      disabled={view.attempted || view.busy} spellCheck={false} autoComplete="off"
      onChange={event => setInput(event.target.value)} /></label>
    <p>已在 CLI 或其他浏览器发起的 Run，请选“只回查输入的原 Run”，该操作不会发送样本。仅显式发送一次；先保存已尝试记录。超时、断线、刷新或未知只回查原 Run，不换 UUID。</p>
    <button type="button" disabled={view.busy || view.attempted || !input}
      onClick={() => { void controller.send(input); }}>发送一次固定样本</button>{" "}
    <button type="button" disabled={view.busy || view.attempted || !input}
      onClick={() => { void controller.readOriginal(input); }}>只回查输入的原 Run</button>{" "}
    <button type="button" disabled={view.busy || !view.runId}
      onClick={() => { void controller.refresh(); }}>仅回查原 Run</button>{" "}
    <button type="button" disabled={view.busy || !view.runId}
      onClick={() => { void controller.refresh(true); }}>回查原 Run 与独立费用</button>
    <p role="status">{view.notice}</p>
    {view.run && <article aria-label="普通 Run 回执">
      <h3>原 Run</h3><p>{view.run.data.runId}：{view.run.data.state}</p><Times value={view.run} />
    </article>}
    {view.runtime && <article aria-label="运行时诊断回执">
      <h3>运行时回执（v2，非执行成功证明）</h3><p>原 Run：{view.runtime.data.runId}；状态：{view.runtime.data.state}</p>
      <Times value={view.runtime} /><p>版本清单摘要：<code>{view.runtime.data.manifest}</code></p>
      <ul>{view.runtime.data.steps.map(step => <li key={step.id}>Step {step.id}：{step.kind} / {step.state}</li>)}</ul>
      {view.runtime.data.providers.map(call => <dl key={call.receiptId}>
        <dt>Provider 收据</dt><dd>{call.receiptId} / {call.state}</dd>
        <dt>模型 Step / attempt / ledger</dt><dd>{call.stepId} / {call.attemptId} / {call.ledgerId}</dd>
        <dt>响应摘要</dt><dd>{call.responseDigest ?? "unknown"}</dd>
        <dt>消息摘要</dt><dd>{call.messageDigest ?? "unknown"}</dd>
        <dt>Provider request ID 摘要</dt><dd>{call.requestIdDigest ?? "unknown"}</dd>
        <dt>原始 usage 字段存在性</dt><dd>{call.usagePresence}</dd>
      </dl>)}
    </article>}
    {view.costNotice && <p>{view.costNotice}</p>}
    {view.costRead && <Times value={view.costRead} />}
    {view.costs && <article aria-label="独立费用回执"><h3>独立费用</h3><Times value={view.costs} />
      {view.costs.data.length === 0 && <p>当前快照无费用记录；不代表免费或无调用。</p>}
      <ul>{view.costs.data.map(cost => <li key={cost.ledgerId}>{cost.ledgerId} / {cost.state}；
        预留 {cost.currency} {cost.reserved}；估算 {cost.estimated ?? "unknown"}</li>)}</ul>
    </article>}
  </section>;
}
