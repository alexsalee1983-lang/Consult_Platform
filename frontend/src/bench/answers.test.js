import { test } from "node:test";
import assert from "node:assert/strict";
import { applyPreview, buildAnswer, previewTexts, problems } from "./answers.js";

const card = {
  questions: [
    { id: "keep", kind: "keep", text: "这条值得收进知识库吗？" },
    { id: "field:ref_document", kind: "field", text: "依据的文件写的是“示范制度”，对吗？" },
    { id: "issue:0", kind: "issue", text: "Agent 觉得有问题，你认可吗：缺适用规模" },
  ],
  template: { key: "practice", slots: [{ key: "how", label: "公司怎么做", required: true }, { key: "why", label: "为什么", required: false }] },
};

test("未答 keep 时提示，答完可提交", () => {
  assert.deepEqual(problems(card, {}, {}), ["先回答“这条值得收进知识库吗”"]);
  assert.deepEqual(problems(card, { keep: { choice: "yes" } }, {}), []);
});

test("答不对要写改正；认可问题要用填空卡；填空卡缺必填", () => {
  const p = problems(card, { keep: { choice: "yes" }, "field:ref_document": { choice: "no" }, "issue:0": { choice: "yes" } }, {});
  assert.equal(p.length, 2);
  assert.match(p[0], /依据的文件/);
  assert.deepEqual(problems(card, { keep: { choice: "yes" } }, { why: "因为" }), ["填空卡还要填：公司怎么做"]);
});

test("buildAnswer 只带已答的问题，空填空卡不提交", () => {
  const b = buildAnswer(card, { keep: { choice: "yes" }, "field:ref_document": { choice: "no", value: " 示范规范 " } }, { how: "" });
  assert.deepEqual(b.answers, [{ id: "keep", choice: "yes", value: "" }, { id: "field:ref_document", choice: "no", value: "示范规范" }]);
  assert.equal(b.fill, null);
  assert.deepEqual(buildAnswer(card, { keep: { choice: "yes" } }, { how: "按步骤" }).fill, { how: "按步骤" });
});

test("冲突处理卡：没答冲突题不能交", () => {
  const c = { ...card, questions: [...card.questions, { id: "conflict", kind: "conflict", text: "怎么处理？" }] };
  assert.match(problems(c, { keep: { choice: "yes" } }, {})[0], /冲突处理/);
  assert.deepEqual(problems(c, { keep: { choice: "yes" }, conflict: { choice: "both" } }, {}), []);
});

test("去标识预览：只送自己写的文字，换回后填空卡和理由都更新", () => {
  const state = { keep: { choice: "no", value: "示范科技有限公司 不用" } };
  const t = previewTexts(state, { how: "找李经理", why: " " });
  assert.deepEqual(t, { "fill:how": "找李经理", keep: "示范科技有限公司 不用" });
  const r = applyPreview(state, { how: "找李经理", why: " " }, { "fill:how": "找【人员1】", keep: "【客户1】 不用" });
  assert.equal(r.fill.how, "找【人员1】");
  assert.equal(r.state.keep.value, "【客户1】 不用");
  assert.equal(r.state.keep.choice, "no");
  assert.equal(buildAnswer(card, state, {}, "", true).deid_ack, true);
});
