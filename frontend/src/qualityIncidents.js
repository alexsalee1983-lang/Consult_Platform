// AI 质量事件台账（四期 F3②）：页面规则，与后端 app/services/quality_incidents.py 保持一致。
// 事件描述和处置说明只记长度与摘要哈希，页面不回显原文。
export const CATEGORIES = {
  fabricated_citation: "编造或错引条款",
  repealed_basis: "引用已废止依据",
  fact_mismatch: "事实与证据不符",
  qualification_wording: "资质边界用语",
  cross_project_leak: "客户信息跨项目泄露",
  improper_advice: "明显不当建议",
};
export const REF_KINDS = { deliverable: "交付件", kb_item: "知识条目", run: "运行记录" };
export const LEVELS = ["P1", "P2", "P3"];
export const LEVEL_CLS = { P1: "c-r", P2: "c-a", P3: "c-n" };
export const STEPS = [
  { key: "register", label: "登记" },
  { key: "grade", label: "分级" },
  { key: "contain", label: "遏制" },
  { key: "assess", label: "影响评估" },
  { key: "correct", label: "纠正" },
  { key: "root_cause", label: "根因" },
  { key: "prevent", label: "预防" },
  { key: "review", label: "复盘关闭" },
];
export const STEP_LABEL = Object.fromEntries(STEPS.map((s) => [s.key, s.label]));

// 后端 View.status：最后一步的键，登记后为 registered、复盘后为 closed
export function statusLabel(status) {
  if (status === "closed") return "已关闭";
  if (status === "registered") return "已登记";
  return `已${STEP_LABEL[status] || status}`;
}
export const ACTIONS = {
  freeze_export: "冻结导出",
  offline_item: "下线条目",
  rollback_agent: "回滚 Agent 版本",
  lower_autonomy: "降自主度",
};
export const ROOT_CAUSES = {
  knowledge: "知识", retrieval: "检索", md: "md", model: "模型", gate: "门禁", human_signoff: "人工签署",
};
export const NOTE_MAX = 2000;

// 与后端 add_event 的校验顺序一致；返回错误文字，通过返回 null。
export function validateEvent({ step, level = "", action = "", root_cause = "", note = "", agent = "", version = "" }) {
  const keys = STEPS.slice(1).map((s) => s.key);
  if (!keys.includes(step)) return "处置步骤不在八步内";
  if (step === "grade" && !LEVELS.includes(level)) return "分级只能是 P1、P2、P3";
  if (step === "contain" && !(action in ACTIONS)) return "遏制措施不在清单内";
  if (step === "contain" && action === "rollback_agent" && !agent) return "回滚 Agent 版本须选 Agent";
  if (step === "contain" && action === "rollback_agent" && !version) return "回滚 Agent 版本须选目标版本";
  if (step === "root_cause" && !(root_cause in ROOT_CAUSES)) return "根因类别不在清单内";
  if ((note || "").length > NOTE_MAX) return `说明不超过 ${NOTE_MAX} 字`;
  return null;
}

// 只发本步需要的字段
export function eventPayload({ step, level = "", action = "", root_cause = "", note = "", agent = "", version = "" }) {
  const p = { step };
  if (step === "grade") p.level = level;
  if (step === "contain") p.action = action;
  if (step === "contain" && action === "rollback_agent") Object.assign(p, { agent, version });
  if (step === "root_cause") p.root_cause = root_cause;
  p.note = note || "";
  return p;
}

// 建议的下一步：最后一步之后的那一步；已复盘关闭返回 null。允许重复某一步（例如多次遏制），由人选。
export function nextStep(events) {
  const order = STEPS.map((s) => s.key);
  const last = events.length ? Math.max(...events.map((e) => order.indexOf(e.step))) : 0;
  if (order[last] === "review") return null;
  return order[Math.max(last, 0) + 1];
}

const HOUR = 3600 * 1000;

export function dueText(inc, now = new Date()) {
  if (!inc.contain_due) return inc.level ? "不设时限" : "未分级";
  if (inc.contained_at) return inc.overdue ? "遏制超时" : "已遏制";
  const left = new Date(inc.contain_due) - now;
  if (left < 0 || inc.overdue) return `已超时 ${Math.max(1, Math.floor(-left / HOUR))} 小时`;
  return `剩 ${Math.max(1, Math.floor(left / HOUR))} 小时`;
}

// 回滚 Agent 版本（四期 F3②g）：版本库历史来自 GET /api/agent-md/{代号}/versions；
// 在用版本 = 回滚指针（有的话）或仓库当前版本，目标不能等于在用版本（后端同样 422）。
export function rollbackVersions(history) {
  if (!history) return [];
  const inUse = history.pinned || history.current;
  return (history.items || []).filter((x) => x.version !== inUse);
}

export function versionLabel(item, history) {
  const parts = [item.version];
  if (item.md_version) parts.push(item.md_version);
  if (item.captured_at) parts.push(item.captured_at.slice(0, 10));
  if (history && item.version === history.current) parts.push("仓库当前");
  return parts.join(" · ");
}
