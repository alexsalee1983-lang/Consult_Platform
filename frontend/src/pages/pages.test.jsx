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
const { default: Projects } = await import("./Projects.jsx");
const { default: Overview } = await import("./Overview.jsx");
const { OutputPage, ProjectLayout } = await import("./ProjectPages.jsx");
const { default: Drawer } = await import("../components/Drawer.jsx");
const { todos } = await import("../components/Panorama.jsx");

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

test("全景：行动条、默认只看例外、已自动完成折叠成一行", async () => {
  stubOverview();
  const c = await render(<Overview code="BANK-A" />);
  expect(c.querySelector("h1").textContent).toBe("项目驾驶舱 · 安全运营中心建设");
  expect(c.textContent).toContain("当前为外部测试模型");
  expect(c.textContent).toContain("最先：组织与人员矛盾");
  const pressed = [...c.querySelectorAll("button[aria-pressed=true]")].map((b) => b.textContent);
  expect(pressed).toEqual(["只看例外"]);
  const fold = [...c.querySelectorAll("button")].find((b) => b.textContent.includes("本周自动完成 2 项"));
  expect(fold).toBeTruthy();
  await act(async () => fold.click());
  expect([...c.querySelectorAll("button[aria-pressed=true]")].map((b) => b.textContent)).toEqual(["全部"]);
});

test("全景抽屉：待你处理按影响排，点确认卡就地打开，Esc 关闭", async () => {
  stubOverview();
  const c = await render(<Overview code="BANK-A" />);
  await act(async () => c.querySelector('section[aria-label="行动条"] button').click());
  const dlg = c.querySelector('dialog[aria-label="待你处理 · 按影响排序"]');
  expect([...dlg.querySelectorAll("li")].map((li) => li.textContent.replace(/影响.*/, ""))).toEqual(
    ["确认卡组织与人员矛盾", "确认卡证据缺口 · 机房", "签署签署 D1"]);
  await act(async () => dlg.querySelector("li button").click());
  expect(c.querySelector('dialog[aria-label="确认卡"]')?.textContent).toContain("组织与人员矛盾");
  expect(c.querySelector('dialog a[href="/project/BANK-A/analysis"]')).toBeTruthy();
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
  expect(c.querySelector("dialog")).toBeNull();
});

test("⌖ 全景定位：带 focus 时切到全部视图并高亮对应节点", async () => {
  stubOverview();
  const c = await render(<Overview code="BANK-A" focus="deliv:D4" />);
  expect([...c.querySelectorAll("button[aria-pressed=true]")].map((b) => b.textContent)).toEqual(["全部"]);
  expect(c.querySelector("g.flash")?.getAttribute("data-node")).toBe("deliv:D4");
});

test("项目导航：五个导航页，交付件页算在“输出”下", async () => {
  const c = await render(
    <Routes><Route path="/project/:code" element={<ProjectLayout />}><Route path="deliverables/:dcode" element={<p>交付件页</p>} /></Route></Routes>,
    "/project/BANK-A/deliverables/D2");
  const tabs = [...c.querySelectorAll('nav[aria-label="项目导航"] a')];
  expect(tabs.map((a) => a.textContent)).toEqual(["全景", "输入", "分析", "输出", "设置", "调研助手 ⤢"]);
  expect(tabs.find((a) => a.className === "on")?.textContent).toBe("输出");
  expect(c.textContent).toContain("BANK-A / 输出 / D2");
  expect(c.textContent).toContain("交付件页");
});

test("输出页：列出交付件，每条有“⌖ 全景”回到对应节点", async () => {
  stub.project = async () => PROJECT;
  const c = await render(<OutputPage code="BANK-A" />);
  expect(c.querySelector('a[href="/project/BANK-A/deliverables/D1"]')?.textContent).toContain("现状差距分析");
  expect(c.querySelector('a[href="/project/BANK-A?focus=deliv%3AD1"]')?.textContent).toBe("⌖ 全景");
});

test("抽屉：✕ 关闭，“查看全部”跳子页面", async () => {
  const onClose = vi.fn();
  const c = await render(<Drawer title="证据缺口" onClose={onClose} more={{ to: "/project/BANK-A/input", label: "查看全部" }}><p>BANK-A 机房</p></Drawer>);
  expect(c.querySelector("dialog[open]")?.getAttribute("aria-label")).toBe("证据缺口");
  expect(c.querySelector('a[href="/project/BANK-A/input"]').textContent).toBe("查看全部 →");
  await act(async () => c.querySelector('button[aria-label="关闭"]').click());
  expect(onClose).toHaveBeenCalledTimes(1);
});
