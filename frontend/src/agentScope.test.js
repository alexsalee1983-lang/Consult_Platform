// 四期 A29：“查看 Agent 运行情况”按键按角色可见（《平台规划刷新设计》2.2）
import { test } from "node:test";
import assert from "node:assert/strict";
import { agentScopes, showAgentButton } from "./agentScope.js";

test("知识管理员：知识侧可看可重跑", () => {
  const s = agentScopes(["knowledge_admin"], false);
  assert.equal(s.knowledge, true);
  assert.equal(s.manageKnowledge, true);
  assert.equal(s.portfolio, false);
  assert.equal(showAgentButton(s), true);
});

test("咨询业务负责人：知识侧只读、跨项目只看状态", () => {
  const s = agentScopes(["project_lead"], false);
  assert.deepEqual([s.knowledge, s.manageKnowledge, s.portfolio], [true, false, true]);
  assert.equal(showAgentButton(s), true);
});

test("知识专家、顾问在项目外没有这个按键；在项目页里（成员）有，只读由后端判", () => {
  assert.equal(showAgentButton(agentScopes(["knowledge_expert"], false)), false);
  assert.equal(showAgentButton(agentScopes(["consultant"], false)), false);
  assert.equal(showAgentButton(agentScopes(["consultant"], true)), true);
  assert.equal(agentScopes(["consultant"], true).knowledge, false);
});
