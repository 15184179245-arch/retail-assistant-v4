import type {ResultSnapshot} from './publicResultSnapshot';

type ScopeSource=Pick<ResultSnapshot,'work_id'|'run_id'|'snapshot_sha256'|'state'>;
type FilterIdentity={work:string;run:string|null;digest:string};
export type ResultFilterMemory={identity:FilterIdentity;queries:ReadonlyMap<string,string>} | null;

function isSame(memory:ResultFilterMemory,source:ScopeSource):memory is NonNullable<ResultFilterMemory>{
  return Boolean(memory&&source.state==='available'&&memory.identity.work===source.work_id&&
    memory.identity.run===source.run_id&&memory.identity.digest===source.snapshot_sha256);
}

/** null denotes a recheck in progress; it is not permission to show the old table. */
export function reconcileResultFilters(memory:ResultFilterMemory,source:ScopeSource|null):ResultFilterMemory{
  return source===null||isSame(memory,source)?memory:null;
}

export function readResultFilter(memory:ResultFilterMemory,source:ScopeSource,block:string):string{
  return isSame(memory,source)?memory.queries.get(block)??'':'';
}

export function rememberResultFilter(
  memory:ResultFilterMemory,source:ScopeSource,block:string,value:string,
):ResultFilterMemory{
  if(source.state!=='available')return null;
  const queries=new Map(isSame(memory,source)?memory.queries:undefined);
  if(value)queries.set(block,value);else queries.delete(block);
  if(!queries.size)return null;
  return {identity:{work:source.work_id,run:source.run_id,digest:source.snapshot_sha256},queries};
}
