// 四期 A26 交付件页四个模式：① 起草 ② 评审与修订 ③ 定稿签署 ④ 客户确认。模式由状态推出，不单独存。
export const MODES = ["起草", "评审与修订", "定稿签署", "客户确认"];

// d：交付件；so：签署状态（signoff）；rv：一键修订状态（revise，可缺）
export function modeOf(d, so, rv) {
  const status = so?.status || d?.status;
  if (status === "signed") return 3;
  if (status === "review") return 2;
  if (so?.latest_review || ["queued", "running"].includes(rv?.status)) return 1;
  return 0;
}

// 写签分离提示：起草人、最后修改人和当前用户能否签
export function separationNote(so) {
  if (!so) return "";
  if (so.separation_exempt) return "对内交付件，不要求写签分离";
  const who = [...new Set([...(so.drafters || []), ...(so.last_modifiers || [])])].join("、");
  return who ? `写签分离：起草人 / 最后修改人 ${who} 不能签署，需另一位项目经理签` : "";
}

export function reviseNote(rv) {
  if (!rv) return "";
  if (rv.status === "queued" || rv.status === "running") return "一键修订进行中，改完自动复查";
  if (rv.status === "failed") return `一键修订失败：${rv.error || "请稍后重试"}`;
  if (rv.handoff) return `已自动修 ${rv.max_rounds} 轮，仍有 ${rv.mechanical_open} 处机械问题，转人工处理`;
  if (rv.status === "done") return "一键修订已完成，复查通过的机械问题已清零";
  return "";
}
