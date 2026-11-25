import type {Work} from './assistantApi';
import {receiptPresence,restartChoices,type Presence} from './companionState';
export function CompanionInbox({items,open,presences={}}:{items:Work[];presences?:Record<string,Presence>;open:(id:string)=>void}){
 const choices=restartChoices(items,presences);if(!choices.length)return null;
 return <div className="acp-resume" aria-label="继续之前的对话"><h3>接着上次聊</h3>{choices.map(w=>{const s=presences[w.work_id]??receiptPresence(w);return <button key={w.work_id} type="button" onClick={()=>open(w.work_id)}>
 <span className={'acp-badge is-'+s.tone}>{s.label}</span><span className="acp-title">{w.title}</span><span aria-hidden="true">›</span></button>})}</div>;
}
