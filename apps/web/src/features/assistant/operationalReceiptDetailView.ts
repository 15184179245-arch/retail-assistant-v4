import type {OperationalReceiptDetailLine} from './operationalReceiptDetail';

export type OperationalDetailView={query:string;pageIndex:number};
export const initialOperationalDetailView:OperationalDetailView={query:'',pageIndex:0};
const itemsPerPage=25;
const normalize=(v:string)=>v.normalize('NFKC').toLocaleLowerCase('zh-CN');

/** One saved document only; preserve original presentation values and row ordinals. */
export function operationalDetailPage(items:OperationalReceiptDetailLine[],view:OperationalDetailView){
 const tokens=normalize(view.query).trim().split(/\s+/u).filter(Boolean);
 const matches:{line:OperationalReceiptDetailLine;ordinal:number}[]=[];
 for(const [index,line] of items.entries()){
  if(tokens.length){
   const searchable=[line.product_code??'',line.product_name??'',line.specification??''].map(normalize);
   if(!tokens.every(token=>searchable.some(value=>value.includes(token))))continue;
  }
  matches.push({line,ordinal:index+1});
 }
 const pageCount=Math.max(1,Math.ceil(matches.length/itemsPerPage));
 const pageIndex=Math.min(pageCount-1,Math.max(0,Number.isSafeInteger(view.pageIndex)?view.pageIndex:0));
 const first=pageIndex*itemsPerPage;
 return {rows:matches.slice(first,first+itemsPerPage),matchCount:matches.length,filtered:tokens.length>0,
  pageCount,pageIndex,first:first+1,last:Math.min(first+itemsPerPage,matches.length)};
}
