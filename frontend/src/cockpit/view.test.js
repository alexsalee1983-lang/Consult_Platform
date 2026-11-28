import { test } from "node:test";
import assert from "node:assert/strict";
import { chainIndex, chainSteps, delivDot, gates, mine, progress, remaining, todos } from "./view.js";

const P = {
  open_confirmations: 2,
  deliverables: [{ code: "D1", status: "signed" }, { code: "D2", status: "review" }, { code: "D3", status: "drafting" },
    { code: "D4", status: "not_started" }],
};
const PANO = {
  current: "s1", my_lanes: ["consultant"],
  columns: [{ id: "s0", name: "启动", status: "done", outputs: ["D1"] }, { id: "s1", name: "调研", status: "cur", outputs: ["D2", "D3"] },
    { id: "s2", name: "分析", status: "next", outputs: ["D4"] }],
  nodes: [
    { id: "client:evidence", col: "s1", lane: "client", type: "evidence", status: "cur", subtitle: "已收 0 份", target: "/project/BANK-A" },
    { id: "deliv:D2", col: "s1", lane: "consultant", type: "deliverable", status: "ok", title: "D2 调研包", target: "/d/D2" },
    { id: "sign:D2", col: "s1", lane: "manager", type: "signoff", status: "cur", title: "签署 D2", target: "/d/D2" },
    { id: "deliv:D3", col: "s1", lane: "consultant", type: "deliverable", status: "blk", title: "D3 事实清单", target: "/d/D3" },
    { id: "card:5", col: "s1", lane: "consultant", type: "card", status: "blk", title: "矛盾", target: "drawer:card:5" },
    { id: "step:x", col: "s1", lane: "ai", type: "step", status: "ai", title: "第 2 步", target: "drawer:run" },
    { id: "deliv:D4", col: "s2", lane: "consultant", type: "deliverable", status: "todo", title: "D4 差距分析", target: "/d/D4" },
  ],
  edges: [{ from: "card:5", to: "deliv:D3" }, { from: "deliv:D3", to: "deliv:D4" }],
};

test("关口落到交付链环节：启动、调研在输入，分析在分析，其后在输出", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5].map(chainIndex), [0, 0, 2, 3, 3, 3]);
});

test("左栏交付链：当前关口调研 → 输入为当前；待办数取定制项待定、确认卡、待签署", () => {
  const cust = [{ status: "pending_pm" }, { status: "proposed" }, { status: "active" }];
  const s = chainSteps(P, PANO, cust);
  assert.deepEqual(s.map((x) => [x.label, x.state, x.n]), [["输入", "cur", 0], ["需求", "", 2], ["分析", "", 2], ["输出", "", 1]]);
  const later = chainSteps(P, { ...PANO, current: "s2" }, []);
  assert.deepEqual(later.map((x) => x.state), ["ok", "ok", "cur", ""]);
  const done = chainSteps(P, { ...PANO, current: "" }, []);
  assert.deepEqual(done.map((x) => x.state), ["ok", "ok", "ok", "ok"]);
  assert.deepEqual(chainSteps(P, null, null).map((x) => x.state), ["", "", "", ""]);
});

test("状态点与进度环", () => {
  assert.deepEqual(["signed", "review", "drafting", "not_started"].map(delivDot), ["ok", "cur", "cur", ""]);
  assert.deepEqual(progress(P), { signed: 1, all: 4, pct: 25 });
  assert.deepEqual(progress({ deliverables: [] }), { signed: 0, all: 0, pct: 0 });
});

test("关口进度线：按产品阶段编号，已过 / 当前 / 未到", () => {
  assert.deepEqual(gates(PANO).map((g) => [g.n, g.name, g.state]), [[1, "启动", "ok"], [2, "调研", "cur"], [3, "分析", ""]]);
});

test("过关口还差：只列当前关口要人做的事，带卡在谁手里；AI 步骤不上", () => {
  const r = remaining(P, PANO);
  assert.deepEqual(r.map((x) => [x.title, x.who, x.pct]), [["客户资料", "客户", 0], ["D2 调研包", "签署人", 67], ["D3 事实清单", "顾问", 34]]);
  assert.equal(r[2].note, "有待确认事项");
  assert.deepEqual(remaining(P, { ...PANO, current: "" }), []);
});

test("待我处理：按影响排，只看我的按泳道筛", () => {
  const t = todos(PANO);
  assert.deepEqual(t.map((x) => x.id), ["card:5", "sign:D2"]);
  assert.deepEqual(mine(PANO, t).map((x) => x.id), ["card:5"]);
});
