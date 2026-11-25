const STARTERS = [
  {
    label: '查采购订单清单',
    permission: 'guanjia.purchase_orders.read',
    draft: '请只读查看本门店最近 7 天的管家婆采购订单清单，只列已核实的单据编号、日期和状态。注明本次查询页范围，不查订单详情，不下单、审批或入库。',
  },
  {
    label: '查单日已过账销售明细',
    permission: 'guanjia.sales.read',
    draft: '请只读查询本门店指定日期的已过账销售明细。日期：尚未填写；请先向我确认具体日期。只展示本次已核实的范围，不把本页视为全日报表。',
  },
] as const;

type Props = {
  enabled: boolean;
  hasCapability: (capability: string) => boolean;
  onDraft: (text: string) => void;
};

/** These starters only compose a draft. The Host still checks the requester, Agent and shop. */
export function BusinessReadStarters({enabled,hasCapability,onDraft}:Props) {
  const allowed=(permission:string)=>hasCapability('guanjia.company.read')&&hasCapability(permission);
  if(!enabled)return null;
  return STARTERS.filter(starter=>allowed(starter.permission)).map(starter=>
    <button key={starter.permission} type="button" onClick={()=>{
      if(allowed(starter.permission))onDraft(starter.draft);
    }}>{starter.label}</button>,
  );
}
