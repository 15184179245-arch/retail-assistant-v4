import {validateUiBlock,type ValidatedUiBlock} from '../../blocks';

// Only consume an authenticated Host public projection, never a provider/native
// stream or JSON extracted from answer text. This decoder grants no authority.
const READ_ONLY_TYPES = new Set(['product_card','product_list','compare_table','stock_location','work_item_list','evidence_panel']);
export function decodeReadOnlyBlocks(value:unknown):ValidatedUiBlock[] {
  if(!Array.isArray(value)||value.length>20)throw Error('公开展示块数量异常。');
  let bytes:number;
  try {bytes=new TextEncoder().encode(JSON.stringify(value)).byteLength;} catch {throw Error('公开展示块格式异常。');}
  if(bytes>262144)throw Error('公开展示块过大。');
  return value.map(block=>{
    const result=validateUiBlock(block);
    if(!result.success||!READ_ONLY_TYPES.has(result.data.type))throw Error('公开展示块未受支持。');
    return result.data;
  });
}
