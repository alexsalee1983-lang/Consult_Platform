// 页面冒烟级组件测试样板（四期 质量）：接口一律打桩、只用虚拟客户；后续 PR 照此给改动的页面加测试。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, expect, test, vi } from "vitest";

const stub = {};   // 每个用例按需填接口返回；未填的接口一直挂起（页面停在加载中，不影响断言）
vi.mock("../api.js", async (orig) => ({
  ...(await orig()),
  api: new Proxy({}, { get: (_, k) => stub[k] || (() => new Promise(() => {})) }),
}));
vi.mock("../auth.jsx", async (orig) => ({
  ...(await orig()),
  useAuth: () => ({ status: "anon", user: { username: "u1", roles: ["consultant"] }, login: stub.login }),
}));

const { default: Login } = await import("./Login.jsx");
const { Toc } = await import("./Deliverable.jsx");
const { default: Projects } = await import("./Projects.jsx");
const { default: Overview } = await import("./Overview.jsx");
const { OutputPage, ProjectLayout, RequirementsPage, SettingsPage } = await import("./ProjectPages.jsx");
const { default: Drawer } = await import("../components/Drawer.jsx");
const { todos } = await import("../cockpit/view.js");
const { default: Members } = await import("../components/Members.jsx");

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
vi.stubGlobal("localStorage", globalThis.jsdom.window.localStorage);   // Node 25+ 自带的 localStorage 会盖住 jsdom 的
let root, el;
async function render(node, path = "/") {
  el = document.body.appendChild(document.createElement("div"));
  await act(async () => { root = createRoot(el); root.render(<MemoryRouter initialEntries={[path]}>{node}</MemoryRouter>); });
  return el;
}
afterEach(() => { act(() => root.unmount()); el.remove(); for (const k in stub) delete stub[k]; });

const PROJECT = {
  code: "BANK-A", service: "安全运营中心建设", industry: "银行", tier: "standard", phase: 1, phase_name: "调研",
  week: 3, total_weeks: 12, next_milestone: "现状访谈", posture_score: 72, open_confirmations: 0,
  retention_state: "active", deliverables: [{ code: "D1", title: "现状差距分析", status: "drafting" }], confirmations: [],
};

test("登录页：三项都填了才能点登录", async () => {
  const c = await render(<Login />);
  const btn = c.querySelector("button[type=submit]");
  expect(btn.disabled).toBe(true);
  expect(c.textContent).toContain("安全咨询专家平台");
});

test("我的项目：列出虚拟项目，链接到项目驾驶舱", async () => {
  stub.projects = async () => [PROJECT];
  stub.confirmations = async () => [];
  const c = await render(<Projects />);
  const link = c.querySelector('a[href="/project/BANK-A"]');
  expect(link?.textContent).toContain("安全运营中心建设");
  expect(c.textContent).toContain("没有待确认事项");
});

