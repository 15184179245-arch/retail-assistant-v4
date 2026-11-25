import type {ResultSnapshot,ResultTable} from './publicResultSnapshot';

type Scope=Pick<ResultSnapshot,'work_id'|'run_id'|'snapshot_sha256'>;
type Frame=Scope&{sequence:number;count:number};
export type LiveFilterWitness={index:number;source:string};

export function liveFilterFrame(snapshot:ResultSnapshot|null):Frame|null {
 if(snapshot?.state!=='available')return null;
 const {work_id,run_id,snapshot_sha256,sequence_no,blocks}=snapshot;
 return {work_id,run_id,snapshot_sha256,sequence:sequence_no,count:blocks.length};
}

export function liveFilterWitness(block:ResultTable,index:number):LiveFilterWitness {
 return {index,source:JSON.stringify([block.block_id,block.sequence_no,block.source.action,
  block.source.canonical_ref,block.source.response_sha256])};
}

/** Only identify the same earlier control in a newly authorized append; no filter terms or result rows are retained. */
export function liveFilterContinuation(previous:Frame|null,next:ResultSnapshot|null,scope:Scope,witness:LiveFilterWitness):ResultTable|null {
 if(!previous||next?.state!=='available'||!previous.run_id||
    previous.work_id!==scope.work_id||previous.run_id!==scope.run_id||previous.snapshot_sha256!==scope.snapshot_sha256||
    previous.work_id!==next.work_id||previous.run_id!==next.run_id||previous.snapshot_sha256===next.snapshot_sha256||
    next.sequence_no<=previous.sequence||next.blocks.length<=previous.count||witness.index>=previous.count)return null;
 const block=next.blocks[witness.index];
 return block?.type==='report_table'&&liveFilterWitness(block,witness.index).source===witness.source?block:null;
}
