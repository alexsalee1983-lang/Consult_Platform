// A15 编辑器内核测试（node --test，不需要浏览器）：往返无损、来源标签与后端一致、标签整体删除、插入保留来源。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { EditorSelection } from "@codemirror/state";
import { createState, docText, findRefs, insertWithSources } from "./core.js";

const samples = JSON.parse(readFileSync(new URL("./refSamples.json", import.meta.url), "utf8")).samples;

test("来源标签与后端正则一致（共用样例）", () => {
  for (const s of samples) {
    const refs = findRefs(s.text);
    const pick = (kind) => refs.filter((r) => r.kind === kind).map((r) => s.text.slice(r.from, r.to));
    assert.deepEqual(pick("ev"), s.ev, s.text);
    assert.deepEqual(pick("kb"), s.kb, s.text);
    assert.deepEqual(pick("inf"), s.inf, s.text);
    if (s.kb_refs) assert.deepEqual(refs.filter((r) => r.kind === "kb").map((r) => r.ref), s.kb_refs);
  }
});

test("载入、不编辑、取出逐字节相等", () => {
  const bodies = [
    ...samples.map((s) => s.text),
    "# 第一章\r\n\r\nBANK-A 现状 [I-01]\r\n",
    "混合换行\n第二行\r\n第三行\r末尾无换行",
    "行尾空格   \n\t制表符\n\n\n",
    "",
  ];
  for (const b of bodies) assert.equal(docText(createState(b)), b, JSON.stringify(b));
});

function del(state, from, to) {
  return state.update({ changes: { from, to }, userEvent: "delete.backward" }).state;
}

test("退格删到标签内：整个标签一起删", () => {
  const text = "现状 [D-12] 结论";
  const st = createState(text);
  const end = text.indexOf("]") + 1;
  assert.equal(docText(del(st, end - 1, end)), "现状  结论");
});

test("选区盖住半个标签：扩展到整个标签", () => {
  const text = "依据 [L4-SOC-ZJ-GB-0012] 的要求";
  const st = createState(text);
  assert.equal(docText(del(st, 0, 6)), " 的要求");
});

test("不碰标签的编辑不受影响", () => {
  const text = "现状 [D-12] 结论";
  const st = createState(text);
  assert.equal(docText(del(st, 0, 2)), " [D-12] 结论");
  const typed = st.update({ changes: { from: text.length, insert: "。" }, userEvent: "input.type" }).state;
  assert.equal(docText(typed), text + "。");
});

test("外部同步（非用户编辑）不扩展删除", () => {
  const st = createState("a [D-12] b");
  const next = st.update({ changes: { from: 0, to: st.doc.length, insert: "a [D-1" } }).state;
  assert.equal(docText(next), "a [D-1");
});

test("插入保留来源：标注原样进正文，光标在插入内容之后", () => {
  const st = createState("第一段。\n第二段。").update({ selection: EditorSelection.cursor(4) }).state;
  const snippet = "BANK-A 已部署边界防护 [I-01]，依据 [L4-SOC-ZJ-GB-0012]。";
  const next = st.update(insertWithSources(st, snippet)).state;
  assert.equal(docText(next), "第一段。" + snippet + "\n第二段。");
  assert.equal(next.selection.main.head, 4 + snippet.length);
  assert.equal(findRefs(docText(next)).length, 2);
});
