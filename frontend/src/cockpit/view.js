// 驾驶舱视图计算（四期 A37，docs/交付链输入输出方案.md 8.1）：项目左栏交付链、全景三块。
// 只用 GET /api/projects/{code} 与 .../panorama 已有字段，纯函数，便于 node --test。

// 交付链四个环节（竖排，先后流程）；设置、调研助手不在链上
export const CHAIN = [["input", "输入"], ["requirements", "需求"], ["analysis", "分析"], ["output", "输出"]];

// 当前关口落在哪个环节：启动、调研两个关口在收输入；分析关口在分析；设计、验收（及其后的落地辅导）在输出。
// 需求（定制项）横贯全程，只在走过它之后标已完成。
export function chainIndex(gateIdx) {
  if (gateIdx <= 1) return 0;
  return gateIdx === 2 ? 2 : 3;
}

const pendingCust = (items) => (items || []).filter((i) => i.status === "proposed" || i.status === "pending_pm").length;

// 左栏交付链：每个环节的状态（ok 已过 / cur 当前 / "" 未到）与待办数
export function chainSteps(p, pano, cust) {
  const cols = pano?.columns || [];
  const allDone = cols.length > 0 && !pano.current;
  const gi = cols.findIndex((c) => c.id === pano?.current);
  const at = allDone ? CHAIN.length : gi < 0 ? -1 : chainIndex(gi);
  const badge = {
    input: 0,
    requirements: pendingCust(cust),
    analysis: p?.open_confirmations || 0,
    output: (p?.deliverables || []).filter((d) => d.status === "review").length,
  };
  return CHAIN.map(([seg, label], i) => ({ seg, label, n: badge[seg], state: i < at ? "ok" : i === at ? "cur" : "" }));
}

// D1～D8 状态点：已签绿、在做琥珀、未开始灰
export const delivDot = (status) => (status === "signed" ? "ok" : status === "review" || status === "drafting" ? "cur" : "");

// 进度环：已签署 / 全部交付件
export function progress(p) {
  const all = p?.deliverables?.length || 0;
  const signed = (p?.deliverables || []).filter((d) => d.status === "signed").length;
  return { signed, all, pct: all ? Math.round((100 * signed) / all) : 0 };
}

// 关口进度线：按产品配置的阶段，已过 / 进行中 / 未到
export function gates(pano) {
  return (pano?.columns || []).map((c, i) => ({ id: c.id, n: i + 1, name: c.name, outputs: c.outputs || [],
    state: c.status === "done" ? "ok" : c.status === "cur" ? "cur" : "" }));
}

const WHO = { client: "客户", consultant: "顾问", manager: "项目经理" };
const STEP = { not_started: 0, drafting: 34, review: 67, signed: 100 };

// “过关口还差”：当前关口里还没办完、要人做的事，每件一张卡（进度 + 卡在谁手里）。
// 交付件：起草中 / 有未处理确认卡 → 顾问；待签署 → 签署人；客户资料未到 → 客户。AI·平台泳道不上全景。
export function remaining(p, pano) {
  const cur = pano?.current;
  if (!cur) return [];
  const nodes = pano.nodes.filter((n) => n.col === cur);
  const out = [];
  const ev = nodes.find((n) => n.id === "client:evidence");
  if (ev && ev.status !== "ok") out.push({ id: ev.id, title: "客户资料", pct: 0, who: WHO.client, note: ev.subtitle, target: ev.target });
  const status = Object.fromEntries((p?.deliverables || []).map((d) => [d.code, d.status]));
  for (const n of nodes.filter((x) => x.type === "deliverable")) {
    const dc = n.id.slice("deliv:".length);
    const st = status[dc] || "not_started";
    if (st === "signed") continue;
    const blocked = n.status === "blk";
    const who = st === "review" ? "签署人" : WHO.consultant;
    const note = blocked ? "有待确认事项" : { not_started: "未开始", drafting: "起草中", review: "待签署" }[st];
    out.push({ id: n.id, title: n.title, pct: STEP[st] ?? 0, who, note, blocked, target: n.target });
  }
  return out;
}

// 影响面 = 沿连线下游能到达的节点数（卡住 D3 的卡也卡住第 4 步、D4……）
export function impact(data, id) {
  const seen = new Set();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop();
    for (const e of data.edges) if (e.from === cur && !seen.has(e.to)) { seen.add(e.to); stack.push(e.to); }
  }
  return seen.size;
}

// 待我处理：未处理的确认卡 + 待签署的交付件，按影响面从大到小；同影响面确认卡在前
export function todos(data) {
  const rows = data.nodes
    .filter((n) => n.type === "card" || (n.type === "signoff" && n.status === "cur"))
    .map((n) => ({ ...n, impact: impact(data, n.id) }));
  return rows.sort((a, b) => b.impact - a.impact || (a.type === "card" ? 0 : 1) - (b.type === "card" ? 0 : 1));
}

// 只看我的：落在本人泳道（顾问；项目经理另含签署）的待办
export const mine = (data, rows) => rows.filter((r) => data.my_lanes.includes(r.lane));
