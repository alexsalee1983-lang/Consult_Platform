// 四期 A41：驾驶舱右侧抽屉（接口打桩，只用虚拟客户）
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, test, vi } from "vitest";

const stub = {};
const sent = [];
vi.mock("../api.js", async (orig) => ({
  ...(await orig()),
  api: new Proxy({}, { get: (_, k) => stub[k] || (() => new Promise(() => {})) }),
  sendMessage: async (id, message, onDelta, onTool, page) => { sent.push({ id, message, page }); onDelta("草稿回复"); return { message_id: 99 }; },
}));
const { default: AdvisorDrawer } = await import("./AdvisorDrawer.jsx");

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let root, el;
async function render(path) {
  el = document.body.appendChild(document.createElement("div"));
  await act(async () => { root = createRoot(el); root.render(<MemoryRouter initialEntries={[path]}><AdvisorDrawer code="BANK-A" /></MemoryRouter>); });
  return el;
}
afterEach(() => { act(() => root.unmount()); el.remove(); for (const k in stub) delete stub[k]; sent.length = 0; });
const click = (node) => act(async () => { node.click(); });
const byText = (t, sel = "button") => [...el.querySelectorAll(sel)].find((b) => b.textContent.includes(t));

const TASKS = {
  kinds: { confirmation: { name: "确认卡" }, deliverable_review: { name: "待签署" } },
  tasks: [
    { kind: "deliverable_review", ref_id: 2, title: "D4 差距分析", project_code: "BANK-A", link: "/project/BANK-A/deliverables/D4", blocked: "" },
    { kind: "confirmation", ref_id: 3, title: "别的项目", project_code: "GRID-B", link: "/project/GRID-B", blocked: "" },
  ],
};

test("沿用本项目最近的咨询师对话，发消息带当前页", async () => {
  stub.myTasks = async () => TASKS;
  stub.conversations = async () => [{ id: 7, agent_code: "consultant-lead" }];
  stub.conversation = async () => ({ id: 7, agent_code: "consultant-lead", messages: [{ id: 1, role: "user", content: "上次的问题", status: "ok" }] });
  await render("/project/BANK-A/deliverables/D4");
  expect(byText("待我处理 1")).toBeTruthy();                         // 按钮上带本项目待办数，别的项目不算
  await click(byText("顾问对话"));
  expect(el.textContent).toContain("当前页：输出 / D4");
  expect(el.textContent).toContain("上次的问题");
  const ta = el.querySelector("textarea");
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, "这一章还缺什么");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => { el.querySelector("form.composer").requestSubmit(); });
  expect(sent).toEqual([{ id: 7, message: "这一章还缺什么", page: "输出 / D4" }]);
  expect(el.textContent).toContain("草稿回复");
});

test("没有对话时发第一条才新建；待我处理只列本项目", async () => {
  stub.myTasks = async () => TASKS;
  stub.conversations = async () => [];
  const created = [];
  stub.createConversation = async (code, agent) => { created.push([code, agent]); return { id: 11, agent_code: agent, messages: [] }; };
  await render("/project/BANK-A/analysis");
  await click(byText("顾问对话"));
  expect(created).toEqual([]);
  await click(byText("待我处理（1）"));
  const list = el.querySelector('[aria-label="待我处理"]');
  expect(list.textContent).toContain("D4 差距分析");
  expect(list.textContent).not.toContain("别的项目");
  expect(list.querySelector("a").getAttribute("href")).toBe("/project/BANK-A/deliverables/D4");
  await click(byText("对话", "nav button"));
  const ta = el.querySelector("textarea");
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, "分析页怎么看");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => { el.querySelector("form.composer").requestSubmit(); });
  expect(created).toEqual([["BANK-A", "consultant-lead"]]);
  expect(sent[0]).toEqual({ id: 11, message: "分析页怎么看", page: "分析" });
});
