// 页面冒烟级组件测试样板（四期 质量）：接口一律打桩、只用虚拟客户；后续 PR 照此给改动的页面加测试。
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
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

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
vi.stubGlobal("localStorage", globalThis.jsdom.window.localStorage);   // Node 25+ 自带的 localStorage 会盖住 jsdom 的
let root, el;
async function render(node) {
  el = document.body.appendChild(document.createElement("div"));
  await act(async () => { root = createRoot(el); root.render(<MemoryRouter>{node}</MemoryRouter>); });
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

test("项目驾驶舱：显示项目、交付件和待确认", async () => {
  stub.project = async () => PROJECT;
  stub.health = async () => ({ llm_external: true });
  stub.evidences = async () => [];
  const c = await render(<Overview code="BANK-A" />);
  expect(c.querySelector("h1").textContent).toBe("项目驾驶舱 · 安全运营中心建设");
  expect(c.textContent).toContain("现状差距分析");
  expect(c.textContent).toContain("当前为外部测试模型");
});
