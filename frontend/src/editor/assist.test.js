import { test } from "node:test";
import assert from "node:assert/strict";
import { ASSIST_NOTE, noteAfterInsert, sourcesLine, spliceText } from "./assist.js";
import { createState, docText, findRefs, insertWithSources } from "./core.js";

const REPLY = "经评估，本系统已通过等保三级测评 [D-99]；夜间值守参照 [L2-SOC-OPS-NIGHT-001] [推断]。";

test("文本框插入：替换选区，标注原样保留，光标在插入内容之后", () => {
  const r = spliceText("甲乙丙丁", 1, 3, REPLY);
  assert.equal(r.text, "甲" + REPLY + "丁");
  assert.equal(r.cursor, 1 + REPLY.length);
  assert.deepEqual(findRefs(r.text).map((x) => x.ref), findRefs(REPLY).map((x) => x.ref));
  assert.equal(spliceText("ab", undefined, undefined, "X").text, "abX");   // 没有光标：追加到末尾
});

test("新编辑器插入替换选区：与文本框结果一致", () => {
  const st = createState("甲乙丙丁");
  const sel = st.update({ selection: { anchor: 1, head: 3 } }).state;
  const next = sel.update(insertWithSources(sel, REPLY)).state;
  assert.equal(docText(next), spliceText("甲乙丙丁", 1, 3, REPLY).text);
});

test("修改说明只追加一次助手标记", () => {
  assert.equal(noteAfterInsert(""), ASSIST_NOTE);
  assert.equal(noteAfterInsert("补夜班"), `补夜班；${ASSIST_NOTE}`);
  assert.equal(noteAfterInsert(`补夜班；${ASSIST_NOTE}`), `补夜班；${ASSIST_NOTE}`);
});

test("来源说明：核对不上的列出并提示签署会被拦", () => {
  const line = sourcesLine({ counts: { evidence: 1, knowledge: 0, inferred: 2 }, unresolved: ["D-99"] });
  assert.match(line, /证据 1 · 知识 0 · 推断 2/);
  assert.match(line, /D-99/);
  assert.match(line, /签署前评审阻断/);
  assert.equal(sourcesLine(null), "");
});
