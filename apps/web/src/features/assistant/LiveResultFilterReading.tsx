import {Component,type ReactNode} from 'react';
import type {ResultSnapshot,ResultTable} from './publicResultSnapshot';
import {liveFilterContinuation,liveFilterFrame,type LiveFilterWitness} from './liveResultFilterFocus';

type Focus={element:HTMLInputElement;source:Pick<ResultSnapshot,'work_id'|'run_id'|'snapshot_sha256'>;witness:LiveFilterWitness};
type Props={
 live:boolean;snapshot:ResultSnapshot;focused:Focus|null;
 lookup:(snapshot:ResultSnapshot,block:ResultTable)=>HTMLInputElement|undefined;
 children:ReactNode;
};
type ReadingPosition={scroller:HTMLElement;scrollTop:number;fieldTop:number};
type Anchor={old:HTMLInputElement;next:ResultTable;position:ReadingPosition|null};

function readingPosition(field:HTMLInputElement):ReadingPosition|null{
 const box=field.getBoundingClientRect();
 for(let scroller=field.parentElement;scroller;scroller=scroller.parentElement){
  if(scroller.scrollHeight<=scroller.clientHeight||!['auto','scroll','overlay'].includes(getComputedStyle(scroller).overflowY))continue;
  const visible=scroller.getBoundingClientRect();
  return box.bottom>Math.max(0,visible.top)&&box.top<Math.min(innerHeight,visible.bottom)
   ?{scroller,scrollTop:scroller.scrollTop,fieldTop:box.top}:null;
 }
 return null;
}

/** Measure before DOM mutation so an inserted directory cannot displace an actively read source. */
export class LiveResultFilterReading extends Component<Props,Record<string,never>,Anchor|null>{
 getSnapshotBeforeUpdate(previous:Props):Anchor|null{
  const {focused}=previous;
  if(!this.props.live||!previous.live||!focused||document.activeElement!==focused.element||
     document.visibilityState==='hidden'||!document.hasFocus())return null;
  const next=liveFilterContinuation(liveFilterFrame(previous.snapshot),this.props.snapshot,focused.source,focused.witness);
  return next?{old:focused.element,next,position:readingPosition(focused.element)}:null;
 }
 componentDidUpdate(_previous:Props,_state:Record<string,never>,anchor:Anchor|null){
  if(!anchor||anchor.old.isConnected||document.activeElement!==document.body||
     document.visibilityState==='hidden'||!document.hasFocus())return;
  const target=this.props.lookup(this.props.snapshot,anchor.next);
  if(!target)return;
  target.focus({preventScroll:true});
  const p=anchor.position;
  if(!p?.scroller.contains(target)||Math.abs(p.scroller.scrollTop-p.scrollTop)>1)return;
  const delta=target.getBoundingClientRect().top-p.fieldTop;
  if(Math.abs(delta)>1)p.scroller.scrollTop+=delta;
 }
 render(){return this.props.children;}
}
