// 来源登记表页面规则（三期 B16②）。与后端 app/kb/source_registry.py 保持一致：
// 页面只用它决定显示哪些按钮，最终以后端校验为准（后端 403/409/422 的原因原样显示）。

export const SOURCE_STATUS = {
  candidate: { label: "候选", cls: "c-n" },
  trial: { label: "试用", cls: "c-c" },
  active: { label: "正式", cls: "c-g" },
  demoted: { label: "降级", cls: "c-a" },
  retired: { label: "停用", cls: "c-r" },
};

export const TRANSITIONS = {
  candidate: ["trial", "retired"],
  trial: ["active", "candidate", "retired"],
  active: ["demoted", "retired"],
  demoted: ["active", "retired"],
  retired: ["candidate"],
};

export const NEEDS_REASON = new Set(["demoted", "retired"]);

// B27 最小版：七类从高到低，等级三级
export const AUTHORITY_KINDS = ["法律", "行政法规", "监管通报", "国家标准", "行业标准", "白皮书", "厂商资料"];
export const AUTHORITY_LEVEL_LABEL = { 1: "一级 · 法律法规与监管文件", 2: "二级 · 标准", 3: "三级 · 参考资料" };

const ACTION_LABEL = {
  trial: "转试用", active: "批准为正式", candidate: "退回候选", demoted: "降级", retired: "停用",
};

// 某条来源对当前用户可用的状态流转按钮：[{to, label, needsReason, disabled, hint}]
export function sourceActions(src, user) {
  const roles = user?.roles || [];
  const isAdmin = roles.includes("knowledge_admin");
  return (TRANSITIONS[src.status] || []).map((to) => {
    let hint = "";
    if (to === "active" && !isAdmin) hint = "转正式只限知识管理员";
    else if (to === "active" && src.created_by === user?.username) hint = "登记人不能批准自己登记的来源";
    const label = src.status === "retired" && to === "candidate" ? "重新登记为候选" : ACTION_LABEL[to];
    return { to, label, needsReason: NEEDS_REASON.has(to), disabled: Boolean(hint), hint };
  });
}

// 登记表单前端预检（后端仍会校验并给出 422 原因）
export function validateSource(f) {
  if (!f.name.trim()) return "来源名称必填";
  if (!/^https?:\/\/[^\s/]+/i.test(f.url.trim())) return "来源地址必须是 http 或 https 网址";
  if (!f.license.trim()) return "许可必填：写明能否内部引用、是否须注明出处";
  if (!AUTHORITY_KINDS.includes(f.authority_kind)) return "权威类别必填";
  return "";
}
