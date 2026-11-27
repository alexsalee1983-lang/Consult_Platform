import { test } from "node:test";
import assert from "node:assert/strict";
import { canPersist, checkResponses, clearDraft, draftKey, listDrafts, loadDraft, progress, resultsBody, saveDraft } from "./draft.js";

const PACK = {
  version: 1, project: "BANK-A-01", code: "P-01", tier: "standard", pack_hash: "h1",
  questions: [
    { qid: "SOC-Q01", control: "C1", kind: "closed", text: "是否有值班表", options: [{ key: "A", label: "有" }, { key: "B", label: "没有" }, { key: "X", label: "不清楚" }] },
    { qid: "SOC-Q02", control: "C2", kind: "open", text: "说明告警分级", options: [] },
  ],
};

function memStore() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k),
    key: (i) => [...m.keys()][i] ?? null, get length() { return m.size; }, _m: m };
}

test("严格档不落盘，普通档可落盘", () => {
  assert.equal(canPersist("standard"), true);
  assert.equal(canPersist("strict"), false);
  assert.equal(canPersist(undefined), false);
});

test("草稿按项目和包哈希存取，回传后清除", () => {
  const s = memStore();
  const rs = [{ respondent_role: "SOC 主管", answers: { "SOC-Q01": { value: "A", text: "" } } }];
  assert.equal(saveDraft(s, PACK, rs, "2027-01-05T10:00:00Z"), true);
  assert.equal(draftKey("BANK-A-01", "h1"), "cp-survey:BANK-A-01:h1");
  const d = loadDraft(s, "BANK-A-01", "h1");
  assert.equal(d.pack.code, "P-01");
  assert.deepEqual(d.responses, rs);
  assert.deepEqual(listDrafts(s, "BANK-A-01").map((x) => x.code), ["P-01"]);
  assert.deepEqual(listDrafts(s, "GRID-B-01"), []);
  clearDraft(s, "BANK-A-01", "h1");
  assert.equal(loadDraft(s, "BANK-A-01", "h1"), null);
});

test("严格档包一律不写本机存储", () => {
  const s = memStore();
  assert.equal(saveDraft(s, { ...PACK, tier: "strict" }, [], "t"), false);
  assert.equal(s.length, 0);
});

test("损坏的草稿当作没有", () => {
  const s = memStore();
  s.setItem(draftKey("BANK-A-01", "h1"), "{坏");
  assert.equal(loadDraft(s, "BANK-A-01", "h1"), null);
  assert.deepEqual(listDrafts(s, "BANK-A-01"), []);
});

test("回传请求体只带已答题，开放题去首尾空白", () => {
  const rs = [{ respondent_role: " SOC 主管 ", answers: { "SOC-Q01": { value: "X", text: "" }, "SOC-Q02": { value: "", text: "  三级  " } } },
    { respondent_role: "值班员", answers: { "SOC-Q01": { value: "", text: "" } } }];
  assert.deepEqual(resultsBody(PACK, rs), { pack_hash: "h1", responses: [
    { respondent_role: "SOC 主管", answers: [{ qid: "SOC-Q01", value: "X", text: "" }, { qid: "SOC-Q02", value: "", text: "三级" }] },
    { respondent_role: "值班员", answers: [] },
  ] });
});

test("进度：答了几题", () => {
  assert.deepEqual(progress(PACK, { respondent_role: "a", answers: { "SOC-Q01": { value: "B" }, "SOC-Q02": { text: " " } } }), { done: 1, total: 2 });
});

test("校验：没有答卷、角色缺失或超长、全空答卷、非法选项、超长", () => {
  assert.deepEqual(checkResponses(PACK, []), ["至少要有一份答卷"]);
  const errs = checkResponses(PACK, [
    { respondent_role: "", answers: { "SOC-Q01": { value: "A" } } },
    { respondent_role: "值班员", answers: {} },
    { respondent_role: "值".repeat(33), answers: { "SOC-Q01": { value: "Z" }, "SOC-Q02": { text: "长".repeat(4001) } } },
  ]);
  assert.deepEqual(errs, [
    "第 1 份答卷：请填答卷人角色",
    "第 2 份答卷：一题都没答",
    "第 3 份答卷：答卷人角色不超过 32 字（写角色不写姓名）",
    "第 3 份答卷 SOC-Q01：选项不在题目里",
    "第 3 份答卷 SOC-Q02：回答超过 4000 字",
  ]);
  assert.deepEqual(checkResponses(PACK, [{ respondent_role: "SOC 主管", answers: { "SOC-Q01": { value: "A" } } }]), []);
});

test("校验：超过 50 份答卷", () => {
  const many = Array.from({ length: 51 }, (_, i) => ({ respondent_role: `r${i}`, answers: { "SOC-Q01": { value: "A" } } }));
  assert.deepEqual(checkResponses(PACK, many), ["一次最多回传 50 份答卷"]);
});