// 全景数据：确认卡 card:7 卡住 D3 → 第 4 步 → D4（影响 3）；card:8 只连 D4（影响 1）；D1 待签署（影响 0）
const PANO = {
  project: "BANK-A", product: "soc", product_name: "SOC 建设", current: "c1", my_lanes: ["consultant"],
  columns: [{ id: "c0", name: "启动", status: "done" }, { id: "c1", name: "调研", status: "cur" }, { id: "c2", name: "分析", status: "next" }],
  lanes: [{ id: "client", name: "客户" }, { id: "consultant", name: "顾问" }, { id: "manager", name: "项目经理" }, { id: "ai", name: "AI·平台" }],
  nodes: [
    { id: "step:s1", col: "c0", lane: "ai", type: "step", status: "ok", title: "第 1 步 启动", subtitle: "", target: "drawer:run" },
    { id: "step:s2", col: "c1", lane: "ai", type: "step", status: "ok", title: "第 2 步 调研", subtitle: "", target: "drawer:run" },
    { id: "deliv:D1", col: "c1", lane: "consultant", type: "deliverable", status: "cur", title: "D1 现状差距分析", subtitle: "待签署", target: "/project/BANK-A/deliverables/D1" },
    { id: "sign:D1", col: "c1", lane: "manager", type: "signoff", status: "cur", title: "签署 D1", subtitle: "", target: "/project/BANK-A/deliverables/D1" },
    { id: "card:8", col: "c1", lane: "consultant", type: "card", status: "blk", title: "证据缺口 · 机房", subtitle: "", target: "drawer:card:8" },
    { id: "card:7", col: "c1", lane: "consultant", type: "card", status: "blk", title: "组织与人员矛盾", subtitle: "", target: "drawer:card:7" },
    { id: "deliv:D3", col: "c2", lane: "consultant", type: "deliverable", status: "todo", title: "D3 事实清单", subtitle: "", target: "/project/BANK-A/deliverables/D3" },
    { id: "step:s4", col: "c2", lane: "ai", type: "step", status: "todo", title: "第 4 步 差距", subtitle: "", target: "drawer:run" },
    { id: "deliv:D4", col: "c2", lane: "consultant", type: "deliverable", status: "todo", title: "D4 差距分析", subtitle: "", target: "/project/BANK-A/deliverables/D4" },
  ],
  edges: [{ from: "card:7", to: "deliv:D3" }, { from: "deliv:D3", to: "step:s4" }, { from: "step:s4", to: "deliv:D4" },
    { from: "card:8", to: "deliv:D4" }, { from: "deliv:D1", to: "sign:D1" }],
};

function stubOverview() {
  stub.project = async () => ({ ...PROJECT, open_confirmations: 2,
    confirmations: [{ id: 7, title: "组织与人员矛盾", kind: "conflict", agent: "fact", options: [], recommendation: "", reason: "", impact: "" }] });
  stub.health = async () => ({ llm_external: true });
  stub.evidences = async () => [];
  stub.panorama = async () => PANO;
}

test("待办按影响排序：卡住下游越多越靠前，同影响确认卡在签署前", () => {
  expect(todos(PANO).map((t) => [t.id, t.impact])).toEqual([["card:7", 3], ["card:8", 1], ["sign:D1", 0]]);
});

test("全景三块：顶部一行（进度环、第 N 周 · 阶段、还差几件、按期）→ 关口进度线 → 过关口还差 + 待我处理；泳道图与行动条已取消", async () => {
  stubOverview();
  const c = await render(<Overview code="BANK-A" />);
  expect(c.querySelector("h1").textContent).toBe("项目驾驶舱 · 安全运营中心建设");
  expect(c.textContent).toContain("当前为外部测试模型");
  const hero = c.querySelector('section[aria-label="项目进度"]');
  expect(hero.textContent).toContain("第 3 周 · 调研阶段");
  expect(hero.textContent).toContain("过关口 2「调研」还差 1 件事");
  expect(hero.querySelector(".ring").getAttribute("aria-label")).toBe("已签署 0 / 1");
  expect(hero.textContent).toContain("按期");
  expect(hero.nextElementSibling.getAttribute("aria-label")).toBe("关口进度线");
  const stns = [...c.querySelectorAll(".metro .stn")];
  expect(stns.map((s) => s.querySelector("b").textContent)).toEqual(["启动", "调研", "分析"]);
  expect(stns.map((s) => s.className)).toEqual(["stn ok", "stn cur", "stn"]);
  const left = c.querySelector('section[aria-label="过关口还差"]');
  expect(left.querySelector("h2").textContent).toBe("过关口 2 还差");
  expect([...left.querySelectorAll(".bt")].map((b) => b.textContent)).toEqual(["D1 现状差距分析起草中卡在：顾问"]);
  expect(c.querySelector("svg")).toBeNull();
  expect(c.querySelector('section[aria-label="行动条"]')).toBeNull();
  expect(c.textContent).not.toContain("自动完成");
});

