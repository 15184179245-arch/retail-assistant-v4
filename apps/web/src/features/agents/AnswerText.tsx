import type { ReactNode } from "react";

/**
 * 安全的最小 Markdown 渲染：只处理段落、列表、小标题、强调、内联代码和完整配对围栏。
 * 一律走 React 文本节点，不使用 dangerouslySetInnerHTML，模型输出无法注入 HTML。
 */

const INLINE_PATTERN = /(\*\*[^*]+\*\*|`[^`]+`)/g;

// Saved addresses remain plain text. Markdown markers inside an address
// belong to the target; never rewrite it or make it navigable.
const SAVED_ADDRESS = /\bhttps?:\/\/[^\s<>"`]+/giu;
function matchAddressAt(text:string){
  const addresses=text.matchAll(SAVED_ADDRESS);
  let current=addresses.next();
  return (offset:number)=>{
    while(!current.done && current.value.index+current.value[0].length<=offset)current=addresses.next();
    return !current.done && current.value.index<=offset ? current.value : undefined;
  };
}


const fenceLikeLine=/^ {0,3}(`{3,}|~{3,})/mu;

/** A soft newline still renders as a space. Do not parse user text as markup. */
function proseWithHardBreaks(text:string,keyPrefix:string):ReactNode[]{
 const lines=text.split('\n'), nodes:ReactNode[]=[];
 for(let i=0;i<lines.length;i++){
  const line=lines[i];
  if(i===lines.length-1){nodes.push(line);break;}
  let cut=line.length, hard=false;
  if(line.endsWith('  ')){
   while(cut>0&&line[cut-1]===' ')cut--;
   hard=true;
  }else if(line.endsWith('\\')){
   while(cut>0&&line[cut-1]==='\\')cut--;
   hard=(line.length-cut)%2===1;
   cut=hard?line.length-1:line.length;
  }
  nodes.push(hard?line.slice(0,cut):line+' ');
  if(hard)nodes.push(<br key={keyPrefix+'-line-'+i}/>);
 }
 return nodes;
}

function renderInline(text: string, keyPrefix: string, prose=false, preserveSavedAddress=false): ReactNode[] {
  const parts = text.split(INLINE_PATTERN).filter((part) => part !== "");
  const addressAt=preserveSavedAddress?matchAddressAt(text):undefined;
  let offset=0;
  return parts.map((part, index) => {
    const start=offset;
    offset+=part.length;
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      const openingAddress=addressAt?.(start);
      const closingAddress=addressAt?.(offset-2);
      // Preserve normal emphasis ending with a bare address, e.g.
      // "**来源：https://example.com**", but not a marker inside <address>.
      const wrapsBareAddress=!!closingAddress && offset===closingAddress.index+closingAddress[0].length
        && text[closingAddress.index-1]!=='<' && text[closingAddress.index-1]!=='('
        && !closingAddress[0].slice(0,-2).includes('*');
      if(openingAddress || (closingAddress && !wrapsBareAddress))
        return <span key={key}>{prose?proseWithHardBreaks(part,key):part}</span>;
      const emphasis=part.slice(2,-2);
      return <strong key={key}>{prose?proseWithHardBreaks(emphasis,key):emphasis}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      const literal=part.slice(1,-1);
      return <code key={key}>{prose?literal.replace(/\n/g,' '):literal}</code>;
    }
    return <span key={key}>{prose?proseWithHardBreaks(part,key):part}</span>;
  });
}

const BULLET = /^\s*[-*•]\s+/;
const ORDERED = /^\s*(\d+)[.)]\s+/;
const HEADING = /^\s*(#{2,4})\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?\s*$/;

function splitTableCells(line: string, count?: number): string[] {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  const cells = trimmed.split("|");
  if (count === undefined) return cells;
  if (cells.length >= count) return cells.slice(0, count);
  return [...cells, ...Array.from({ length: count - cells.length }, () => "")];
}

function tableAlignments(sepLine: string): Array<"left" | "center" | "right" | undefined> {
  return splitTableCells(sepLine).map((raw) => {
    const s = raw.trim();
    const left = s.startsWith(":");
    const right = s.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    if (left) return "left";
    return undefined;
  });
}

type AnswerSegment={kind:'plain'|'literal';text:string};
const openingFence=/^ {0,3}(`{3,}|~{3,})(.*)$/u;
const closingFence=/^ {0,3}(`{3,}|~{3,})[ \t]*$/u;

/** Scan once. An incomplete fence stays in the original plain-text renderer. */
function answerSegments(text:string):AnswerSegment[]{
 const normalized=text.replace(/\r\n/g,'\n');
 if(!normalized.includes('```')&&!normalized.includes('~~~'))return [{kind:'plain',text:normalized}];
 const lines=normalized.split('\n'), pieces:AnswerSegment[]=[];
 let start=0, open:{index:number;marker:string}|undefined;
 const addPlain=(from:number,to:number)=>{
  // The complete fence is already a block boundary. Bare separator lines
  // outside it shouldn't turn a standalone heading/list into a paragraph.
  while(from<to&&!lines[from].trim())from++;
  while(to>from&&!lines[to-1].trim())to--;
  if(from<to)pieces.push({kind:'plain',text:lines.slice(from,to).join('\n')});
 };
 for(let i=0;i<lines.length;i++){
  if(!open){
   const match=openingFence.exec(lines[i]);
   // Never interpret the language annotation or accept an inline backtick run.
   if(match&&!(match[1][0]==='`'&&match[2].includes('`')))open={index:i,marker:match[1]};
   continue;
  }
  const close=closingFence.exec(lines[i]);
  if(!close||close[1][0]!==open.marker[0]||close[1].length<open.marker.length)continue;
  addPlain(start,open.index);
  pieces.push({kind:'literal',text:lines.slice(open.index+1,i).join('\n')});
  start=i+1;open=undefined;
 }
 if(start===0)return [{kind:'plain',text:normalized}];
 addPlain(start,lines.length);
 return pieces;
}

