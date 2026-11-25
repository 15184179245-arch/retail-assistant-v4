import {useId,useMemo,useState,type ReactNode} from 'react';
import type {ProductBarcodePage,ProductBarcodeRemainderPage,ProductUnitRow} from './productBarcodePage';
import './product-barcode-page.css';

type VisibleRow=ProductUnitRow|{kind:'barcode';unit:string|undefined;ordinal:number;code:string};
const showing=60;
function status(row:ProductUnitRow):string{
 if(row.barcode_state==='not_reported')return '来源未提供可核的条码，不按零条计。';
 if(row.barcode_state==='partial')return '仅部分条码可核对，已核 '+row.verified_barcode_count+' 条。';
 return '原件该单位已核 '+row.verified_barcode_count+' 个条码位置。';
}

/** A bounded local reader for one saved source page, never a new lookup. */
export function ProductBarcodePageCard({page,hasSavedSuccessor,remainder}:{page:ProductBarcodePage;hasSavedSuccessor:boolean;remainder?:ReactNode}){
 const title=useId();
 return <article className="ar-card ar-product-barcode" aria-labelledby={title}>
  <header>
   <span className="ar-kicker">管家婆 · 已保存的商品资料（不是实时查询）</span>
   <h3 id={title}>商品单位与条码 · 已核对来源页</h3>
   <p><bdi>{page.product_name}</bdi>（商品编码 <bdi>{page.product_code}</bdi>）</p>
   <p>本页是第 {page.offset+1} 至 {page.offset+page.returned_count} 条，单位和条码位置分别计入；相同条码保留各次出现。</p>
   <p>原件的可核范围有 {page.total_units} 个单位、{page.total_verified_barcodes} 个条码位置。当前连续核到第 {page.verified_through} 条，共 {page.total_records} 条记录。</p>
  </header>
  {page.all_pages_verified?<p>本原件的已核分页已到末尾。来源没有提供或无法核对的条码不算零条；不代表其他商品查全。</p>:
   <p className="assistant-notice">本原件的分页还没核完；这里仅显示原工作已保存的来源页。</p>}
  {hasSavedSuccessor&&<p>同一原件的下一页已经保存，在其他来源卡中单独显示，不与本页相加。</p>}
  {remainder}
  <ProductBarcodeRows sourceRows={page.rows}/>
 </article>;
}

/** Work-local paging only. Never sends an HTTP request. */
export function ProductBarcodeRows({sourceRows}:{sourceRows:ProductBarcodeRemainderPage['rows']}) {
 const [chunk,setChunk]=useState(0);
 const rows=useMemo(()=>{
   const units=new Map(sourceRows.flatMap(row=>row.kind==='unit'?[[row.standard_id,row.standard_name] as const]:[]));
   return sourceRows.flatMap<VisibleRow>(row=>row.kind==='unit'?[row]:
    row.barcodes.map((code,i)=>({kind:'barcode',code,ordinal:row.start_ordinal+i+1,unit:units.get(row.standard_id)})));
 },[sourceRows]);
 const start=chunk*showing,end=Math.min(start+showing,rows.length);
 return <>
  <ul aria-label="本页已核对的单位和条码" className="ar-product-barcode-rows">
   {rows.slice(start,end).map((row,i)=><li key={start+i}>
    {row.kind==='unit'?<><strong>单位：<bdi>{row.standard_name}</bdi></strong> · 换算关系原值：<bdi>{row.relation}</bdi><br/>{status(row)}</>:
     <><span>{row.unit?<bdi>{row.unit}</bdi>:'对应单位名称未在本页显示'} · 该单位第 {row.ordinal} 个条码：</span> <bdi className="ar-product-barcode-code">{row.code}</bdi></>}
   </li>)}
  </ul>
  {rows.length>showing&&<div className="ar-product-barcode-nav" aria-label="本页显示范围">
   <button type="button" className="ar-read" disabled={chunk===0} onClick={()=>setChunk(n=>n-1)}>上组已核记录</button>
   <span role="status">本页第 {start+1} 至 {end} 条</span>
   <button type="button" className="ar-read" disabled={end===rows.length} onClick={()=>setChunk(n=>n+1)}>下组已核记录</button>
  </div>}
 </>;
}