test("待我处理：一行一件事按影响排，“只看我的”只留本人泳道", async () => {
  stubOverview();
  const c = await render(<Overview code="BANK-A" />);
  const box = c.querySelector('section[aria-label="待我处理"]');
  const rows = () => [...box.querySelectorAll("li.todo")].map((li) => li.textContent.replace(/影响.*/, ""));
  expect(rows()).toEqual(["确认卡组织与人员矛盾", "确认卡证据缺口 · 机房", "签署签署 D1"]);
  await act(async () => [...box.querySelectorAll("button")].find((b) => b.textContent === "只看我的").click());
  expect(rows()).toEqual(["确认卡组织与人员矛盾", "确认卡证据缺口 · 机房"]);
});

test("全景：项目只有项目经理一人时，顶部提示先添加成员并链到设置页", async () => {
  stubOverview();
  stub.members = async () => [{ username: "u1", display_name: "经理甲", project_role: "manager" }];
  const c = await render(<Overview code="BANK-A" />);
  const hero = c.querySelector('section[aria-label="项目进度"]');
  expect(hero.querySelector('a[href="/project/BANK-A/settings"]')?.textContent).toContain("先添加项目成员");
});

test("确认卡在抽屉里就地办：点“处理”打开，Esc 关闭；⌖ 带 focus=card 直接打开", async () => {
  stubOverview();
  let c = await render(<Overview code="BANK-A" />);
  await act(async () => c.querySelector('section[aria-label="待我处理"] li.todo button').click());
  expect(c.querySelector('dialog[aria-label="确认卡"]')?.textContent).toContain("组织与人员矛盾");
  expect(c.querySelector('dialog a[href="/project/BANK-A/analysis"]')).toBeTruthy();
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
  expect(c.querySelector("dialog")).toBeNull();
  act(() => root.unmount()); el.remove();
  stubOverview();
  c = await render(<Overview code="BANK-A" focus="card:7" />);
  expect(c.querySelector('dialog[aria-label="确认卡"]')).toBeTruthy();
});

test("⌖ 全景定位：focus 为交付件时高亮“过关口还差”里对应的卡", async () => {
  stubOverview();
  const c = await render(<Overview code="BANK-A" focus="deliv:D1" />);
  expect(c.querySelector(".bt.flash")?.getAttribute("data-node")).toBe("deliv:D1");
});

test("项目栏：项目卡 + 新建项目 + 全景 + 输入→需求→分析→输出竖向交付链 + D1～D8 + 调研助手 + 设置；交付件页算在“输出”下", async () => {
  stub.project = async () => PROJECT;
  stub.panorama = async () => PANO;
  stub.customizations = async () => ({ items: [{ id: 1, status: "pending_pm" }], needs_update: {} });
  const c = await render(
    <Routes><Route path="/project/:code" element={<ProjectLayout />}><Route path="deliverables/:dcode" element={<p>交付件页</p>} /></Route></Routes>,
    "/project/BANK-A/deliverables/D1");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const rail = c.querySelector('nav[aria-label="项目导航"]');
  expect(rail.querySelector(".pcard").textContent).toContain("BANK-A");
  expect(rail.querySelector('a[href="/projects?new=1"]').textContent).toBe("＋ 新建项目");
  expect(rail.querySelector('a[href="/project/BANK-A"]').textContent).toBe("◎ 全景3");
  const chain = [...rail.querySelectorAll('[aria-label="交付链"] a')];
  expect(chain.map((a) => a.textContent)).toEqual(["输入", "需求1", "分析", "输出"]);
  expect(chain.map((a) => a.dataset.state)).toEqual(["cur", "todo", "todo", "todo"]);
  expect(chain.find((a) => a.classList.contains("on"))?.textContent).toBe("输出");
  const d1 = rail.querySelector('[aria-label="交付件"] a[href="/project/BANK-A/deliverables/D1"]');
  expect(d1.querySelector(".sd-cur")).toBeTruthy();
  expect(rail.querySelector('a[href="/project/BANK-A/survey"]').textContent).toContain("调研助手");
  expect(rail.querySelector('a[href="/project/BANK-A/settings"]')).toBeTruthy();
  expect(c.textContent).toContain("BANK-A / 输出 / D1");
  expect(c.textContent).toContain("交付件页");
  expect(c.querySelector("nav.tabs")).toBeNull();
});

