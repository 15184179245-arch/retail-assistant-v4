/** Same-tab unsent text only. Never stores tokens, source values, files or request receipts. */
type Scope = {organizationId:string;storeId:string;subjectId:string};
type Entry = {work:string;text:string;updatedAt:number};
type Checkpoint = {scope:string;version:string;drafts:Entry[]};
type StorageLike = Pick<Storage,'getItem'|'setItem'|'removeItem'>;
const key = 'v4-assistant-unsent-text-v1';
const hour = 60 * 60 * 1000;
// This is a bounded local allowance, not the independently negotiated send limit.
// At most eight such drafts remain within this tab's storage envelope.
export const assistantCheckpointTextLimit = 128 * 1024;
const uuid = /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/;
const validWork = (work:unknown):work is string => typeof work==='string'&&(work==='new'||uuid.test(work));
const scopeId = (scope:Scope) => JSON.stringify([scope.organizationId,scope.storeId,scope.subjectId]);
function browserStorage():StorageLike|undefined {
 try { return typeof window==='undefined'?undefined:window.sessionStorage; } catch { return undefined; }
}
function read(storage:StorageLike):Checkpoint|null {
 const raw=storage.getItem(key);
 if(!raw)return null;
 const value=JSON.parse(raw) as Checkpoint;
 if(!value||typeof value.scope!=='string'||typeof value.version!=='string'||!Array.isArray(value.drafts)||value.drafts.length>8||
    value.drafts.some(e=>!e||!validWork(e.work)||typeof e.text!=='string'||e.text.length>assistantCheckpointTextLimit||
                        !Number.isSafeInteger(e.updatedAt))) {
  return null;
 }
 return value;
}
function matching(storage:StorageLike, scope:Scope, version:string, now:number):Entry[] {
 const value=read(storage);
 if(!value)return [];
 if(value.scope!==scopeId(scope)||value.version!==version)return [];
 return value.drafts.filter(e=>e.updatedAt<=now&&now-e.updatedAt<hour);
}
export function clearDraftCheckpoint(scope?:Scope,storage=browserStorage()):void {
 try { if(storage&&(!scope||read(storage)?.scope===scopeId(scope)))storage.removeItem(key); } catch { /* in-memory draft unaffected */ }
}
export function reconcileDraftCheckpointScope(scope:Scope,storage=browserStorage()):void {
 try { if(storage&&read(storage)?.scope!==scopeId(scope))storage.removeItem(key); } catch { clearDraftCheckpoint(undefined,storage); }
}
export function reconcileDraftCheckpointVersion(scope:Scope,version:string,storage=browserStorage()):void {
 try {
  const value=storage&&read(storage);
  if(value&&value.scope===scopeId(scope)&&value.version!==version)storage?.removeItem(key);
 } catch { clearDraftCheckpoint(undefined,storage); }
}
/** Read-only metadata so the UI can stop promising a recoverable copy when it expires. */
export function readDraftCheckpointInfo(scope:Scope,version:string,work:string,storage=browserStorage(),now=Date.now()):
 {text:string;expiresAt:number}|null {
 try {
  if(!storage||!validWork(work))return null;
  const entry=matching(storage,scope,version,now).find(e=>e.work===work);
  return entry?{text:entry.text,expiresAt:entry.updatedAt+hour}:null;
 } catch { return null; }
}
export function readDraftCheckpoint(scope:Scope,version:string,work:string,storage=browserStorage(),now=Date.now()):string|null {
 return readDraftCheckpointInfo(scope,version,work,storage,now)?.text??null;
}
export function saveDraftCheckpoint(scope:Scope,version:string,work:string,text:string,storage=browserStorage(),now=Date.now()):boolean {
 try {
  if(!storage||!validWork(work)||!version||text.length>assistantCheckpointTextLimit)return false;
  const others = matching(storage,scope,version,now).filter(e=>e.work!==work);
  const save = text.length>0;
  const drafts = (save?[{work,text,updatedAt:now},...others]:others).slice(0,8);
  if(!drafts.length)storage.removeItem(key);
  else storage.setItem(key,JSON.stringify({scope:scopeId(scope),version,drafts}));
  return save;
 } catch { /* A failed update must not erase the previous recoverable envelope. */ return false; }
}


/** A user's deliberate choice must not replace a newer or different draft. */
export function replaceDraftCheckpoint(scope:Scope,version:string,work:string,expected:string,text:string,
                                       storage=browserStorage(),now=Date.now()):boolean {
 try {
  if(!storage||!validWork(work)||!version||!text.length||text.length>assistantCheckpointTextLimit)return false;
  const drafts=matching(storage,scope,version,now);
  if(drafts.find(e=>e.work===work)?.text!==expected)return false;
  storage.setItem(key,JSON.stringify({scope:scopeId(scope),version,
    drafts:[{work,text,updatedAt:now},...drafts.filter(e=>e.work!==work)].slice(0,8)}));
  return true;
 } catch { return false; }
}
