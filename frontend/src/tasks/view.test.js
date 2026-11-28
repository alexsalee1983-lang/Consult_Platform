import { test } from "node:test";
import assert from "node:assert/strict";
import { actionableCount, filterChips, splitTasks, toggleFilter } from "./view.js";

const tasks = [
  { kind: "confirmation", ref_id: 1, blocked: "", folded: false },
  { kind: "kb_content", ref_id: 2, blocked: "", folded: true },
  { kind: "deid", ref_id: 3, blocked: "你是上传者，需另一位知识管理员确认", folded: false },
  { kind: "deletion", ref_id: 4, blocked: "", folded: false },
];
const kinds = {
  confirmation: { name: "确认卡", available: true, count: 1 }, deliverable_review: { name: "待签署", available: true, count: 0 },
  kb_content: { name: "知识内容审核", available: true, count: 1 }, kb_compliance: { name: "知识合规审核", available: true, count: 0 },
  recheck: { name: "待复审", available: true, count: 0 }, deid: { name: "去标识确认", available: true, count: 1 },
  deletion: { name: "资料删除确认", available: true, count: 1 }, sample: { name: "抽检", available: true, count: 2 },
  practice: { name: "公司做法确认", available: false, count: 0 }, source_confirm: { name: "来源确认", available: false, count: 0 },
};

test("角标只数本人能处理的", () => {
  assert.equal(actionableCount(tasks), 3);
  assert.equal(actionableCount(undefined), 0);
});

test("筛选条：七类固定在前，其余有数才列，未接入的不列", () => {
  const keys = filterChips(kinds).map((c) => c.key);
  assert.deepEqual(keys, ["confirmation", "deliverable_review", "kb_content", "kb_compliance", "recheck", "deid", "deletion", "sample"]);
  assert.equal(filterChips(kinds)[5].count, 1);
});

test("点选只看该类，再点取消", () => {
  assert.equal(toggleFilter("", "deid"), "deid");
  assert.equal(toggleFilter("deid", "deid"), "");
  assert.equal(toggleFilter("deid", "kb_content"), "kb_content");
  const s = splitTasks(tasks, "deid");
  assert.equal(s.today.length + s.later.length, 0);
});

test("A29：分两段（今天、超出额度）；别人的卡隐藏，不灰显", () => {
  const s = splitTasks(tasks, "");
  assert.deepEqual(s.today.map((t) => t.ref_id), [1, 4]);
  assert.deepEqual(s.later.map((t) => t.ref_id), [2]);
  assert.equal(s.blocked, undefined);
});

