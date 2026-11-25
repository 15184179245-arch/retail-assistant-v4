import {BlockRenderer} from '../../blocks';
import {decodeReadOnlyBlocks} from './publicBlockDecoder';

// No mutation/navigation callbacks are provided. The caller must withdraw this
// entire projection on scope/capability/source changes, just like answer facts.
export function ReadOnlyBlocks({value}:{value:unknown}) {
  try {
    const blocks=decodeReadOnlyBlocks(value);
    return <section aria-label="结构化结果">{blocks.map((block,index)=><BlockRenderer key={index} block={block}/>)}</section>;
  } catch {
    return <p role="status">结构化结果暂不可展示，请查看文字答复。</p>;
  }
}
