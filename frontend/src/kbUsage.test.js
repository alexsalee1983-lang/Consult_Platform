// 四期 A17：本段用了哪些知识的说明行（版本、权威等级、依据、待复查）
import { test } from "node:test";
import assert from "node:assert/strict";
import { knowledgeLine, itemsById } from "./kbUsage.js";

test("说明行带版本、权威等级、依据和待复查原因", () => {
  const line = knowledgeLine({
    doc_id: "KB-000001", visible: true, title: "示范", version: 2, authority_label: "标准",
    ref_code: "GB/T 22239-2019", ref_version: "2019", recheck_state: "pending", flagged: true,
  });
  assert.equal(line, "v2 · 标准 · 依据 GB/T 22239-2019 2019 · 待复查（顾问反馈）");
});

test("内部条目不分级、没有依据时只出版本；依据文件名在没有文号时补上", () => {
  assert.equal(knowledgeLine({ visible: true, version: 1, authority_label: "" }), "v1 · 内部（不分级）");
  assert.equal(knowledgeLine({ visible: true, version: 1, authority_label: "参考资料", ref_document: "某白皮书" }),
    "v1 · 参考资料 · 依据 某白皮书");
  assert.equal(knowledgeLine({ visible: true, version: 3, authority_label: "", recheck_state: "pending", flagged: false }),
    "v3 · 内部（不分级） · 待复查");
});

test("看不到的条目只说明无权查看；按编号索引", () => {
  assert.equal(knowledgeLine({ doc_id: "KB-9", visible: false }), "无权查看这条知识的详情");
  assert.equal(knowledgeLine(undefined), "");
  assert.deepEqual(Object.keys(itemsById([{ doc_id: "A" }, { doc_id: "B" }])), ["A", "B"]);
});