test("需求页：定制项列来源与生效状态，另有修订指令、决定记录、变更单三类", async () => {
  stub.project = async () => PROJECT;
  stub.customizations = async () => ({ needs_update: {}, items: [
    { id: 3, code: "C-3", text: "统一称“安全运营中心”", source_name: "合同", kind_name: "口径", status: "active", status_name: "生效", deliverable: null },
    { id: 4, code: "C-4", text: "出具等保测评结论", source_name: "顾问对话", kind_name: "口径", status: "unacceptable", status_name: "不可接受", reason: "资质边界", deliverable: null }] });
  const c = await render(<RequirementsPage code="BANK-A" />);
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect([...c.querySelectorAll('nav[aria-label="需求分类"] button')].map((b) => b.textContent.replace(/\s.*$/, "")))
    .toEqual(["定制项", "修订指令", "决定记录", "变更单"]);
  const rows = [...c.querySelectorAll('section[aria-label="定制项"] tbody tr')];
  expect(rows.length).toBe(2);
  expect(rows[1].textContent).toContain("资质边界");
  expect(rows[1].querySelector(".chip.c-r").textContent).toBe("不可接受");
  await act(async () => [...c.querySelectorAll('nav[aria-label="需求分类"] button')][3].click());
  expect(c.querySelector('section[aria-label="变更单"]').textContent).toContain("超出合同范围");
});

test("输出页：列出交付件，每条有“⌖ 全景”回到对应节点", async () => {
  stub.project = async () => PROJECT;
  const c = await render(<OutputPage code="BANK-A" />);
  expect(c.querySelector('a[href="/project/BANK-A/deliverables/D1"]')?.textContent).toContain("现状差距分析");
  expect(c.querySelector('a[href="/project/BANK-A?focus=deliv%3AD1"]')?.textContent).toBe("⌖ 全景");
});

test("输出页（A33）：按阶段列输出包组件，已签署给只读查看，阶段包一键导出", async () => {
  stub.project = async () => PROJECT;
  stub.outputs = async () => ({
    stages: [{ stage: "启动", deliverables: [{ code: "D1", title: "现状差距分析", status: "signed", signed: true,
      components: [{ id: "D1-C01", name: "范围确认单", format: "docx", default: true, client_signs: true },
        { id: "D1-C09", name: "外包方案", format: "pptx", default: false }] }] }],
    closeout: [{ id: "R-C01", name: "项目复盘", format: "docx", default: true }],
  });
  stub.exportStage = vi.fn(async () => ({ name: "BANK-A-启动阶段包.zip" }));
  const c = await render(<OutputPage code="BANK-A" />);
  const sec = c.querySelector('section[aria-label="交付件"]');
  expect(sec.textContent).toContain("范围确认单 · Word");
  expect(sec.textContent).toContain("外包方案 · PPT（可选）");
  expect(sec.textContent).toContain("只读查看");
  expect(sec.textContent).toContain("项目复盘");
  await act(async () => [...sec.querySelectorAll("button")].find((b) => b.textContent === "导出阶段包").click());
  expect(stub.exportStage).toHaveBeenCalledWith("BANK-A", "启动");
  expect(sec.textContent).toContain("已导出 BANK-A-启动阶段包.zip");
});

test("抽屉：✕ 关闭，“查看全部”跳子页面", async () => {
  const onClose = vi.fn();
  const c = await render(<Drawer title="证据缺口" onClose={onClose} more={{ to: "/project/BANK-A/input", label: "查看全部" }}><p>BANK-A 机房</p></Drawer>);
  expect(c.querySelector("dialog[open]")?.getAttribute("aria-label")).toBe("证据缺口");
  expect(c.querySelector('a[href="/project/BANK-A/input"]').textContent).toBe("查看全部 →");
  await act(async () => c.querySelector('button[aria-label="关闭"]').click());
  expect(onClose).toHaveBeenCalledTimes(1);
});

