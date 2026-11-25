const panelKey=(work:string,run:string)=>JSON.stringify([work,run]);
const limit=32;

/** One assistant access scope in this tab only; contains no result, query, or storage. */
export class FoldedSourcePanels {
 private readonly folded=new Set<string>();
 has(work:string,run:string):boolean{return this.folded.has(panelKey(work,run));}
 set(work:string,run:string,folded:boolean):void{
  const key=panelKey(work,run);
  this.folded.delete(key);
  if(!folded)return;
  this.folded.add(key);
  if(this.folded.size>limit){
   const oldest=this.folded.values().next();
   if(!oldest.done)this.folded.delete(oldest.value);
  }
 }
}
