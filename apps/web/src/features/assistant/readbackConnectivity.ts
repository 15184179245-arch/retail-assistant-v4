import {ApiError} from '../../lib/api/client';

export type ReadbackConnection='current'|'offline'|'checking'|'not_reached';
export const browserOffline=()=>typeof navigator!=='undefined'&&navigator.onLine===false;

/** An unavailable resource may deliberately carry 404, not a semantic permission diagnosis. */
export function readbackNoLongerAccessible(error:unknown):boolean {
 return error instanceof ApiError&&[401,403,404,409,410].includes(error.status);
}
export function retryableReadbackDisconnect(error:unknown):boolean {
 return error instanceof ApiError&&((error.status===0&&['NETWORK_ERROR','TIMEOUT'].includes(error.code))||
  [408,429,500,502,503,504].includes(error.status));
}
export function readbackConnectionHint(connection:ReadbackConnection,showingOld:boolean):string {
 if(connection==='current')return '';
 const old=showingOld?'这里保留上次核对的来源，可能不是最新。':'';
 if(connection==='offline')return '浏览器报告离线。'+old+'联网后只核对原结果。';
 if(connection==='checking')return '已联网，正在核对原结果。'+old;
 return '暂未能重新核对原结果。'+old+'不会重新执行业务。';
}
