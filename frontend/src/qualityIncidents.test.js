// node --test（不引入新依赖）：质量事件处置页的规则与后端 services/quality_incidents.py 一致（四期 F3②）
import { test } from "node:test";
import assert from "node:assert/strict";
import { statusLabel, CATEGORIES, STEPS, ACTIONS, ROOT_CAUSES, LEVELS, validateEvent, eventPayload, nextStep, dueText } from "./qualityIncidents.js";

test("清单与后端一致", () => {
  assert.deepEqual(Object.keys(CATEGORIES), ["fabricated_citation", "repealed_basis", "fact_mismatch",
    "qualification_wording", "cross_project_leak", "improper_advice"]);
  assert.deepEqual(STEPS.map((s) => s.key), ["register", "grade", "contain", "assess", "correct", "root_cause",
    "prevent", "review"]);
  assert.deepEqual(Object.keys(ACTIONS), ["freeze_export", "offline_item", "rollback_agent", "lower_autonomy"]);
  assert.deepEqual(Object.keys(ROOT_CAUSES), ["knowledge", "retrieval", "md", "model", "gate", "human_signoff"]);
  assert.deepEqual(LEVELS, ["P1", "P2", "P3"]);
});

test("登记一步不能手工追加，分级、遏制、根因必须选清单内的值", () => {
  assert.match(validateEvent({ step: "register" }), /八步/);
  assert.match(validateEvent({ step: "grade", level: "" }), /P1/);
  assert.equal(validateEvent({ step: "grade", level: "P2" }), null);
  assert.match(validateEvent({ step: "contain", action: "x" }), /遏制/);
  assert.equal(validateEvent({ step: "contain", action: "freeze_export" }), null);
  assert.match(validateEvent({ step: "root_cause", root_cause: "" }), /根因/);
  assert.equal(validateEvent({ step: "assess" }), null);
  assert.match(validateEvent({ step: "assess", note: "x".repeat(2001) }), /2000/);
});

test("只发本步需要的字段", () => {
  assert.deepEqual(eventPayload({ step: "contain", level: "P1", action: "freeze_export", root_cause: "md", note: "n" }),
    { step: "contain", action: "freeze_export", note: "n" });
  assert.deepEqual(eventPayload({ step: "review", level: "P1", note: "" }), { step: "review", note: "" });
});

test("下一步按八步顺序取最后一步之后，已复盘返回 null", () => {
  assert.equal(nextStep([]), "grade");
  assert.equal(nextStep([{ step: "register" }]), "grade");
  assert.equal(nextStep([{ step: "register" }, { step: "grade" }, { step: "contain" }]), "assess");
  assert.equal(nextStep([{ step: "register" }, { step: "review" }]), null);
});

test("遏制时限文字", () => {
  const now = new Date("2026-11-24T10:00:00Z");
  assert.equal(dueText({ contain_due: null }, now), "未分级");
  assert.equal(dueText({ level: "P3", contain_due: null }, now), "不设时限");
  assert.equal(dueText({ contain_due: "2026-11-24T08:00:00Z", contained_at: "2026-11-24T07:00:00Z" }, now), "已遏制");
  assert.equal(dueText({ contain_due: "2026-11-24T08:00:00Z", contained_at: "2026-11-24T09:00:00Z", overdue: true }, now),
    "遏制超时");
  assert.equal(dueText({ contain_due: "2026-11-24T08:00:00Z", overdue: true }, now), "已超时 2 小时");
  assert.equal(dueText({ contain_due: "2026-11-24T13:30:00Z" }, now), "剩 3 小时");
});

test("进度文字按后端 status", () => {
  assert.equal(statusLabel("registered"), "已登记");
  assert.equal(statusLabel("contain"), "已遏制");
  assert.equal(statusLabel("closed"), "已关闭");
});
