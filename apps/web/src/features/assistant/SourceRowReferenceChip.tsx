import {useId} from 'react';
import './source-row-reference-chip.css';

type Props={ordinal:number;onRemove:()=>void;disabled?:boolean};

/** Presentation only: this component never receives the bound source token or the question. */
export function SourceRowReferenceChip({ordinal,onRemove,disabled=false}:Props){
 const id=useId();
 if(!Number.isSafeInteger(ordinal)||ordinal<1)return null;
 return <div className="assistant-source-reference" role="group" aria-labelledby={id+'-label'} aria-describedby={id+'-help'}>
  <div className="assistant-source-reference-text">
   <strong id={id+'-label'}>已引用采购入库单 · 本页第{ordinal}行</strong>
   <p id={id+'-help'}>引用只随你手动发送。移除引用不会改动问题草稿。</p>
  </div>
  <button type="button" disabled={disabled} onClick={onRemove}>移除入库单引用</button>
 </div>;
}
