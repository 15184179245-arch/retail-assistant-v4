import type {TrialCitation} from './piTrialApi';
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import type { Session } from "../../lib/auth/session";
import { getScopeVersion, subscribe } from "../../lib/auth/session";
import { PiTrialController, TRIAL_SAMPLE_SHA256, TRIAL_TEXT, trialSessionIsCurrent } from "./piTrialApi";
import type { Observation } from "./piTrialApi";

function Times({ value }: { value: Pick<Observation<unknown>, "startedAt" | "collectedAt"> }) {
  return <p>请求时间：{value.startedAt}；采集时间：{value.collectedAt}</p>;
}

export function TrialCitationContent({citation:c}:{citation:TrialCitation}) {
  if(c.kind==='product_image_result'||c.kind==='history_image_result')return <>{c.label}{c.observedAt&&<p>来源时间：{c.observedAt}</p>}</>;
  return <>{c.label} / {c.kind} / {c.objectId} / {c.version} / {c.versionRef}{c.sourceRef&&<p>来源：{c.sourceRef}</p>}{c.observedAt&&<p>来源时间：{c.observedAt}</p>}</>;
}
export function PiTrialPage({ session, baseUrl = "" }: { session: Session; baseUrl?: string }) {
  const scope = useSyncExternalStore(subscribe, getScopeVersion, getScopeVersion);
  const controller = useMemo(() => new PiTrialController(session, {
    getItem: key => window.localStorage.getItem(key),
    setItem: (key, value) => window.localStorage.setItem(key, value),
  }, baseUrl), [session, baseUrl, scope]);
  const view = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [input, setInput] = useState("");
  useEffect(() => { controller.start(); return () => controller.dispose(); }, [controller]);
  useEffect(() => { setInput(""); }, [controller]);
  if (!trialSessionIsCurrent(session)) return <p role="alert">身份已改变，旧试用内容已隐藏。请重新进入。</p>;
  const info = view.info?.data;
  const modeLabel = info?.mode === "real-readonly-trial" ? "真实模型只读试用（Host 已配置）" :
    info?.mode === "real-probe" ? "真实模型诊断（单次响应，非完整只读试用）" :
    info?.mode === "synthetic-only-v1" ? "匿名离线联调（合成模型）" : "模型模式待核";
  const configuredRun = info?.runId ?? "";
  return <section aria-labelledby="pi-trial-title" style={{ maxWidth: 900, margin: "24px auto", padding: 24 }}>
    <h2 id="pi-trial-title">Pi 只读试用</h2>
    <p>{modeLabel}。配置说明不表示模型已经执行，也不表示营业可用。</p>
    <p role="status">{view.infoNotice ?? "Host 配置待核；尚不可发送。"}</p>
    <p>仅发送下列固定匿名样本。未知、断线或刷新后仅回查原 Run；已从 CLI 发起的 Run 可直接只读回查。</p>
    <blockquote>{info?.sampleText ?? TRIAL_TEXT}</blockquote>
    <details><summary>技术信息与手工回查</summary>
    <p>样本 SHA-256：<code>{info?.sampleSha256 ?? TRIAL_SAMPLE_SHA256}</code></p>
    {info && <p>Host 配置有效期：{info.expiresAt}</p>}
    <label>预配置的原 Run UUID <input aria-label="预配置的原 Run UUID" value={view.runId || input || configuredRun}
      disabled={view.attempted || view.busy} spellCheck={false} autoComplete="off"
      onChange={event => setInput(event.target.value)} /></label>
    <button type="button" disabled={view.busy || view.attempted || !(input || configuredRun)}
      onClick={() => { void controller.readOriginal(input || configuredRun); }}>只回查输入的原 Run</button>
    </details>
    <p>仅显式发送一次；先持久保存尝试记录。不要重发或更换 UUID。</p>
    <button type="button" disabled={view.busy || view.infoBusy || view.attempted || !info?.startAvailable}
      onClick={() => { void controller.send(configuredRun); }}>发送一次固定样本</button>{" "}
    <button type="button" disabled={view.busy || !(view.runId || configuredRun)}
      onClick={() => { void (view.runId ? controller.refresh() : controller.readOriginal(configuredRun)); }}>仅回查原 Run</button>{" "}
    <button type="button" disabled={view.busy || !(view.runId || configuredRun)}
      onClick={() => { void (view.runId ? controller.refresh(true) : controller.readOriginal(configuredRun, true)); }}>回查原 Run 与独立费用</button>
    <p role="status">{view.notice}</p>
    <p>各 GET 独立采集，非原子快照。运行时登记不证明真实模型执行；不支持结果重放或来源到 Step 的关联。</p>
    {view.run && <article aria-label="普通 Run 与答案">
      <h3>原 Run 与答案</h3><p>运行状态：{view.run.data.state}</p><Times value={view.run} />
      <p>答案状态：{view.run.data.answerStatus}</p>
      {view.run.data.answerStatus === "available" ? <>
        <p style={{ whiteSpace: "pre-wrap" }}>{view.run.data.answer}</p>
        <h4>服务端来源</h4>
        {view.run.data.citations?.length === 0 && <p>服务端未提供知识或商品引用；不补造来源。</p>}
        <ul>{view.run.data.citations?.map((c, index) => <li key={index}>
          <TrialCitationContent citation={c}/>
        </li>)}</ul>
      </> : <p>答案尚未记录或答案／来源不可用；旧内容已隐藏。</p>}
    </article>}
    {view.runtime && <article aria-label="正式 Pi 运行时回执">
      <h3>正式 Pi 运行时回执</h3><p>运行状态：{view.runtime.data.state}</p><Times value={view.runtime} />
      <details><summary>展开运行时技术回执</summary>
      <p>原 Run UUID：<code>{view.runtime.data.runId}</code></p>
      <p>版本清单摘要：<code>{view.runtime.data.manifest}</code>；执行证明：not_proven</p>
      <ol>{view.runtime.data.steps.map(step => <li key={step.id}>Step {step.id}：{step.kind} / {step.state}</li>)}</ol>
      <h4>原生工具与 Host 收据</h4>
      <ul>{view.runtime.data.natives.map(call => <li key={call.receiptId}>
        {call.tool} / {call.state}；native {call.nativeCallId}；收据 {call.receiptId}；
        模型 Step {call.modelStepId}；工具 Step {call.toolStepId ?? "尚未关联"}；输出摘要 {call.outputRef ?? "unknown"}
      </li>)}</ul>
      <h4>模型调用收据</h4>
      {view.runtime.data.providers.map(call => <dl key={call.receiptId}>
        <dt>Provider 收据</dt><dd>{call.receiptId} / {call.state}</dd>
        <dt>模型 Step / attempt / ledger</dt><dd>{call.stepId} / {call.attemptId} / {call.ledgerId}</dd>
        <dt>响应摘要 / 消息摘要</dt><dd>{call.responseDigest ?? "unknown"} / {call.messageDigest ?? "unknown"}</dd>
        <dt>Provider request ID 摘要</dt><dd>{call.requestIdDigest ?? "unknown"}</dd>
        <dt>原始 usage 字段存在性</dt><dd>{call.usagePresence}</dd>
      </dl>)}
      </details>
    </article>}
    {view.costNotice && <p>{view.costNotice}</p>}
    {view.costRead && <Times value={view.costRead} />}
    {view.costs && <article aria-label="独立费用回执"><h3>独立费用</h3><Times value={view.costs} />
      <p>保守估算不是实际 Go 扣费；provider_key 不证明真实 Go 请求。</p>
      {view.costs.data.length === 0 && <p>当前快照无费用记录；不代表免费或无调用。</p>}
      <ul>{view.costs.data.map(cost => <li key={cost.ledgerId}>{cost.ledgerId} / {cost.state}；
        预留 {cost.currency} {cost.reserved}；估算 {cost.estimated ?? "unknown"}</li>)}</ul>
    </article>}
  </section>;
}
