import {Component,createRef,type ReactNode} from 'react';
import type {ResultBlock,ResultSnapshot} from './publicResultSnapshot';

type Props={live:boolean;snapshot:ResultSnapshot;children:ReactNode};
type Reading={scroller:HTMLElement;scrollTop:number;cardTop:number;index:number};

function matchingSource(a:ResultBlock,b:ResultBlock|undefined):boolean{
 if(!b||a.type!==b.type||a.block_id!==b.block_id||a.sequence_no!==b.sequence_no)return false;
 if(a.type==='report_table')return b.type==='report_table'&&a.source.canonical_ref===b.source.canonical_ref&&
  a.source.response_sha256===b.source.response_sha256;
 return true;
}
function cards(section:HTMLElement,count:number){
 const matches=section.querySelectorAll<HTMLElement>('article.ar-card');
 return matches.length===count?matches:null;
}
function scrollOwner(section:HTMLElement){
 for(let node=section.parentElement;node;node=node.parentElement){
  if(node.scrollHeight>node.clientHeight&&['auto','scroll','overlay'].includes(getComputedStyle(node).overflowY))return node;
 }
 return null;
}

/** Keep the already-read card in place when a new authorized source adds content above it.
 * Does not preserve any old snapshot outside React's current DOM update, or move focus.
 */
export class AssistantLiveSourceReading extends Component<Props,Record<string,never>,Reading|null>{
 private readonly section=createRef<HTMLElement>();
 getSnapshotBeforeUpdate(previous:Props):Reading|null{
  const section=this.section.current,next=this.props.snapshot,prior=previous.snapshot;
  if(!section||!previous.live||!this.props.live||document.visibilityState==='hidden'||
     prior.state!=='available'||next.state!=='available'||!prior.run_id||prior.work_id!==next.work_id||prior.run_id!==next.run_id||
     prior.snapshot_sha256===next.snapshot_sha256||next.sequence_no<=prior.sequence_no||next.blocks.length<=prior.blocks.length)return null;
  const scroller=scrollOwner(section),shown=cards(section,prior.blocks.length);
  if(!scroller||!shown||!scroller.scrollTop||
     (document.activeElement!==document.body&&document.activeElement!==scroller))return null;
  const viewport=scroller.getBoundingClientRect(), area=section.getBoundingClientRect();
  const topbar=section.closest('.assistant-main')?.querySelector('.assistant-topbar');
  const top=Math.max(0,viewport.top,area.top,topbar?.getBoundingClientRect().bottom??0);
  const bottom=Math.min(innerHeight,viewport.bottom,area.bottom);
  const center=(Math.max(0,viewport.left,area.left)+Math.min(innerWidth,viewport.right,area.right))/2;
  if(top>=bottom)return null;
  for(const y of [top+Math.min(30,(bottom-top)/4),(top+bottom)/2]){
   const visible=document.elementFromPoint(center,y)?.closest<HTMLElement>('article.ar-card');
   const index=visible?Array.prototype.indexOf.call(shown,visible):-1;
   if(!visible||index<0||!matchingSource(prior.blocks[index],next.blocks[index]))continue;
   return {scroller,scrollTop:scroller.scrollTop,cardTop:visible.getBoundingClientRect().top,index};
  }
  return null;
 }
 componentDidUpdate(_previous:Props,_state:Record<string,never>,reading:Reading|null){
  const section=this.section.current;
  if(!section||!reading||!reading.scroller.contains(section)||
     (document.activeElement!==document.body&&document.activeElement!==reading.scroller)||
     Math.abs(reading.scroller.scrollTop-reading.scrollTop)>1)return;
  const card=cards(section,this.props.snapshot.blocks.length)?.[reading.index];
  if(!card)return;
  const delta=card.getBoundingClientRect().top-reading.cardTop;
  if(Math.abs(delta)>1)reading.scroller.scrollTop+=delta;
 }
 render(){return <section ref={this.section} aria-label="业务查询结果" className="ar-results">{this.props.children}</section>;}
}
