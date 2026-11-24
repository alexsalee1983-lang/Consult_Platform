import { test } from "node:test";
import assert from "node:assert/strict";
import { applyReplace, diffStats, lostRefs, tokenize, wordDiff } from "./selectEdit.js";
import { createState, docText, replaceRangeIfUnchanged, selectionAsk } from "./core.js";

const join = (segs, ops) => segs.filter((s) => ops.includes(s.op)).map((s) => s.t).join("");

test("分词：汉字逐字、英文数字成词、来源标注整体一个词", () => {
  assert.deepEqual(tokenize("夜间值守 SOC2 [L2-SOC-OPS-NIGHT-001]。"),
    ["夜", "间", "值", "守", " ", "SOC2", " ", "[L2-SOC-OPS-NIGHT-001]", "。"]);
  assert.deepEqual(tokenize(""), []);
});

test("词级对比：两侧都能还原，相邻同类合并", () => {
  const a = "本系统夜间值守依赖人工 [I-01]。";
  const b = "本系统夜间值守依赖轮班与告警分级 [I-01]。";
  const segs = wordDiff(a, b);
  assert.equal(join(segs, ["=", "-"]), a);
  assert.equal(join(segs, ["=", "+"]), b);
  for (let i = 1; i < segs.length; i++) assert.notEqual(segs[i].op, segs[i - 1].op);
  assert.ok(segs.some((s) => s.op === "-" && s.t.includes("人工")));
  assert.ok(segs.some((s) => s.op === "=" && s.t.includes("[I-01]")));
  assert.deepEqual(diffStats(segs), { removed: 2, added: 7 });
});

test("词级对比：相同、全删、全增与超长兜底", () => {
  assert.deepEqual(wordDiff("甲乙", "甲乙"), [{ op: "=", t: "甲乙" }]);
  assert.deepEqual(wordDiff("甲乙", ""), [{ op: "-", t: "甲乙" }]);
  assert.deepEqual(wordDiff("", "丙"), [{ op: "+", t: "丙" }]);
  const a = "字".repeat(1500), b = "词".repeat(1500);   // 超过上限不做逐词 DP，整段替换，仍可还原
  assert.deepEqual(wordDiff(a, b), [{ op: "-", t: a }, { op: "+", t: b }]);
  const long = "字".repeat(3000);
  assert.deepEqual(wordDiff(long, long + "尾"), [{ op: "=", t: long }, { op: "+", t: "尾" }]);   // 同头同尾先剥掉
});

test("改写去掉的来源标注要列出来（证据、知识、推断）", () => {
  const a = "差距一 [I-01]；做法 [L2-SOC-OPS-NIGHT-001] [推断]。";
  assert.deepEqual(lostRefs(a, "差距一 [I-01]。"), ["L2-SOC-OPS-NIGHT-001", "推断"]);
  assert.deepEqual(lostRefs(a, a), []);
});

test("文本框替换：原文没动才替换，动过就拒绝", () => {
  const range = { from: 1, to: 3, text: "乙丙" };
  assert.deepEqual(applyReplace("甲乙丙丁", range, "戊"), { ok: true, text: "甲戊丁", cursor: 2 });
  assert.equal(applyReplace("甲乙X丁", range, "戊").ok, false);
  assert.equal(applyReplace("甲", range, "戊").ok, false);
});

test("新编辑器替换：校验原文、可回退（替换后再换回去还原）", () => {
  const st = createState("甲乙丙丁");
  const spec = replaceRangeIfUnchanged(st, 1, 3, "乙丙", "戊己庚");
  const next = st.update(spec).state;
  assert.equal(docText(next), "甲戊己庚丁");
  assert.equal(replaceRangeIfUnchanged(next, 1, 3, "乙丙", "X"), null);   // 原文已变
  const back = next.update(replaceRangeIfUnchanged(next, 1, 4, "戊己庚", "乙丙")).state;
  assert.equal(docText(back), "甲乙丙丁");
});

test("选区浮层：有选区才出现，空光标不出现", () => {
  const field = selectionAsk(() => {});
  const st = createState("甲乙丙丁", [field]);
  assert.equal(st.field(field), null);
  const sel = st.update({ selection: { anchor: 1, head: 3 } }).state;
  assert.equal(sel.field(field).pos, 3);
  assert.equal(sel.update({ selection: { anchor: 2 } }).state.field(field), null);
});
