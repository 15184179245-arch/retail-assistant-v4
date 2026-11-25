import {useId,useState} from 'react';
import {choiceTitles,sourceRowChoice,type ChoiceRef} from './businessChoice';
import type {ResultTable} from './publicResultSnapshot';

export function BusinessChoiceCards({block,digest,onChoose}:{block:ResultTable;digest:string;onChoose?:(ref:ChoiceRef)=>void}) {
 const [selected,setSelected]=useState(0), id=useId();
 const options=block.rows.map((_r,i)=>({i,c:sourceRowChoice(block,i)})).filter(v=>v.c!==null),active=options.find(v=>v.i===selected)??options[0];
 if(!active || !active.c)return <p>本页没有可核对的来源记录。</p>;
 const c=active.c;
 return <div className="ar-next">
 <h4>接下来核对什么？</h4><p>从这页真实返回的记录中选一行，再选择要问的问题。只加入当前输入框，不会自动发送或改动单据。</p>
 <label htmlFor={id}>选择来源记录</label><select id={id} value={active.i} onChange={e=>setSelected(Number(e.target.value))}>
 {options.map(({i,c:x})=><option key={x!.rowId} value={i}>第 {i+1} 行 · {x!.name}</option>)}</select>
 <p>{c.detail||'仅显示本次来源页已有的字段。'}</p>
 <div className="ar-next-cards">{c.kinds.map(k=><button key={k} type="button" disabled={!onChoose} onClick={()=>onChoose?.({digest,blockId:block.block_id,rowId:c.rowId,kind:k})}>
 <strong>{choiceTitles[k]}</strong><small>加入追问草稿</small></button>)}</div>
 {!onChoose&&<p>当前工作不能追加追问，可查看明细和依据。</p>}
 </div>;
}
