import { test } from "node:test";
import assert from "node:assert/strict";
import { checkForm, emptyValues, formFields, listCount } from "./form.js";

const FORM = {
  code: "D1", title: "启动包", writable: true,
  sections: [
    { title: "项目概况", fixed: "", drafted_by_agent: false, fields: [
      { key: "goal", label: "项目目标", type: "textarea", required: true, hint: "", max: 2000 },
      { key: "period", label: "项目周期", type: "text", required: true, hint: "例如 2027-01-05 至 2027-03-31", max: 200 },
    ] },
    { title: "范围确认", fixed: "差距分析，不出具测评结论。", drafted_by_agent: false, fields: [
      { key: "in_scope", label: "本次范围（每行一项）", type: "list", required: true, hint: "", max: 4000 },
      { key: "assumptions", label: "前提与依赖（每行一项）", type: "list", required: false, hint: "", max: 4000 },
    ] },
    { title: "风险与沟通", fixed: "", drafted_by_agent: true, fields: [] },
  ],
};

test("字段平铺、初值为空串", () => {
  assert.deepEqual(formFields(FORM).map((f) => f.key), ["goal", "period", "in_scope", "assumptions"]);
  assert.deepEqual(emptyValues(FORM), { goal: "", period: "", in_scope: "", assumptions: "" });
});

test("列表项计数：去空行和项目符号", () => {
  assert.equal(listCount("- 甲\n\n * 乙\n•丙\n  "), 3);
  assert.equal(listCount(""), 0);
});

test("必填未填：逐项报错，全部填好后无错", () => {
  const v = emptyValues(FORM);
  const e1 = checkForm(FORM, v);
  assert.deepEqual(Object.keys(e1).sort(), ["goal", "in_scope", "period"]);
  assert.match(e1.goal, /必填/);
  const ok = checkForm(FORM, { ...v, goal: "差距分析", period: "2027-01 至 2027-03", in_scope: "- 核心系统" });
  assert.deepEqual(ok, {});
});

test("只有空白算未填；超长与列表超 50 项报错", () => {
  const base = { goal: "x", period: "y", in_scope: "a", assumptions: "" };
  assert.ok(checkForm(FORM, { ...base, goal: "   \n " }).goal);
  assert.match(checkForm(FORM, { ...base, period: "字".repeat(201) }).period, /200/);
  const many = Array.from({ length: 51 }, (_, i) => `项${i}`).join("\n");
  assert.match(checkForm(FORM, { ...base, assumptions: many }).assumptions, /50/);
});

test("产品配置预填：default 作初值，没有 default 的为空串（issue 468）", () => {
  const form = { sections: [{ title: "工作方法与方法论", fixed: "", drafted_by_agent: false, fields: [
    { key: "frameworks", label: "依据框架与标准", type: "list", required: true, hint: "", max: 4000, default: "等保 2.0" },
    { key: "approach", label: "方法说明", type: "textarea", required: false, hint: "", max: 2000 },
  ] }] };
  assert.deepEqual(emptyValues(form), { frameworks: "等保 2.0", approach: "" });
  assert.deepEqual(checkForm(form, emptyValues(form)), {});
});
