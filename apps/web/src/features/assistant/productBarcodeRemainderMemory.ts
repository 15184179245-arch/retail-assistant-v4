import {canReadProductRemainder,type ProductBarcodePage,type ProductBarcodeRemainderPage} from './productBarcodePage';
import type {ResultSnapshot} from './publicResultSnapshot';

export type ProductRemainderProgress={pages:ProductBarcodeRemainderPage[];through:number;cursor:string|null;recordedAt:string;archiveProof:string|null};
export type ProductRemainderReading={progress:ProductRemainderProgress;pageIndex:number};
type AnchorGuard={snapshotDigest:string;anchorId:string};
type Retained=AnchorGuard&{reading:ProductRemainderReading};
type Checked=Readonly<{origin:string;anchor:ProductBarcodePage;reading:ProductRemainderReading}>;

/** Current scoped view only. Holding rows is never permission to render them during a recheck. */
export class ProductBarcodeRemainderMemory {
 private readonly entries=new Map<string,Retained>();
 private readonly active=new Map<string,AnchorGuard>();
 private readonly interrupted=new Map<string,AnchorGuard>();

 clear(){this.entries.clear();this.active.clear();this.interrupted.clear();}
 get hasReading(){return this.entries.size>0;}
 get hasInterrupted(){return this.interrupted.size>0;}

 private matches(entry:AnchorGuard|undefined,snapshotDigest:string,anchorId:string):boolean{
  return entry?.snapshotDigest===snapshotDigest&&entry.anchorId===anchorId;
 }

 activity(snapshotDigest:string,anchor:ProductBarcodePage,reading:boolean){
  const origin=anchor.origin_block_id;
  if(reading){
   this.interrupted.delete(origin);
   this.active.set(origin,{snapshotDigest,anchorId:anchor.block_id});
  }else if(this.matches(this.active.get(origin),snapshotDigest,anchor.block_id)){
   this.active.delete(origin);
  }
 }

 pauseForRecheck(){
  for(const [origin,guard] of this.active)this.interrupted.set(origin,guard);
  this.active.clear();
 }

 wasInterrupted(snapshotDigest:string,anchor:ProductBarcodePage){
  return this.matches(this.interrupted.get(anchor.origin_block_id),snapshotDigest,anchor.block_id);
 }

 save(snapshotDigest:string,anchor:ProductBarcodePage,reading:ProductRemainderReading|null){
  const origin=anchor.origin_block_id;
  this.entries.delete(origin);
  if(!reading?.progress.archiveProof)return;
  this.entries.set(origin,{snapshotDigest,anchorId:anchor.block_id,reading});
  if(this.entries.size>3){
   const oldest=this.entries.keys().next();if(!oldest.done)this.entries.delete(oldest.value);
  }
 }

 // Called only for a snapshot cleared for display, before the strict checks.
 candidates(snapshot:ResultSnapshot):Checked[]{
  const anchors=new Map<string,ProductBarcodePage>();
  if(snapshot.state==='available')for(const block of snapshot.blocks){
   if(block.type==='product_barcode_page'&&canReadProductRemainder(block))anchors.set(block.origin_block_id,block);
  }
  for(const [origin,guard] of this.interrupted){
   const anchor=anchors.get(origin);
   if(!anchor||!this.matches(guard,snapshot.snapshot_sha256,anchor.block_id))this.interrupted.delete(origin);
  }
  const matches:Checked[]=[];
  for(const [origin,entry] of this.entries){
   const anchor=anchors.get(origin);
   if(entry.snapshotDigest!==snapshot.snapshot_sha256||anchor?.block_id!==entry.anchorId){
    this.entries.delete(origin);continue;
   }
   matches.push({origin,anchor,reading:entry.reading});
  }
  return matches;
 }

 keepOnly(matches:Set<string>){
  for(const key of this.entries.keys())if(!matches.has(key)){
   this.entries.delete(key);
   this.interrupted.delete(key);
  }
 }

 reading(snapshotDigest:string,anchor:ProductBarcodePage):ProductRemainderReading|null{
  const entry=this.entries.get(anchor.origin_block_id);
  return entry?.snapshotDigest===snapshotDigest&&entry.anchorId===anchor.block_id?entry.reading:null;
 }
}
