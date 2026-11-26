// 四期 E3②：客户自评包。客户在离线网页导出的加密答卷（kind = selfeval-answers）由 fieldPackage.js 在浏览器本机解密，
// 口令不上传；这里把解密后的正文核对成导入接口的请求体。包号、版本、校验码不对的在本机先拦下，平台再校验一遍。

export const SELFEVAL_STATUS = {
  issued: ["已生成", "c-a"], sent: ["已发出", "c-a"], imported: ["已导入", "c-g"], superseded: ["已作废", ""],
};
export const REMIND_TEXT = { 1: "明天到期", 2: "今天到期", 3: "逾期，已升级项目经理" };

export function importBody(kind, body, pack, confirm = false) {
  if (kind !== "selfeval-answers") throw new Error("这不是客户自评答卷（请让客户在问卷里点“导出加密答卷”）");
  if (!body || body.format !== "cp-selfeval-answers" || !Array.isArray(body.answers)) throw new Error("答卷内容格式不对");
  if (body.code !== pack.code) throw new Error(`这份答卷属于自评包 ${body.code}，不是 ${pack.code}`);
  if (body.selfeval_version !== pack.version) throw new Error(`这份答卷是第 ${body.selfeval_version} 版，当前是第 ${pack.version} 版`);
  return {
    version: body.selfeval_version, pack_hash: String(body.pack_hash || ""), confirm,
    answers: body.answers.map((a) => ({ qid: String(a.qid || ""), value: String(a.value || ""), note: String(a.note || "") })),
  };
}

// 导入返回 409 needs_confirm 时给顾问看的说明；不是这种 409 返回空串
export function confirmText(detail) {
  if (!detail?.needs_confirm) return "";
  const parts = [`共 ${detail.total} 题，未答 ${detail.unanswered} 题`];
  if (detail.comparable) parts.push(`与资料、现场证据可比 ${detail.comparable} 题，不一致 ${detail.mismatch} 题`);
  return `${parts.join("；")}。${detail.message || ""}`;
}

export function importSummary(r) {
  return `已导入答案池（客户自评）：事实 ${r.facts} 条，自动确认 ${r.auto_confirmed} 条，不清楚 ${r.unclear} 处，`
    + `未答 ${r.unanswered} 题，跨来源矛盾 ${r.conflicts} 处`;
}
