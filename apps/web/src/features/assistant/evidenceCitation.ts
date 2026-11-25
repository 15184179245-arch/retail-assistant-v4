/** Projection of already-authorized public answer citations. Never fetches sources. */
export interface EvidenceCitation { kind: string; objectId: string; version: string; label: string; observedAt?: string | null }
function field(value: unknown, max: number): string {
  if (typeof value !== 'string' || [...value].length > max) throw Error('答复来源异常。');
  return value;
}
export function parseObservationTime(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!m) return null;
  const [,y,mo,d,hh,mm,ss] = m, year=Number(y), month=Number(mo), day=Number(d);
  if (year<1000 || month<1 || month>12 || day<1 || day>new Date(Date.UTC(year,month,0)).getUTCDate() || Number(hh)>23 || Number(mm)>59 || Number(ss)>59 || !Number.isFinite(Date.parse(value))) return null;
  return value;
}
export function parseEvidenceCitation(value: unknown): EvidenceCitation {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('答复来源异常。');
  const c=value as Record<string,unknown>;
  // Authorized Work reads can include these two compact v1 source citations
  // without display text or timestamps. Neither is a content reference.
  if((c.kind==='guanjia_endpoint'||c.kind==='input_image')&&!Object.hasOwn(c,'label')){
    const keys=['kind','object_id','version_ref','version'];
    if(Object.keys(c).length!==keys.length||!keys.every(key=>Object.hasOwn(c,key))||c.version!=='1'||
       typeof c.object_id!=='string'||c.object_id.length!==36||!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u.test(c.object_id)||
       typeof c.version_ref!=='string'||c.version_ref.length!==64||!/^[0-9a-f]{64}$/u.test(c.version_ref))throw Error('答复来源异常。');
    const label=c.kind==='guanjia_endpoint'?'已保存的管家婆接口来源（不含原始返回）':'用户上传的图片（仅来源说明）';
    return {kind:c.kind,objectId:c.object_id,version:c.version_ref,label,observedAt:null};
  }
  return {kind:field(c.kind,100),objectId:field(c.object_id,200),version:field(c.version_ref,200),label:field(c.label,1000),observedAt:parseObservationTime(c.observed_at)};
}
export function observationTimeLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZoneName:'short'}).format(new Date(value));
}
