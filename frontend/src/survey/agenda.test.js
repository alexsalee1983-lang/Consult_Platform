import { test } from "node:test";
import assert from "node:assert/strict";
import { clashes, itemsByQid, phases, roleAgenda } from "./agenda.js";

const q = (qid) => ({ qid, control: "C", kind: "closed", text: qid, options: [] });
const PACK = { questions: [q("Q1"), q("Q2"), q("Q3"), q("Q4"), q("Q5")] };
const AGENDA = {
  roles: [
    { role: "安全负责人", quick_confirm: [{ qid: "Q2", answers: [{ role: "安全负责人", source: "self", value: "B" }] }],
      agenda: [{ qid: "Q3", reason: "conflict", answers: [{ role: "客户资料", source: "document", value: "A" }] }, { qid: "Q2" }],
      followup: [{ qid: "Q5", reason: "observe" }] },
  ],
};

test("按角色把包里的题分成 快速确认 → 议程 → 补问 → 其他", () => {
  const p = phases(PACK, AGENDA, " 安全负责人 ");
  assert.deepEqual(p.quick_confirm.map((x) => x.qid), ["Q2"]);
  assert.deepEqual(p.agenda.map((x) => x.qid), ["Q3"]);
  assert.deepEqual(p.followup.map((x) => x.qid), ["Q5"]);
  assert.deepEqual(p.rest.map((x) => x.qid), ["Q1", "Q4"]);
});

test("没有议程的角色全部在其他题", () => {
  const p = phases(PACK, AGENDA, "运维");
  assert.equal(roleAgenda(AGENDA, "运维"), null);
  assert.equal(p.rest.length, 5);
  assert.equal(p.quick_confirm.length, 0);
});

test("实时矛盾提示：与已有答案不一致才提示，不清楚不提示", () => {
  const items = itemsByQid(AGENDA, "安全负责人");
  assert.equal(items.Q2.answers[0].value, "B");
  assert.deepEqual(clashes(items.Q3, "B"), ["客户资料（客户资料）"]);
  assert.deepEqual(clashes(items.Q3, "A"), []);
  assert.deepEqual(clashes(items.Q3, "X"), []);
});
