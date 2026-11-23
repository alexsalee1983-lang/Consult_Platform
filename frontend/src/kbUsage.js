// 四期 A17：知识使用可见——把后端 describe() 给的元数据拼成一行说明（版本、权威等级、依据、待复查）。

export function knowledgeLine(it) {
  if (!it) return "";
  if (!it.visible) return "无权查看这条知识的详情";
  const parts = [`v${it.version}`, it.authority_label || "内部（不分级）"];
  const basis = [it.ref_code || it.ref_document, it.ref_version].filter(Boolean).join(" ");
  if (basis) parts.push(`依据 ${basis}`);
  if (it.recheck_state === "pending" || it.recheck_state === "failed") parts.push(it.flagged ? "待复查（顾问反馈）" : "待复查");
  return parts.join(" · ");
}

export function itemsById(items) {
  return Object.fromEntries((items || []).map((it) => [it.doc_id, it]));
}
