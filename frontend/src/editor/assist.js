// A15② 写稿助手的纯函数（不依赖浏览器与 CodeMirror，可在 node 里测试）。
// 插入保留来源：助手回复原样（含 [I-01] [L4-…] [推断] 等标注）放到光标处或替换选区，不改写、不剥标注；
// 插入后的正文仍走原保存接口，签署前照样过质量门禁。

export const ASSIST_NOTE = "含写稿助手插入内容";

// 文本框兜底时的插入：替换 [from, to)，返回新正文和插入内容之后的光标位置
export function spliceText(text, from, to, snippet) {
  const a = Math.max(0, Math.min(from ?? text.length, text.length));
  const b = Math.max(a, Math.min(to ?? a, text.length));
  return { text: text.slice(0, a) + snippet + text.slice(b), cursor: a + snippet.length };
}

// 修改说明里记一笔“含写稿助手插入内容”（不覆盖顾问自己写的说明，重复插入只记一次）
export function noteAfterInsert(note) {
  const n = (note || "").trim();
  if (n.includes(ASSIST_NOTE)) return note;
  return n ? `${n}；${ASSIST_NOTE}` : ASSIST_NOTE;
}

// done.sources → 侧栏一行说明
export function sourcesLine(sources) {
  if (!sources) return "";
  const c = sources.counts || {};
  const parts = [`证据 ${c.evidence || 0}`, `知识 ${c.knowledge || 0}`, `推断 ${c.inferred || 0}`];
  const bad = sources.unresolved || [];
  return `来源：${parts.join(" · ")}${bad.length ? `；核对不上：${bad.join("、")}（插入后保存会列入待核对，签署前评审阻断）` : ""}`;
}
