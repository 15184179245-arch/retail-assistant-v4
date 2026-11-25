import type {ResultSnapshot} from './publicResultSnapshot';
import type {ReceiptDetailChoiceRef} from './businessChoice';

export type SourceRowRef={
 kind:'authorized_result_row';
 source_run_id:string;
 block_id:string;
 snapshot_sha256:string;
 row_index:number;
};
export type DraftSourceRow={rootWorkId:string;reference:SourceRowRef;anchor:string};
export type ScopedDraftSourceRow=DraftSourceRow&{scopeKey:string};
export type SourceRowVisibility={rootWorkId:string;runId:string;state:'checking'|'closed'|'available'|'unavailable';snapshotSha256:string|null};
export function sourceRowVisibility(binding:DraftSourceRow|null,proof:SourceRowVisibility|null):'checking'|'closed'|'current'|'invalid'{
 if(!binding||!proof||proof.rootWorkId!==binding.rootWorkId||proof.runId!==binding.reference.source_run_id||
    proof.state==='checking')return 'checking';
 if(proof.state==='closed')return 'closed';
 if(proof.state!=='available'||proof.snapshotSha256!==binding.reference.snapshot_sha256)return 'invalid';
 return 'current';
}
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
const sha=/^[0-9a-f]{64}$/u;
export function validSourceRowRef(value:SourceRowRef):boolean{
 return value.kind==='authorized_result_row'&&uuid.test(value.source_run_id)&&uuid.test(value.block_id)&&
  sha.test(value.snapshot_sha256)&&Number.isSafeInteger(value.row_index)&&value.row_index>=0&&value.row_index<500;
}
export function selectedPurchaseSource(fresh:ResultSnapshot,ref:ReceiptDetailChoiceRef):DraftSourceRow|null{
 if(fresh.state!=='available'||fresh.snapshot_sha256!==ref.digest||!fresh.run_id||!uuid.test(fresh.work_id))return null;
 const page=fresh.blocks.find(b=>b.block_id===ref.blockId&&b.type==='operational_receipt_page'&&b.action==='purchase_receipts_query');
 if(page?.type!=='operational_receipt_page'||ref.rowIndex>=page.items.length)return null;
 const reference:SourceRowRef={kind:'authorized_result_row',source_run_id:fresh.run_id,block_id:ref.blockId,
  snapshot_sha256:fresh.snapshot_sha256,row_index:ref.rowIndex};
 if(!validSourceRowRef(reference))return null;
 return {rootWorkId:fresh.work_id,reference,anchor:receiptCoordinates(page.sequence_no,page.offset,ref.rowIndex)};
}
/** A fresh pre-send read must identify the exact same authorized row, not just any page. */
export function snapshotMatchesDraftSource(snapshot:ResultSnapshot,source:DraftSourceRow):boolean {
 const ref=source.reference;
 if(snapshot.work_id!==source.rootWorkId||snapshot.run_id!==ref.source_run_id||!validSourceRowRef(ref))return false;
 const picked=selectedPurchaseSource(snapshot,{kind:'purchase_receipt_detail',digest:ref.snapshot_sha256,
  blockId:ref.block_id,rowIndex:ref.row_index});
 return picked!==null&&picked.anchor===source.anchor;
}
export const receiptCoordinates=(sequence:number,offset:number,rowIndex:number)=>
 `执行步骤 ${sequence}，来源偏移 ${offset}，本页第 ${rowIndex+1} 行`;

export function activeDraftSourceRow(binding:ScopedDraftSourceRow|null,scopeKey:string|null,text:string){
 return binding&&scopeKey!==null&&binding.scopeKey===scopeKey&&text.includes(binding.anchor)?binding:null;
}
/** Rebuild exact protocol fields; never serialize UI scope, the user text anchor or caller-supplied extras. */
export const wireSourceRowRef=(value:SourceRowRef):SourceRowRef=>({kind:value.kind,source_run_id:value.source_run_id,
 block_id:value.block_id,snapshot_sha256:value.snapshot_sha256,row_index:value.row_index});
