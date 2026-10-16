import { Fragment, type ReactNode } from "react";

// The little Markdown the models write: paragraphs, lists, tables, **bold** and `code`.
// It builds React nodes, never HTML, so a model's text can't inject markup.
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    out.push(
      tok.startsWith("**") ? (
        <strong key={m.index}>{tok.slice(2, -2)}</strong>
      ) : (
        <code key={m.index} className="rounded-md bg-white/10 px-1 py-px text-[0.92em]">
          {tok.slice(1, -1)}
        </code>
      ),
    );
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  const lines = text.replace(/\r/g, "").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const bullet = /^\s*[-*•]\s+/;
    const numbered = /^\s*\d+[.)]\s+/;
    const row = /^\s*\|.*\|/; // a trailing pipe is optional in Markdown tables
    if (row.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      // a table: header row, separator row, body rows
      const cells = (l: string) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const body: string[][] = [];
      while (i < lines.length && row.test(lines[i])) body.push(cells(lines[i++]));
      blocks.push(
        <div key={blocks.length} className="my-1 overflow-x-auto">
          <table className="w-full text-left text-[14px]">
            <thead className="text-label-2">
              <tr>
                {head.map((h, k) => (
                  <th key={k} className="border-b border-white/15 py-1 pr-3 font-medium">
                    {inline(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((r, k) => (
                <tr key={k} className="border-b border-white/8 last:border-0">
                  {r.map((c, j) => (
                    <td key={j} className="py-1.5 pr-3 align-top">
                      {inline(c)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    if (bullet.test(line) || numbered.test(line)) {
      const ordered = numbered.test(line);
      const start = ordered ? parseInt(line.trim(), 10) : undefined; // a list split by a paragraph keeps counting
      const items: string[] = [];
      while (i < lines.length && (bullet.test(lines[i]) || numbered.test(lines[i]))) {
        items.push(lines[i].replace(ordered ? numbered : bullet, ""));
        i++;
      }
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={blocks.length} start={start} className={ordered ? "list-decimal" : "list-disc"}>
          {items.map((it, k) => (
            <li key={k}>{inline(it)}</li>
          ))}
        </List>,
      );
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !bullet.test(lines[i]) && !numbered.test(lines[i])) {
      para.push(lines[i].replace(/^#+\s*/, ""));
      i++;
    }
    blocks.push(
      <p key={blocks.length}>
        {para.map((p, k) => (
          <Fragment key={k}>
            {k > 0 && <br />}
            {inline(p)}
          </Fragment>
        ))}
      </p>,
    );
  }
  return <div className="prose-msg">{blocks}</div>;
}
