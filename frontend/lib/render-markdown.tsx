import React from "react";

/**
 * Lightweight markdown-to-JSX renderer for ARIA chat messages.
 * Handles: headers, tables, bold, italic, inline code, lists, horizontal rules.
 * No external dependencies.
 */

function inlineFormat(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // Matches: **bold**, __bold__, *italic*, _italic_, `code`
  const re = /(\*\*(.+?)\*\*|__(.+?)__|(?<!\w)\*(.+?)\*(?!\w)|(?<!\w)_(.+?)_(?!\w)|`(.+?)`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let idx = 0;

  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[2]) nodes.push(<strong key={`${keyPrefix}-b${idx}`}>{m[2]}</strong>);
    else if (m[3]) nodes.push(<strong key={`${keyPrefix}-b${idx}`}>{m[3]}</strong>);
    else if (m[4]) nodes.push(<em key={`${keyPrefix}-i${idx}`}>{m[4]}</em>);
    else if (m[5]) nodes.push(<em key={`${keyPrefix}-i${idx}`}>{m[5]}</em>);
    else if (m[6])
      nodes.push(
        <code
          key={`${keyPrefix}-c${idx}`}
          className="px-1 py-0.5 bg-black/5 rounded text-[0.85em] font-mono"
        >
          {m[6]}
        </code>
      );
    last = m.index + m[0].length;
    idx++;
  }

  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function parseTable(
  lines: string[],
  startIdx: number,
  keyPrefix: string
): { element: React.ReactNode; consumed: number } {
  const rows: string[][] = [];
  let i = startIdx;

  while (i < lines.length && lines[i].trim().startsWith("|")) {
    const row = lines[i]
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => cell.trim());
    rows.push(row);
    i++;
  }

  if (rows.length < 2) return { element: null, consumed: 0 };

  // Check if row[1] is a separator (e.g. |---|---|)
  const isSeparator = rows[1].every((cell) => /^[-:]+$/.test(cell));
  const headerRow = rows[0];
  const dataRows = isSeparator ? rows.slice(2) : rows.slice(1);

  const element = (
    <div key={keyPrefix} className="overflow-x-auto my-2">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b-2 border-[#E0DED8]">
            {headerRow.map((cell, ci) => (
              <th
                key={ci}
                className="text-left py-2 px-3 text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide"
              >
                {inlineFormat(cell, `${keyPrefix}-th${ci}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {dataRows.map((row, ri) => (
            <tr
              key={ri}
              className="border-b border-[#E0DED8] last:border-0"
            >
              {row.map((cell, ci) => (
                <td key={ci} className="py-2 px-3">
                  {inlineFormat(cell, `${keyPrefix}-td${ri}-${ci}`)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  return { element, consumed: i - startIdx };
}

function parseCodeBlock(
  lines: string[],
  startIdx: number,
  keyPrefix: string
): { element: React.ReactNode; consumed: number } {
  const openMatch = lines[startIdx].trim().match(/^```(\w*)$/);
  if (!openMatch) return { element: null, consumed: 0 };

  const lang = openMatch[1] || "";
  const codeLines: string[] = [];
  let i = startIdx + 1;

  while (i < lines.length && !lines[i].trim().startsWith("```")) {
    codeLines.push(lines[i]);
    i++;
  }

  // Skip closing ```
  if (i < lines.length) i++;

  const element = (
    <div key={keyPrefix} className="my-2 rounded-lg overflow-hidden border border-[#E0DED8]">
      {lang && (
        <div className="px-3 py-1 bg-[#F0F0EC] text-[10px] font-mono text-[#5F5E5A] uppercase tracking-wide">
          {lang}
        </div>
      )}
      <pre className="px-3 py-2 bg-[#F8F8F6] overflow-x-auto text-xs leading-relaxed">
        <code className="font-mono text-[#2C2C2A]">{codeLines.join("\n")}</code>
      </pre>
    </div>
  );

  return { element, consumed: i - startIdx };
}

export function renderMarkdown(text: string): React.ReactNode {
  const lines = text.split("\n");
  const elements: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    // Blank line
    if (!trimmed) {
      i++;
      continue;
    }

    // Code block (```json ... ```)
    if (trimmed.startsWith("```")) {
      const { element, consumed } = parseCodeBlock(lines, i, `code-${i}`);
      if (element && consumed > 0) {
        elements.push(element);
        i += consumed;
        continue;
      }
    }

    // Horizontal rule (---, ***, ___)
    if (/^[-*_]{3,}$/.test(trimmed)) {
      elements.push(<hr key={`hr-${i}`} className="my-3 border-[#E0DED8]" />);
      i++;
      continue;
    }

    // Headers
    if (trimmed.startsWith("### ")) {
      elements.push(
        <h3 key={`h3-${i}`} className="text-sm font-bold text-[#2C2C2A] mt-3 mb-1">
          {inlineFormat(trimmed.slice(4), `h3-${i}`)}
        </h3>
      );
      i++;
      continue;
    }
    if (trimmed.startsWith("## ")) {
      elements.push(
        <h2 key={`h2-${i}`} className="text-base font-bold text-[#2C2C2A] mt-3 mb-1">
          {inlineFormat(trimmed.slice(3), `h2-${i}`)}
        </h2>
      );
      i++;
      continue;
    }
    if (trimmed.startsWith("# ")) {
      elements.push(
        <h1 key={`h1-${i}`} className="text-lg font-bold text-[#2C2C2A] mt-3 mb-1">
          {inlineFormat(trimmed.slice(2), `h1-${i}`)}
        </h1>
      );
      i++;
      continue;
    }

    // Table
    if (trimmed.startsWith("|")) {
      const { element, consumed } = parseTable(lines, i, `tbl-${i}`);
      if (element && consumed > 0) {
        elements.push(element);
        i += consumed;
        continue;
      }
    }

    // Unordered list item (-, *, +)
    if (/^[-*+]\s/.test(trimmed)) {
      const listItems: React.ReactNode[] = [];
      while (i < lines.length && /^[-*+]\s/.test(lines[i].trim())) {
        const itemText = lines[i].trim().replace(/^[-*+]\s+/, "");
        listItems.push(
          <li key={`li-${i}`} className="ml-4 list-disc">
            {inlineFormat(itemText, `li-${i}`)}
          </li>
        );
        i++;
      }
      elements.push(
        <ul key={`ul-${i}`} className="my-1 space-y-0.5">
          {listItems}
        </ul>
      );
      continue;
    }

    // Ordered list item (1. 2. etc.)
    if (/^\d+\.\s/.test(trimmed)) {
      const listItems: React.ReactNode[] = [];
      while (i < lines.length && /^\d+\.\s/.test(lines[i].trim())) {
        const itemText = lines[i].trim().replace(/^\d+\.\s+/, "");
        listItems.push(
          <li key={`oli-${i}`} className="ml-4 list-decimal">
            {inlineFormat(itemText, `oli-${i}`)}
          </li>
        );
        i++;
      }
      elements.push(
        <ol key={`ol-${i}`} className="my-1 space-y-0.5">
          {listItems}
        </ol>
      );
      continue;
    }

    // Regular paragraph
    elements.push(
      <p key={`p-${i}`} className="my-1">
        {inlineFormat(trimmed, `p-${i}`)}
      </p>
    );
    i++;
  }

  return <>{elements}</>;
}
