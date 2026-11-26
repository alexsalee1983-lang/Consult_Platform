// 四期 E3③：调研助手现场三段（快速确认 → 议程 → 补问）的纯逻辑。议程由平台 GET /survey/agenda 现算（app/project/agenda.py）。
// 一份答卷按受访角色取该角色的议程；包里其余题放“其他”，顾问需要时再问。

export const PHASES = [
  ["quick_confirm", "快速确认"],
  ["agenda", "议程"],
  ["followup", "补问"],
  ["rest", "其他题"],
];
export const REASONS = { confirm: "待点头", conflict: "矛盾·现场定案", insufficient: "证据不足", open: "开放讨论", observe: "需观察或演示" };

export const roleAgenda = (agenda, role) => (agenda?.roles || []).find((r) => r.role === (role || "").trim()) || null;

// 按三段给包里的题分组：{quick_confirm: [q], agenda: [q], followup: [q], rest: [q]}；同一题只进最先的一段
export function phases(pack, agenda, role) {
  const mine = roleAgenda(agenda, role);
  const seen = new Set();
  const out = {};
  for (const [key] of PHASES.slice(0, 3)) {
    const ids = new Set((mine?.[key] || []).map((i) => i.qid));
    out[key] = pack.questions.filter((q) => ids.has(q.qid) && !seen.has(q.qid));
    out[key].forEach((q) => seen.add(q.qid));
  }
  out.rest = pack.questions.filter((q) => !seen.has(q.qid));
  return out;
}

// 议程条目（带已有答案）按题号查
export function itemsByQid(agenda, role) {
  const mine = roleAgenda(agenda, role);
  const m = {};
  for (const [key] of PHASES.slice(0, 3)) for (const i of mine?.[key] || []) m[i.qid] ??= i;
  return m;
}

// 实时矛盾提示：现场选的选项与答案池里已有答案不一致的，列出“角色（来源）”
const SOURCE = { document: "客户资料", self: "客户自评", oral: "现场口述", evidence: "现场证据" };
export function clashes(item, value) {
  if (!item || !value || value === "X") return [];
  return (item.answers || []).filter((a) => a.value && a.value !== value).map((a) => `${a.role}（${SOURCE[a.source] || a.source}）`);
}

export const sourceLabel = (s) => SOURCE[s] || s;
