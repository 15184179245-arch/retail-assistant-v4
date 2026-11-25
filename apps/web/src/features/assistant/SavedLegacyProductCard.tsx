import {useEffect,useLayoutEffect,useRef,useState,type ReactNode} from 'react';
import {usePath} from '../../app/router';
import {parseSavedLegacyProduct,type SavedLegacyProduct} from './savedLegacyProduct';
import type {SavedLegacyProductStep} from './workProgress';

function SourceText({children}:{children:ReactNode}){
 return <bdi style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{children}</bdi>;
}
function BarcodeGroups({units}:{units:SavedLegacyProduct['units']}){
 const [expanded,setExpanded]=useState<number|null>(null);
 const inline=units.reduce((n,unit)=>n+(unit.barcodes?.length??0),0)<=60;
 return <div className="ar-operational-scroll" tabIndex={0} aria-label="原单位、比例及条码">
  <table aria-label="原单位、比例及条码">
   <thead><tr><th scope="col">原单位</th><th scope="col">原比例</th><th scope="col">该单位的原条码记录</th></tr></thead>
   <tbody>{units.map((unit,i)=>{
    const n=unit.barcodes?.length??0;
    const status=unit.barcode_state==='not_in_version'?'旧版 v1 未提供条码字段。':
     unit.barcode_state==='not_reported'?'该单位原记录未报告条码，不等于没有。':
     unit.barcode_state==='partial'?(n?'只核实了部分条码。':'原组只能部分核实，未核到可显示的条码，不等于没有。'):
     n?'原记录报告了条码，未验证全量。':'该单位当时明确报告空列表，不代表现在没有或已查全。';
    return <tr key={i}>
     <th scope="row"><SourceText>{unit.name}</SourceText></th><td><SourceText>{unit.reported_relation}</SourceText></td>
     <td style={{whiteSpace:'normal'}}><p>{status}</p>
      {!inline&&n>0&&<button className="ar-read" type="button" aria-expanded={expanded===i}
       onClick={()=>setExpanded(old=>old===i?null:i)}>{expanded===i?'收起本组条码':`展开本组已存 ${n} 条条码`}</button>}
      {n>0&&(inline||expanded===i)&&<ol aria-label="按保存原序的条码">
       {unit.barcodes!.map((barcode,index)=><li key={index}><SourceText>{barcode}</SourceText></li>)}
      </ol>}
     </td>
    </tr>;
   })}</tbody>
  </table>
 </div>;
}

export function SavedLegacyProductCard({workId,runId,ready,steps,read}:{workId:string;runId:string;ready:boolean;
 steps:SavedLegacyProductStep[];read:(stepId:string,signal:AbortSignal)=>Promise<unknown>}){
 const path=usePath(),panel=useRef<HTMLElement>(null),job=useRef<AbortController|null>(null);
 const [working,setWorking]=useState<SavedLegacyProductStep|null>(null),[notice,setNotice]=useState('');
 const [view,setView]=useState<{step:SavedLegacyProductStep;record:SavedLegacyProduct}|null>(null);
 const allowed=(original:SavedLegacyProductStep)=>steps.some(step=>step.id===original.id&&step.version===original.version&&
  step.sequence===original.sequence&&step.outputDigest===original.outputDigest);
 const current=()=>ready&&document.visibilityState!=='hidden'&&navigator.onLine!==false&&
  !!panel.current&&!panel.current.closest('[hidden]');
 function reset(message=''){
  const active=job.current;job.current=null;active?.abort();setWorking(null);setView(null);setNotice(message);
 }
 useLayoutEffect(()=>{
  reset();
  return ()=>{const active=job.current;job.current=null;active?.abort();};
 },[workId,runId,path]);
 const shown=ready&&view&&allowed(view.step)?view:null;
 const witness=steps.map(step=>[step.id,step.version,step.sequence,step.outputDigest].join(':')).join(',');
 useLayoutEffect(()=>{
  if((!ready&&(working||view))||(working&&!allowed(working))||(view&&!allowed(view.step)))
   reset('为核对查看权限，暂不展示上次记录。可手动再查看。');
 },[ready,witness]);
 useEffect(()=>{
  function hidden(){if(document.visibilityState==='hidden')reset();}
  function offline(){reset();}
  document.addEventListener('visibilitychange',hidden);window.addEventListener('offline',offline);
  return ()=>{document.removeEventListener('visibilitychange',hidden);window.removeEventListener('offline',offline);};
 },[]);
 async function show(step:SavedLegacyProductStep){
  if(!current()||!allowed(step)||working?.id===step.id)return;
  reset('正在核对这一步保存的旧版商品记录…');
  const request=new AbortController();job.current=request;setWorking(step);
  const live=()=>job.current===request&&!request.signal.aborted&&current();
  try{
   const data=await read(step.id,request.signal);
   if(live()){setView({step,record:parseSavedLegacyProduct(data,{workId,runId,stepId:step.id,version:step.version})});setNotice('');}
  }catch{
   if(live())reset('目前无法核实这一步的旧版商品记录。没有重新查询，不能据此判断商品或条码是否存在。');
  }finally{if(job.current===request){job.current=null;setWorking(null);}}
 }
 const product=shown?.record.product;
 return <article className="ar-card" ref={panel} aria-label="已保存的旧版商品记录">
  <header>
   <h3>已保存的旧版商品记录</h3>
   <p>只核对所选步骤当时保存的字段，不是实时商品资料，也不是全量条码。这份旧版记录不能续页。</p>
   <div role="group" aria-label="原商品步骤">
    {steps.map(step=><button className="ar-read" type="button" disabled={!ready||working?.id===step.id}
     key={step.id} onClick={()=>void show(step)}>查看第 {step.sequence} 步商品原记录（旧版 v{step.version}）</button>)}
    {working&&<button className="ar-read" type="button" onClick={()=>reset('已取消查看。没有重新查询。')}>取消查看</button>}
   </div>
   {notice&&<p role="status">{notice}</p>}
  </header>
  {shown&&product&&<section aria-label="旧版商品原字段">
   <div className="ar-panel">
    <h4><SourceText>{product.name}</SourceText>（<SourceText>{product.code}</SourceText>）</h4>
    <p>原保存时间（UTC）：<time dateTime={shown.record.recorded_at}>{shown.record.recorded_at}</time>。</p>
    {shown.step.version==='1'&&<p>旧版 v1 未提供型号、规格、品牌、分类、采购或销售默认单位，也未提供条码字段。</p>}
    <dl className="ar-operational-detail-fields" aria-label="商品原字段">
     {(shown.step.version==='1'?[['辅助单位',product.subunit_name]]:[
      ['型号',product.model],['规格',product.specification],['品牌',product.brand_name],['分类',product.category_name],
      ['来源采购默认单位',product.reported_purchase_unit_name],['来源销售默认单位',product.reported_sales_unit_name],
      ['辅助单位',product.subunit_name],
     ]).map(([label,value])=><div key={label}><dt>{label}</dt>
      <dd>{value===null?'原记录未提供':<SourceText>{value}</SourceText>}</dd></div>)}
    </dl>
   </div>
   <BarcodeGroups key={shown.step.id} units={shown.record.units}/>
  </section>}
 </article>;
}
