import type {Work} from './assistantApi';
function preview(value:string){let text=value.slice(0,180);if(/[\uD800-\uDBFF]$/.test(text))text=text.slice(0,-1);return text.length<value.length?text+'…':text;}
/** Saved receipt states only. A new draft is never a replay of the old request. */
export function RecoveryPanel({messages,currentUnresolved,busy,canStart,canManage,onRefresh,onNew,onCancel,onCarry,onCorrect}:{messages:Work[];currentUnresolved:boolean;busy:boolean;canStart:boolean;canManage:boolean;onRefresh:()=>void;onNew:()=>void;onCancel:(id:string)=>void;onCarry:(id:string,text:string)=>void;onCorrect?:(id:string)=>void}){
 const pending=messages.filter(m=>m.inbox_intake==='received'),cancelled=messages.filter(m=>m.inbox_intake==='cancelled');
 if(!currentUnresolved&&!pending.length&&!cancelled.length)return null;
 return <section className="assistant-recovery-panel" aria-label="工作恢复与未执行消息">
  <h2>{currentUnresolved?'上一轮需要核对':pending.length?'还有消息等待处理':'已取消消息保留在这里'}</h2>
  {currentUnresolved&&<p>部分结果尚未核实，不等于完全没有执行。查看记录不会重新执行，系统不会替你重发。</p>}
  {!!pending.length&&<p>{pending.length}条消息已收到、尚未执行。另起工作不会自动取消它们；承接原文前，请先取消原收件并核对回执。</p>}
  <div className="assistant-recovery-actions"><button type="button" disabled={busy} onClick={onRefresh}>只刷新原记录</button><button type="button" disabled={!canStart} onClick={onNew}>新建独立工作</button></div>
  {!!pending.length&&onCorrect&&<p>需要更正？“取消并修改”会先取消尚未执行的旧补充，确认后只预填本对话草稿，不会自动发送或打断当前工作。</p>}
  {!!pending.length&&<details><summary>查看未执行消息（{pending.length}）</summary><ol>{pending.map(message=><li key={message.work_id}><p>{preview(message.text)}</p><small>已保存 · 尚未执行</small><button type="button" disabled={!canStart||!canManage} onClick={()=>onCancel(message.work_id)}>取消这条收件</button>{onCorrect&&!message.images?.length&&<button type="button" disabled={!canStart||!canManage} onClick={()=>onCorrect(message.work_id)}>取消并修改这条补充</button>}</li>)}</ol></details>}
  {!!cancelled.length&&<details><summary>查看已确认取消的消息（{cancelled.length}）</summary><ol>{cancelled.map(message=><li key={message.work_id}><p>{preview(message.text)}</p><small>取消已确认 · 原记录保留</small>{!message.images?.length&&<button type="button" disabled={!canStart} onClick={()=>onCarry(message.work_id,message.text)}>带原文新建工作</button>}{onCorrect&&!message.images?.length&&<button type="button" disabled={!canStart||!canManage} onClick={()=>onCorrect(message.work_id)}>带原文在本对话修改</button>}</li>)}</ol><p>只预填文字，不发送、不删除原记录。需要继续时，由你再确认发送。</p></details>}
 </section>;
}
