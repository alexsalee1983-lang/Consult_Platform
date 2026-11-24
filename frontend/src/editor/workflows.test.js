import { test } from "node:test";
import assert from "node:assert/strict";
import { WORKFLOWS, findingsLine, workflowTarget } from "./assist.js";

test("三个一键工作流：补来源、口径检查、管理层摘要", () => {
  assert.deepEqual(WORKFLOWS.map(([k]) => k), ["cite", "check", "summary"]);
});

test("补来源、口径检查：有选区改选区，没有选区改整章；摘要不锁区间（插入新段落）", () => {
  const body = "甲乙丙丁";
  assert.deepEqual(workflowTarget("cite", { from: 1, to: 3, text: "乙丙" }, body), { from: 1, to: 3, text: "乙丙" });
  assert.deepEqual(workflowTarget("check", { from: 2, to: 2, text: "" }, body), { from: 0, to: 4, text: body });
  assert.deepEqual(workflowTarget("check", null, body), { from: 0, to: 4, text: body });
  assert.equal(workflowTarget("summary", { from: 1, to: 3, text: "乙丙" }, body), null);
  assert.equal(workflowTarget("cite", null, ""), null);   // 空章节没东西可改
});

test("程序核对结果一行说明", () => {
  assert.equal(findingsLine(null), "");
  assert.equal(findingsLine({ unsourced: 2, qualification: ["通过等保"] }),
    "程序核对：无来源论断 2 句；资质边界用语：通过等保（接受前请确认已改掉）");
  assert.equal(findingsLine({ unsourced: 0, qualification: [] }), "程序核对：无来源论断 0 句；未发现资质边界用语");
});
