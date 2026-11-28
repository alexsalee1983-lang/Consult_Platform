// 四期 A41：驾驶舱抽屉的当前页上下文与本项目“待我处理”
import { test } from "node:test";
import assert from "node:assert/strict";
import { pageContext, pickConversation, projectTodos } from "./cockpitDrawer.js";

test("当前页：各页与交付件页", () => {
  assert.equal(pageContext("/project/BANK-A"), "全景");
  assert.equal(pageContext("/project/BANK-A/"), "全景");
  assert.equal(pageContext("/project/BANK-A/input"), "输入");
  assert.equal(pageContext("/project/BANK-A/requirements"), "需求");
  assert.equal(pageContext("/project/BANK-A/analysis"), "分析");
  assert.equal(pageContext("/project/BANK-A/output"), "输出");
  assert.equal(pageContext("/project/BANK-A/settings"), "设置");
  assert.equal(pageContext("/project/BANK-A/deliverables"), "输出");
  assert.equal(pageContext("/project/BANK-A/deliverables/D4"), "输出 / D4");
  assert.equal(pageContext("/project/BANK-A/deliverables/D4", "差距分析"), "输出 / D4 差距分析");
  assert.equal(pageContext("/project/BANK-A/deliverables/%E3%80%90x"), "输出");   // 不合规的段不带
  assert.equal(pageContext("/project/BANK-A/weird"), "全景");
  const long = pageContext("/project/BANK-A/deliverables/D4", "很".repeat(60));
  assert.ok(long.length <= 40 && long.startsWith("输出 / D4 "));
  assert.equal(pageContext("/project/BANK-A/deliverables/D4", "差距【分析】\n"), "输出 / D4 差距分析");
});

test("同一项目同一会话：取最近一次咨询师对话", () => {
  const convs = [{ id: 9, agent_code: "compliance-basis" }, { id: 7, agent_code: "consultant-lead" }, { id: 3, agent_code: "consultant-lead" }];
  assert.equal(pickConversation(convs).id, 7);
  assert.equal(pickConversation([{ id: 9, agent_code: "compliance-basis" }]), null);
  assert.equal(pickConversation([]), null);
});

test("待我处理只留本项目，带类别名，卡住的排后", () => {
  const d = {
    kinds: { confirmation: { name: "确认卡" }, deliverable_review: { name: "待签署" } },
    tasks: [
      { kind: "confirmation", ref_id: 1, title: "甲", project_code: "BANK-A", link: "/project/BANK-A", blocked: "等客户" },
      { kind: "deliverable_review", ref_id: 2, title: "乙", project_code: "BANK-A", link: "/project/BANK-A/deliverables/D4", blocked: "" },
      { kind: "confirmation", ref_id: 3, title: "丙", project_code: "GRID-B", link: "/project/GRID-B", blocked: "" },
    ],
  };
  const t = projectTodos(d, "BANK-A");
  assert.deepEqual(t.map((x) => x.title), ["乙", "甲"]);
  assert.equal(t[0].kindName, "待签署");
  assert.deepEqual(projectTodos(null, "BANK-A"), []);
});
