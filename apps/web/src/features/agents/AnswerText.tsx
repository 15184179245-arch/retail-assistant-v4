import type { ReactNode } from "react";

/**
 * 安全的最小 Markdown 渲染：只处理段落、列表、小标题、**加粗** 与 `代码`。
 * 一律走 React 文本节点，不使用 dangerouslySetInnerHTML，模型输出无法注入 HTML。
 */

const INLINE_PATTERN = /(\*\*[^*]+\*\*|`[^`]+`)/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const parts = text.split(INLINE_PATTERN).filter((part) => part !== "");
  return parts.map((part, index) => {
    const key = `${keyPrefix}-${index}`;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    return <span key={key}>{part}</span>;
  });
}

const BULLET = /^\s*[-*•]\s+/;
const ORDERED = /^\s*\d+[.)]\s+/;
const HEADING = /^\s*(#{2,4})\s+(.*)$/;

export function AnswerText({ text }: { text: string }): ReactNode {
  const blocks = text.replace(/\r\n/g, "\n").split(/\n{2,}/).filter((block) => block.trim() !== "");

  return (
    <div className="answer-text">
      {blocks.map((block, blockIndex) => {
        const lines = block.split("\n");
        const heading = HEADING.exec(lines[0]);
        if (heading && lines.length === 1) {
          return <h4 key={blockIndex}>{renderInline(heading[2], `h${blockIndex}`)}</h4>;
        }
        if (lines.every((line) => BULLET.test(line))) {
          return (
            <ul key={blockIndex}>
              {lines.map((line, lineIndex) => (
                <li key={lineIndex}>{renderInline(line.replace(BULLET, ""), `b${blockIndex}-${lineIndex}`)}</li>
              ))}
            </ul>
          );
        }
        if (lines.every((line) => ORDERED.test(line))) {
          return (
            <ol key={blockIndex}>
              {lines.map((line, lineIndex) => (
                <li key={lineIndex}>{renderInline(line.replace(ORDERED, ""), `o${blockIndex}-${lineIndex}`)}</li>
              ))}
            </ol>
          );
        }
        return <p key={blockIndex}>{renderInline(block.replace(/\n/g, " "), `p${blockIndex}`)}</p>;
      })}
    </div>
  );
}