function plainAnswerBlocks(text:string,segment:number,preserveSavedAddress:boolean):ReactNode[]{
  const prose=!fenceLikeLine.test(text);
  const blocks = text.split(/\n{2,}/).filter((block) => block.trim() !== "");
  return blocks.map((block, blockIndex) => {
    const key = `${segment}-${blockIndex}`;
    const lines = block.split("\n");
    const heading = HEADING.exec(lines[0]);
    if (heading && lines.length === 1) {
      return <h4 key={key}>{renderInline(heading[2], `h${key}`,false,preserveSavedAddress)}</h4>;
    }
    if (lines.length >= 2 && lines[0].includes("|") && TABLE_SEP.test(lines[1])) {
      const headerCells = splitTableCells(lines[0]);
      const aligns = tableAlignments(lines[1]);
      if (headerCells.length >= 2 && headerCells.length === aligns.length) {
        const bodyLines = lines.slice(2);
        return (
          <div key={key} className="answer-table-wrap">
            <table className="answer-table">
              <thead>
                <tr>
                  {headerCells.map((cell, ci) => (
                    <th key={ci} style={aligns[ci] ? { textAlign: aligns[ci] } : undefined}>
                      {renderInline(cell.trim(), `th${key}-${ci}`, false, preserveSavedAddress)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {bodyLines.map((rowLine, ri) => {
                  const cells = splitTableCells(rowLine, headerCells.length);
                  return (
                    <tr key={ri}>
                      {cells.map((cell, ci) => (
                        <td key={ci} style={aligns[ci] ? { textAlign: aligns[ci] } : undefined}>
                          {renderInline(cell.trim(), `td${key}-${ri}-${ci}`, false, preserveSavedAddress)}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      }
    }
    if (lines.every((line) => /^\s*>\s?/.test(line))) {
      const quoteText = lines.map((line) => line.replace(/^\s*>\s?/, "")).join("\n");
      return (
        <blockquote key={key} className="answer-callout">
          {renderInline(quoteText, `bq${key}`, prose, preserveSavedAddress)}
        </blockquote>
      );
    }
    if (lines.every((line) => BULLET.test(line))) {
      return (
        <ul key={key}>
          {lines.map((line, lineIndex) => (
            <li key={lineIndex}>{renderInline(line.replace(BULLET, ""), `b${key}-${lineIndex}`,false,preserveSavedAddress)}</li>
          ))}
        </ul>
      );
    }
    const ordinals = lines.map((line) => Number(ORDERED.exec(line)?.[1]));
    if (ordinals.every((n) => Number.isSafeInteger(n) && n <= 2147483647)) {
      return (
        <ol key={key} start={ordinals[0] === 1 ? undefined : ordinals[0]}>
          {lines.map((line, lineIndex) => (
            <li key={lineIndex} value={lineIndex > 0 && ordinals[lineIndex] !== ordinals[lineIndex - 1] + 1
              ? ordinals[lineIndex] : undefined}>{renderInline(line.replace(ORDERED, ""), `o${key}-${lineIndex}`,false,preserveSavedAddress)}</li>
          ))}
        </ol>
      );
    }
    return <p key={key}>{renderInline(prose?block:block.replace(/\n/g, " "), `p${key}`,prose,preserveSavedAddress)}</p>;
  });
}

export function AnswerText({ text, preserveSavedAddress=false }: { text: string; preserveSavedAddress?: boolean }): ReactNode {
  return (
    <div className="answer-text">
      {answerSegments(text).flatMap<ReactNode>((section,index)=>section.kind==='plain'
       ?plainAnswerBlocks(section.text,index,preserveSavedAddress)
       :[<pre key={'literal-'+index} className="answer-preformatted" tabIndex={0} aria-label="原格式文字"
          style={{maxWidth:'100%',overflowX:'auto',whiteSpace:'pre',overflowWrap:'normal'}}>{section.text}</pre>])}
    </div>
  );
}
