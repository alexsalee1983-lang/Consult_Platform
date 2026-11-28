// 四期 A41（docs/交付链输入输出方案.md 8.1“顾问入口收敛”）：驾驶舱右侧抽屉的纯逻辑。
// 当前页标签随消息发给后端（POST .../messages 的 page，只拼进本轮发给模型的内容，不存）；
// 后端只收单行短标签（字母数字汉字、空格、/ · ～ -，≤40 字），这里先按同一规则清洗。
const PAGES = { "": "全景", input: "输入", requirements: "需求", analysis: "分析", output: "输出", settings: "设置", deliverables: "输出" };
const MAX = 40;
const clean = (s) => (s || "").replace(/[^\p{L}\p{N}_ /·～-]/gu, "").replace(/\s+/g, " ").trim();

export function pageContext(pathname, title = "") {
  const [, , , seg = "", sub = ""] = pathname.split("/");
  const page = PAGES[seg] ?? "全景";
  let dcode = "";
  try { dcode = decodeURIComponent(sub); } catch { /* 非法编码不带 */ }
  if (seg !== "deliverables" || !/^[A-Za-z0-9_-]{1,12}$/.test(dcode)) return page;
  return `${page} / ${dcode} ${clean(title)}`.trim().slice(0, MAX).trim();
}

// 同一项目同一会话：沿用我在本项目最近一次与咨询师 Agent 的对话（列表已按更新时间倒序），没有就发第一条时新建
export const DRAWER_AGENT = "consultant-lead";
export function pickConversation(convs) {
  return (convs || []).find((c) => c.agent_code === DRAWER_AGENT) || null;
}

// “待我处理”：复用“我的任务”（确认卡、待签署、自评等合一，已按影响排好），只留本项目；卡在别人手里的排后
export function projectTodos(data, code) {
  if (!data) return [];
  const rows = data.tasks.filter((t) => t.project_code === code)
    .map((t) => ({ ...t, kindName: data.kinds?.[t.kind]?.name || t.kind }));
  return [...rows.filter((t) => !t.blocked), ...rows.filter((t) => t.blocked)];
}
