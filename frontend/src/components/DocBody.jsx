// 交付件章节正文的只读渲染：标题、列表、表格、段落，以及来源标注上标。
// 来源颜色是固定语义：绿 = 客户证据（D / I / O），青 = 知识库（L4-…），紫 = 模型推断；核对不上的标注用猩红。
// 规则与后端 app/project/deliverables.py、export.py 的正则一致。

const TOKEN = /\[((?:I|D|O)-\d{2,4}(?:-\d{1,3})?)\]|[[｜|]\s*(L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5})\s*[\]】]|\[(?:推断|模型推断)\]|\*\*(.+?)\*\*/g;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*(\d{1,3})[.、)）]\s*(.*)$/;
const TABLE_SEP = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/;

function Ref({ cls, label, bad }) {
  return <sup className={`src ${bad ? "src-bad" : cls}`} title={bad ? "核对不上：项目里没有这条证据或知识" : undefined}>{label}</sup>;
}

export function Inline({ text, unresolved }) {
  const out = [];
  let last = 0;
  let m;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = m.index;
    if (m[1]) out.push(<Ref key={k} cls="src-ev" label={m[1]} bad={unresolved.has(m[1])} />);
    else if (m[2]) out.push(<Ref key={k} cls="src-kb" label={m[2]} bad={unresolved.has(m[2])} />);
    else if (m[3]) out.push(<strong key={k}>{m[3]}</strong>);
    else out.push(<Ref key={k} cls="src-inf" label="推断" />);
    last = TOKEN.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function blocks(body) {
  const lines = body.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (line.trim().startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
      i--;
      const head = rows.length > 1 && TABLE_SEP.test(rows[1].trim()) ? cells(rows[0]) : null;
      out.push({ t: "table", head, rows: rows.slice(head ? 2 : 0).filter((r) => !TABLE_SEP.test(r.trim())).map(cells) });
      continue;
    }
    let m;
    if ((m = HEADING.exec(line))) out.push({ t: "h", level: m[1].length, text: m[2] });
    else if ((m = BULLET.exec(line))) {
      const last = out[out.length - 1];
      if (last?.t === "ul") last.items.push(m[1]); else out.push({ t: "ul", items: [m[1]] });
    } else if ((m = NUMBERED.exec(line))) {
      const last = out[out.length - 1];
      if (last?.t === "ol") last.items.push(m[2]); else out.push({ t: "ol", items: [m[2]] });
    } else out.push({ t: "p", text: line });
  }
  return out;
}

export default function DocBody({ body, unresolved = [] }) {
  const bad = new Set(unresolved);
  return (
    <div className="doc">
      {blocks(body).map((b, i) => {
        if (b.t === "h") return <h3 key={i} className={b.level <= 2 ? "doc-h" : "doc-h3"}><Inline text={b.text} unresolved={bad} /></h3>;
        if (b.t === "ul") return <ul key={i}>{b.items.map((x, j) => <li key={j}><Inline text={x} unresolved={bad} /></li>)}</ul>;
        if (b.t === "ol") return <ol key={i}>{b.items.map((x, j) => <li key={j}><Inline text={x} unresolved={bad} /></li>)}</ol>;
        if (b.t === "table") {
          return (
            <table key={i} className="tbl doc-tbl">
              {b.head && <thead><tr>{b.head.map((c, j) => <th key={j}><Inline text={c} unresolved={bad} /></th>)}</tr></thead>}
              <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => <td key={k}><Inline text={c} unresolved={bad} /></td>)}</tr>)}</tbody>
            </table>
          );
        }
        return <p key={i}><Inline text={b.text} unresolved={bad} /></p>;
      })}
    </div>
  );
}
