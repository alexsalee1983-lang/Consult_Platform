// 四期 A32②：确认卡证据并排。卡片里引用的两处证据各取一段原文，左右并排，标明页码和段落（规则见 app/project/card_evidence.py）。
const KIND = { document: "客户资料", interview: "访谈纪要", observation: "现场观察" };

export function where(x) {
  return [x.page && `第 ${x.page} 页`, x.paragraph && `第 ${x.paragraph} 段`, x.heading && !x.page && x.heading].filter(Boolean).join(" · ");
}

export default function EvidencePair({ items }) {
  if (!items?.length) return null;
  return (
    <div className="ev-pair" aria-label="证据并排">
      {items.map((x) => (
        <div key={x.code} className="ev-cell">
          <div className="sb">
            <span className="mono" style={{ color: "var(--cyan)" }}>{x.ref}</span>
            <span className="sub">{KIND[x.kind] || "证据"}</span>
          </div>
          {x.title && <div style={{ fontWeight: 600 }}>{x.title}{x.interviewee_role ? `（${x.interviewee_role}）` : ""}</div>}
          {x.found ? (
            <>
              <div className="sub mono">{where(x)}</div>
              <blockquote>{x.excerpt}</blockquote>
            </>
          ) : <div className="sub">{x.note}</div>}
        </div>
      ))}
    </div>
  );
}

// 同一份资料引出的多张卡：按后端分组合成一组（card_ids），组内逐张处理
export function groupCards(cards, groups) {
  const byId = new Map(cards.map((c) => [c.id, c]));
  const used = new Set();
  const out = [];
  for (const g of groups || []) {
    const members = g.card_ids.map((id) => byId.get(id)).filter(Boolean);
    if (!members.length) continue;
    members.forEach((c) => used.add(c.id));
    out.push({ source: g.source, title: g.title, cards: members });
  }
  for (const c of cards) if (!used.has(c.id)) out.push({ source: "", title: "", cards: [c] });
  return out;
}