// ---- 四期 A26 交付件页四模式 ----
const { default: Deliverable } = await import("./Deliverable.jsx");
const { modeOf, separationNote, reviseNote } = await import("../delivModes.js");

test("交付件模式：由状态推出 ① 起草 ② 评审与修订 ③ 定稿签署 ④ 客户确认", () => {
  expect(modeOf({ status: "drafting" }, { status: "drafting", latest_review: null })).toBe(0);
  expect(modeOf({}, { status: "drafting", latest_review: { id: 1 } })).toBe(1);
  expect(modeOf({}, { status: "drafting", latest_review: null }, { status: "running" })).toBe(1);
  expect(modeOf({}, { status: "review" })).toBe(2);
  expect(modeOf({}, { status: "signed" })).toBe(3);
  expect(separationNote({ separation_exempt: true })).toContain("对内");
  expect(separationNote({ drafters: ["u2"], last_modifiers: ["u3"] })).toContain("u2、u3");
  expect(reviseNote({ status: "done", handoff: true, max_rounds: 2, mechanical_open: 1 })).toContain("转人工");
});

const DELIV = {
  code: "D4", title: "差距分析报告", status: "review", revision: 1, updated_by: "u2", updated_at: "2026-11-25T08:00:00Z",
  sections: [], totals: { evidence: 0, knowledge: 0, inferred: 0, knowledge_updates: 0 },
};
const SO = {
  deliverable: "D4", status: "review", revision: 1, gate: true, reasons: [], latest_review: null, history: [],
  drafters: ["u1"], last_modifiers: ["u1"], separation_exempt: false, merged: false, merged_ready: false,
  sign_block: "写签分离：你是 D4 的起草人和最后修改人，不能签署自己写的对客户交付件，请由本项目另一位项目经理签署",
};
function stubDeliverable(d, so) {
  stub.project = async () => ({ ...PROJECT, deliverables: [{ code: d.code, title: d.title, status: d.status }] });
  stub.members = async () => [{ username: "u1", project_role: "manager" }];
  stub.deliverable = async () => d;
  stub.signoff = async () => so;
  stub.projectConfirmations = async () => [];
  stub.deliverableForm = async () => null;
  stub.reviseState = async () => ({ status: "", max_rounds: 2, mechanical_open: 0, pending_feedback: 0, handoff: false });
}

test("交付件页 ③ 定稿签署：写签分离时签署按钮禁用并给出中文原因", async () => {
  stubDeliverable(DELIV, SO);
  const c = await render(<Deliverable code="BANK-A" dcode="D4" />);
  expect(c.querySelector('[aria-current="step"]')?.textContent).toContain("定稿签署");
  expect(c.textContent).toContain("不能签署自己写的对客户交付件");
  const sign = [...c.querySelectorAll("button")].find((b) => b.textContent === "签署");
  expect(sign.disabled).toBe(true);
});

