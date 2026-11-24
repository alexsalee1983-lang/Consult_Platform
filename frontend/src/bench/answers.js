// 专家工作台（四期 B23①）：把问答卡上的选择整理成提交内容；只做前端校验提示，规则以后端为准。

export const CHOICE_LABELS = { yes: "对", no: "不对", unsure: "不确定" };

// state: { [questionId]: { choice, value } }；fill: { [slotKey]: text }，全空视为没用填空卡
export function buildAnswer(card, state, fill, comment = "", deidAck = false) {
  const answers = card.questions
    .filter((q) => state[q.id]?.choice)
    .map((q) => ({ id: q.id, choice: state[q.id].choice, value: (state[q.id].value || "").trim() }));
  const usedFill = Object.values(fill || {}).some((v) => (v || "").trim());
  return { answers, fill: usedFill ? fill : null, comment: comment.trim(), deid_ack: !!deidAck };
}

// 返回要提示给专家的问题（空数组 = 可以提交）
export function problems(card, state, fill) {
  const out = [];
  if (!state.keep?.choice) out.push("先回答“这条值得收进知识库吗”");
  if (card.questions.some((q) => q.kind === "conflict") && !state.conflict?.choice) out.push("先回答冲突处理：和已有条目重复或矛盾时怎么办");
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

// 四期 B23②：去标识预览要看的文字（专家自己写的：填空卡各格 + 不收的理由），键与后端回来的 texts 对应
export function previewTexts(state, fill) {
  const out = {};
  for (const [k, v] of Object.entries(fill || {})) if ((v || "").trim()) out[`fill:${k}`] = v;
  if ((state.keep?.value || "").trim()) out.keep = state.keep.value;
  return out;
}

// 把去标识后的文字换回填空卡和“不收的理由”
export function applyPreview(state, fill, texts) {
  const nextFill = { ...fill };
  let nextState = state;
  for (const [k, v] of Object.entries(texts || {})) {
    if (k.startsWith("fill:")) nextFill[k.slice(5)] = v;
    else if (k === "keep") nextState = { ...state, keep: { ...state.keep, value: v } };
  }
  return { state: nextState, fill: nextFill };
}
