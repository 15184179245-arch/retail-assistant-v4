import type {ResultTable} from './publicResultSnapshot';
const normalize=(text:string)=>text.normalize('NFKC').toLocaleLowerCase('zh-CN');
/** Local display only. Preserve original cell values, ordering, and source row IDs. */
export function filterResultRows(rows:ResultTable['rows'],query:string):ResultTable['rows'] {
 const terms=normalize(query).trim().split(/\s+/u).filter(Boolean);
 if(!terms.length)return rows;
 return rows.filter(row=>{
  const cells=row.cells.map(v=>normalize(v===null?'未提供':typeof v==='boolean'?(v?'是':'否'):String(v)));
  return terms.every(term=>cells.some(value=>value.includes(term)));
 });
}