test("交付件页 ④ 客户确认：录入客户意见生成确认卡", async () => {
  const sent = [];
  stubDeliverable({ ...DELIV, status: "signed", sections: [{ seq: 1, title: "现状概述", current_version: 1, unresolved: [], knowledge_updates: [] }] },
    { ...SO, status: "signed", sign_block: "" });
  stub.clientFeedback = async (code, dcode, items) => { sent.push(items); return { confirmations: [11] }; };
  const c = await render(<Deliverable code="BANK-A" dcode="D4" />);
  expect(c.querySelector('[aria-current="step"]')?.textContent).toContain("客户确认");
  const box = c.querySelector('textarea[aria-label="第 1 条客户意见"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(box, "BANK-A 希望写明白班人数口径");
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const btn = [...c.querySelectorAll("button")].find((b) => b.textContent === "录入客户意见");
  await act(async () => { btn.click(); });
  expect(sent).toEqual([[{ section: null, text: "BANK-A 希望写明白班人数口径" }]]);
  expect(c.textContent).toContain("已录入 1 条客户意见");
});

test("D3 批量签：只能勾选门禁 0 问题的，签后逐份回报", async () => {
  const signed = [];
  stubDeliverable({ ...DELIV, code: "D3", title: "访谈纪要", status: "drafting" },
    { ...SO, deliverable: "D3", status: "drafting", separation_exempt: true, merged: true, merged_ready: true, sign_block: "" });
  stub.batchCandidates = async () => [
    { project: "BANK-A", deliverable: "D3", title: "访谈纪要", ready: true, reasons: [] },
    { project: "GRID-B", deliverable: "D3", title: "访谈纪要", ready: false, reasons: ["质量门禁还有问题，请回到逐条核对"] },
  ];
  stub.batchSign = async (items) => { signed.push(items); return [{ project: "BANK-A", deliverable: "D3", ok: true }]; };
  const c = await render(<Deliverable code="BANK-A" dcode="D3" />);
  expect(c.textContent).toContain("对内交付件，不要求写签分离");
  expect(c.textContent).toContain("核对并签署（一步完成）");
  const boxes = c.querySelectorAll('section[aria-label="D3 批量签"] input[type=checkbox]');
  expect(boxes[1].disabled).toBe(true);
  await act(async () => { boxes[0].click(); });
  const btn = [...c.querySelectorAll("button")].find((b) => b.textContent.startsWith("签署选中的"));
  await act(async () => { btn.click(); });
  expect(signed).toEqual([[{ project: "BANK-A", deliverable: "D3" }]]);
  expect(c.textContent).toContain("BANK-A 已签");
});

test("按表单成稿：每个输入框下有填写示例；已预填章节收起，表格逐格编辑", async () => {
  const { default: DeliverableForm } = await import("../components/DeliverableForm.jsx");
  const form = { code: "D1", title: "启动包", writable: true, sections: [
    { title: "项目概况", fixed: "", fields: [
      { key: "goal", label: "项目目标", type: "textarea", required: true, max: 2000, default: "", example: "帮助 BANK-A 建立安全运营体系" }] },
    { title: "风险与变更管理", fixed: "", fields: [
      { key: "risks", label: "主要风险与应对", type: "table", columns: ["风险", "应对"], required: false, max: 4000,
        default: "访谈难排期｜提前锁定", example: "资料滞后｜每周跟踪" }] },
  ] };
  const el = await render(<DeliverableForm code="BANK-A" dcode="D1" form={form} onDone={() => {}} onCancel={() => {}} />);
  const ex = [...el.querySelectorAll(".form-ex")].map((x) => x.textContent);
  expect(ex).toEqual(["示例：帮助 BANK-A 建立安全运营体系", "示例：资料滞后｜每周跟踪"]);
  expect(el.querySelector("details summary").textContent).toContain("已预填");
  expect(el.querySelector('table[aria-label="主要风险与应对"] input').value).toBe("访谈难排期");
});

test("按表单成稿：从合同导入后回填表单（含表格），等顾问核对再成稿", async () => {
  const { default: DeliverableForm } = await import("../components/DeliverableForm.jsx");
  const sent = [];
  stub.importContract = async (code, dcode, file) => { sent.push([code, dcode, file.name]); return { job_id: 1, status: "queued" }; };
  stub.contractImport = async () => ({ status: "done", error: "", values: { goal: "建立 BANK-A 安全运营体系", risks: "资料滞后｜每周跟踪", nope: "x" } });
  const form = { code: "D1", title: "启动包", writable: true, sections: [
    { title: "项目概况", fixed: "", fields: [{ key: "goal", label: "项目目标", type: "textarea", required: true, max: 2000, default: "" }] },
    { title: "风险与变更管理", fixed: "", fields: [
      { key: "risks", label: "主要风险与应对", type: "table", columns: ["风险", "应对"], required: false, max: 4000, default: "" }] },
  ] };
  const el = await render(<DeliverableForm code="BANK-A" dcode="D1" form={form} onDone={() => {}} onCancel={() => {}} />);
  const input = el.querySelector('input[aria-label="合同文件"]');
  const file = new File(["合同"], "合同.docx");
  Object.defineProperty(input, "files", { value: [file] });
  await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(sent).toEqual([["BANK-A", "D1", "合同.docx"]]);
  expect(el.querySelector("textarea").value).toBe("建立 BANK-A 安全运营体系");
  expect(el.querySelector('table[aria-label="主要风险与应对"] input').value).toBe("资料滞后");
  expect(el.textContent).toContain("已从合同回填 2 项");
});

test("项目成员：项目经理不用输入就能看到全部可加入的同事，勾选后一次添加", async () => {
  const added = [];
  stub.members = async () => [{ username: "u1", display_name: "经理甲", project_role: "manager" }];
  stub.lookupUsers = async (q) => [{ username: "u1", display_name: "经理甲" }, { username: "u2", display_name: "顾问乙" },
    { username: "u3", display_name: "顾问丙" }].filter((u) => !q || u.display_name.includes(q));
  stub.putMember = async (code, username, role) => { added.push([username, role]); return [{ username: "u1", display_name: "经理甲", project_role: "manager" }]; };
  const c = await render(<Members code="BANK-A" />);
  await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
  const pick = c.querySelector('[aria-label="可添加的同事"]');
  expect([...pick.querySelectorAll("label")].map((l) => l.textContent)).toEqual(["顾问乙u2", "顾问丙u3"]);
  const boxes = pick.querySelectorAll("input[type=checkbox]");
  await act(async () => { boxes[0].click(); boxes[1].click(); });
  const btn = [...c.querySelectorAll("button")].find((b) => b.textContent.startsWith("添加选中的 2 人"));
  await act(async () => btn.click());
  expect(added).toEqual([["u2", "member"], ["u3", "member"]]);
});

test("立项后直接进成员设置：带 new=1 时设置页顶部提示下一步加成员，并给回驾驶舱的入口", async () => {
  stub.project = async () => PROJECT;
  stub.members = async () => [{ username: "u1", display_name: "经理甲", project_role: "manager" }];
  stub.lookupUsers = async () => [];
  const c = await render(<Routes><Route path="/project/:code/settings" element={<SettingsPage code="BANK-A" />} /></Routes>,
    "/project/BANK-A/settings?new=1");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const tip = c.querySelector('[aria-label="立项后下一步"]');
  expect(tip?.textContent).toContain("立项完成");
  expect(tip.querySelector('a[href="/project/BANK-A"]')).toBeTruthy();
});

test("普通进入设置页不出立项提示", async () => {
  stub.project = async () => PROJECT;
  stub.members = async () => [];
  const c = await render(<Routes><Route path="/project/:code/settings" element={<SettingsPage code="BANK-A" />} /></Routes>,
    "/project/BANK-A/settings");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(c.querySelector('[aria-label="立项后下一步"]')).toBeNull();
});

const D1_FORM = { code: "D1", title: "启动包", writable: true, sections: [
  { title: "项目概况", fixed: "", fields: [{ key: "goal", label: "项目目标", type: "textarea", required: true, max: 2000, default: "" }] }] };

test("启动包两种成稿入口：大纲上方并排“从合同生成”“按表单成稿”，选了合同文件即交给表单去抽取", async () => {
  const { StartChoices } = await import("../components/DeliverableForm.jsx");
  const calls = [];
  const el = await render(<StartChoices form={D1_FORM} onForm={() => calls.push("form")} onContract={(f) => calls.push(f.name)} />);
  const box = el.querySelector('[aria-label="成稿方式"]');
  expect(box.textContent).toContain("按合同生成");
  const btn = [...box.querySelectorAll("button")].find((b) => b.textContent === "按表单成稿");
  await act(async () => btn.click());
  const input = box.querySelector('input[aria-label="合同文件"]');
  Object.defineProperty(input, "files", { value: [new File(["合同"], "合同.docx")] });
  await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(calls).toEqual(["form", "合同.docx"]);
});

test("启动包两种成稿入口：已有正文时不给按钮，说明原因（表单只用于首次成稿）", async () => {
  const { StartChoices } = await import("../components/DeliverableForm.jsx");
  const el = await render(<StartChoices form={{ ...D1_FORM, writable: false }} onForm={() => {}} onContract={() => {}} />);
  const box = el.querySelector('[aria-label="成稿方式"]');
  expect(box.querySelectorAll("button").length).toBe(0);
  expect(box.textContent).toContain("已有正文");
});

test("按表单成稿：带着合同文件打开时，一打开就开始抽取并回填", async () => {
  const { default: DeliverableForm } = await import("../components/DeliverableForm.jsx");
  const sent = [];
  stub.importContract = async (code, dcode, file) => { sent.push(file.name); return { job_id: 1, status: "queued" }; };
  stub.contractImport = async () => ({ status: "done", error: "", values: { goal: "建立 BANK-A 安全运营体系" } });
  const el = await render(<DeliverableForm code="BANK-A" dcode="D1" form={D1_FORM} initialFile={new File(["合同"], "合同.docx")}
    onDone={() => {}} onCancel={() => {}} />);
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(sent).toEqual(["合同.docx"]);
  expect(el.querySelector("textarea").value).toBe("建立 BANK-A 安全运营体系");
});

const LAYOUT = (path) => render(<Routes><Route path="/project/:code" element={<ProjectLayout />}>
  <Route index element={<p>驾驶舱</p>} /><Route path="settings" element={<p>设置</p>} /></Route></Routes>, path);

test("配人提示：项目经理进项目时项目里只有他一人，顶部提示去设置加成员（项目经理是别人立的项也一样）", async () => {
  stub.members = async () => [{ username: "u1", display_name: "经理甲", project_role: "manager" }];
  const c = await LAYOUT("/project/BANK-A");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  const tip = c.querySelector('[aria-label="待配项目成员"]');
  expect(tip?.textContent).toContain("还没有其他成员");
  expect(tip.querySelector('a[href="/project/BANK-A/settings?new=1"]')).toBeTruthy();
});

test("配人提示：已有其他成员、不是项目经理、或已在设置页时都不出", async () => {
  stub.members = async () => [{ username: "u1", project_role: "manager" }, { username: "u2", project_role: "member" }];
  let c = await LAYOUT("/project/BANK-A");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(c.querySelector('[aria-label="待配项目成员"]')).toBeNull();
  act(() => root.unmount()); el.remove();
  stub.members = async () => [{ username: "u9", project_role: "manager" }];
  c = await LAYOUT("/project/BANK-A");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(c.querySelector('[aria-label="待配项目成员"]')).toBeNull();
  act(() => root.unmount()); el.remove();
  stub.members = async () => [{ username: "u1", project_role: "manager" }];
  c = await LAYOUT("/project/BANK-A/settings");
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(c.querySelector('[aria-label="待配项目成员"]')).toBeNull();
});

test("写大纲那一行右边直接并排“按表单成稿”“按合同生成”，不用展开（负责人 11-28）", async () => {
  const calls = [];
  const d = { sections: [] };
  const el = await render(<Toc d={d} seq={0} onPick={() => {}} editable onOutline={async () => {}}
    form={D1_FORM} onForm={() => calls.push("form")} onContract={(f) => calls.push(f.name)} />);
  const row = el.querySelector('[aria-label="目录"] .sb');
  const labels = [...row.querySelectorAll("button, label")].map((b) => b.textContent.trim());
  expect(labels).toEqual(["写大纲", "按表单成稿", "按合同生成"]);
  await act(async () => [...row.querySelectorAll("button")].find((b) => b.textContent === "按表单成稿").click());
  const input = row.querySelector('input[aria-label="合同文件"]');
  Object.defineProperty(input, "files", { value: [new File(["合同"], "合同.docx")] });
  await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
  expect(calls).toEqual(["form", "合同.docx"]);
});
