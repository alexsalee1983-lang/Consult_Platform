import { test } from "node:test";
import assert from "node:assert/strict";
import { EditorSelection } from "@codemirror/state";
import { createState, docText } from "./core.js";
import {
  applyCandidate, buildCandidates, filterCandidates, findTrigger, mentionComplete, mentionField, mentionKeys, refreshMention,
} from "./mention.js";

const EVS = [
  { code: "D-12", title: "BANK-A 防火墙配置截图", status: "parsed" },
  { code: "I-01", title: "GRID-B 运维负责人访谈", status: "ready" },
  { code: "D-13", title: "已删资料", status: "deleted" },
];
const KB = [
  { doc_id: "L4-SOC-ZJ-GB-0012", title: "日志留存要求" },
  { doc_id: "随便的编号", title: "不合编号规则的不收" },
];
const SECS = [{ seq: 1, title: "现状概述", current_version: 2 }, { seq: 2, title: "差距分析", current_version: 0 }];

test("@ 触发：取 @ 后到光标的查询；空格或换行打断", () => {
  assert.deepEqual(findTrigger("依据 @防火", 6), { kind: "@", from: 3, query: "防火" });
  assert.deepEqual(findTrigger("@", 1), { kind: "@", from: 0, query: "" });
  assert.equal(findTrigger("@防火 墙", 5), null);
  assert.equal(findTrigger("@防\n火", 4), null);
  assert.equal(findTrigger("邮件 a@b", 6), null, "@ 前是字母数字（邮箱）不触发");
});

test("[ 触发：只认编号字符，已闭合或中文不触发", () => {
  assert.deepEqual(findTrigger("见 [L4-S", 7), { kind: "[", from: 2, query: "L4-S" });
  assert.deepEqual(findTrigger("[", 1), { kind: "[", from: 0, query: "" });
  assert.equal(findTrigger("[D-12] ", 7), null);
  assert.equal(findTrigger("[推断", 3), null);
});

test("候选：资料排除已删除，知识只收合编号规则的，章节只收有正文的；插入文字带方括号", () => {
  const c = buildCandidates({ evidences: EVS, kb: KB, sections: SECS, currentSeq: 9 });
  assert.deepEqual(c.map((x) => x.insert), ["[D-12]", "[I-01]", "[L4-SOC-ZJ-GB-0012]", "见第 1 章“现状概述”"]);
  assert.equal(buildCandidates({ sections: SECS, currentSeq: 1 }).length, 0, "不引用本章自己");
});

test("过滤：@ 按编号或标题包含（不分大小写），[ 只按编号且不出章节", () => {
  const c = buildCandidates({ evidences: EVS, kb: KB, sections: SECS });
  assert.deepEqual(filterCandidates(c, "@", "防火").map((x) => x.code), ["D-12"]);
  assert.deepEqual(filterCandidates(c, "@", "d-1").map((x) => x.code), ["D-12"]);
  assert.deepEqual(filterCandidates(c, "[", "L4").map((x) => x.code), ["L4-SOC-ZJ-GB-0012"]);
  assert.equal(filterCandidates(c, "[", "").some((x) => x.kind === "sec"), false);
  assert.equal(filterCandidates(c, "[", "防火").length, 0);
  assert.equal(filterCandidates(Array.from({ length: 20 }, (_, i) => ({ kind: "ev", code: `D-${i + 10}`, label: "" })), "@", "").length, 8);
});

test("插入：把触发符到光标的文字换成引用，光标停在后面", () => {
  const s1 = createState("依据 @防火 。").update({ selection: EditorSelection.cursor(6) }).state;
  const s2 = s1.update(applyCandidate(s1, { kind: "@", from: 3, query: "防火" }, { insert: "[D-12]" })).state;
  assert.equal(docText(s2), "依据 [D-12] 。");
  assert.equal(s2.selection.main.head, 9);
});

function editorWith(text, cands) {
  let st = createState(text, [mentionComplete(() => cands)]);
  st = st.update({ selection: EditorSelection.cursor(text.length) }).state;
  return st;
}

test("扩展：输入 @ 弹出候选，方向键选、回车插入", () => {
  const cands = buildCandidates({ evidences: EVS, kb: KB });
  let st = editorWith("", cands);
  st = st.update({ changes: { from: 0, insert: "@" }, selection: EditorSelection.cursor(1), userEvent: "input.type" }).state;
  const m = st.field(mentionField);
  assert.equal(m.items.length, 3);
  assert.equal(m.active, 0);
  const view = fakeView(st);
  assert.equal(mentionKeys.down(view), true);
  assert.equal(view.state.field(mentionField).active, 1);
  assert.equal(mentionKeys.enter(view), true);
  assert.equal(docText(view.state), "[I-01]");
  assert.equal(view.state.field(mentionField).items.length, 0, "插入后关闭");
});

test("扩展：Esc 关闭，继续输入同一触发也不再弹，换新触发再弹", () => {
  const cands = buildCandidates({ evidences: EVS });
  let st = editorWith("", cands);
  st = st.update({ changes: { from: 0, insert: "[D" }, selection: EditorSelection.cursor(2), userEvent: "input.type" }).state;
  assert.equal(st.field(mentionField).items.length, 1);
  const view = fakeView(st);
  assert.equal(mentionKeys.escape(view), true);
  assert.equal(view.state.field(mentionField).items.length, 0);
  view.dispatch({ changes: { from: 2, insert: "-" }, selection: EditorSelection.cursor(3), userEvent: "input.type" });
  assert.equal(view.state.field(mentionField).items.length, 0, "同一触发 Esc 后不再弹");
  view.dispatch({ changes: { from: 3, insert: " @" }, selection: EditorSelection.cursor(5), userEvent: "input.type" });
  assert.equal(view.state.field(mentionField).items.length, 2);
  assert.equal(mentionKeys.escape(fakeView(view.state)), true);
});

test("扩展：没有弹出时按键不拦截（交给默认回车、方向键）", () => {
  const view = fakeView(editorWith("普通文字", buildCandidates({ evidences: EVS })));
  assert.equal(mentionKeys.enter(view), false);
  assert.equal(mentionKeys.down(view), false);
  assert.equal(mentionKeys.escape(view), false);
});

test("扩展：查询变化随输入更新（父组件按它异步检索知识，回来后 refreshMention 刷新候选）", () => {
  let cands = [];
  let st = createState("", [mentionComplete(() => cands)]);
  st = st.update({ changes: { from: 0, insert: "@日志" }, selection: EditorSelection.cursor(3), userEvent: "input.type" }).state;
  assert.deepEqual(st.field(mentionField).trig, { kind: "@", from: 0, query: "日志" });
  assert.equal(st.field(mentionField).items.length, 0);
  cands = buildCandidates({ kb: KB });
  st = st.update({ effects: refreshMention.of(null) }).state;
  assert.deepEqual(st.field(mentionField).items.map((x) => x.code), ["L4-SOC-ZJ-GB-0012"]);
});

function fakeView(state) {
  const v = { state, dispatch(spec) { v.state = v.state.update(spec).state; } };
  return v;
}
