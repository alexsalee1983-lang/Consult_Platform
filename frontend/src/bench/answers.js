// 专家工作台（四期 B23①）：把问答卡上的选择整理成提交内容；只做前端校验提示，规则以后端为准。

export const CHOICE_LABELS = { yes: "对", no: "不对", unsure: "不确定" };

// state: { [questionId]: { choice, value } }；fill: { [slotKey]: text }，全空视为没用填空卡
export function buildAnswer(card, state, fill, comment = "") {
  const answers = card.questions
    .filter((q) => state[q.id]?.choice)
    .map((q) => ({ id: q.id, choice: state[q.id].choice, value: (state[q.id].value || "").trim() }));
  const usedFill = Object.values(fill || {}).some((v) => (v || "").trim());
  return { answers, fill: usedFill ? fill : null, comment: comment.trim() };
}

// 返回要提示给专家的问题（空数组 = 可以提交）
export function problems(card, state, fill) {
  const out = [];
  if (!state.keep?.choice) out.push("先回答“这条值得收进知识库吗”");
  for (const q of card.questions) {
    const a = state[q.id];
    if (q.kind === "field" && a?.choice === "no" && !(a.value || "").trim()) out.push(`${q.text.split("写的是")[0]}：请写上对的`);
  }
  const usedFill = Object.values(fill || {}).some((v) => (v || "").trim());
  const agreed = card.questions.some((q) => q.kind === "issue" && state[q.id]?.choice === "yes");
  if (state.keep?.choice === "yes" && agreed && !usedFill) out.push("你认可 Agent 提的问题：请用填空卡改正文，或选“不收”");
  if (usedFill) {
    for (const s of card.template.slots) {
      if (s.required && !(fill[s.key] || "").trim()) out.push(`填空卡还要填：${s.label}`);
    }
  }
  return out;
}
