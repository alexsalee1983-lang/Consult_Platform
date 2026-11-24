// node --test（不引入新依赖）：运行层配置编辑页的规则与后端 services/runtime_config.py 一致（四期 G3③）
import { test } from "node:test";
import assert from "node:assert/strict";
import { STATUS, REASON_MAX, MAX_KEYS, parseValue, buildChanges, validateChange, canDecide, rollbackTargets,
  versionTitle, valueText } from "./runtimeConfig.js";

test("状态文字与后端三种状态一致", () => {
  assert.deepEqual(Object.keys(STATUS), ["pending", "applied", "rejected"]);
  assert.equal(REASON_MAX, 500);
  assert.equal(MAX_KEYS, 40);
});

test("按当前值类型解析输入", () => {
  assert.deepEqual(parseValue(true, "false"), { value: false });
  assert.deepEqual(parseValue(0.6, " 0.75 "), { value: 0.75 });
  assert.deepEqual(parseValue(3, "5"), { value: 5 });
  assert.match(parseValue(3, "abc").error, /数字/);
  assert.match(parseValue(3, "").error, /数字/);
  assert.deepEqual(parseValue("qwen-72b", " qwen-110b "), { value: "qwen-110b" });
  assert.match(parseValue("qwen-72b", "  ").error, /不能为空/);
});

test("只发改过的键", () => {
  const items = [{ key: "a", value: 0.6 }, { key: "b", value: true }, { key: "c", value: "m1" }];
  assert.deepEqual(buildChanges(items, { a: "0.6", b: "false", c: "m1" }), { values: { b: false }, errors: {} });
  assert.deepEqual(buildChanges(items, {}), { values: {}, errors: {} });
  const r = buildChanges(items, { a: "x" });
  assert.deepEqual(r.values, {});
  assert.match(r.errors.a, /数字/);
});

test("改动必须填理由、至少改一项、不超过 40 项", () => {
  assert.match(validateChange("", { a: 1 }), /理由/);
  assert.match(validateChange("   ", { a: 1 }), /理由/);
  assert.match(validateChange("x".repeat(501), { a: 1 }), /500/);
  assert.match(validateChange("调门槛", {}), /至少/);
  const many = Object.fromEntries(Array.from({ length: 41 }, (_, i) => [`k${i}`, i]));
  assert.match(validateChange("调门槛", many), /40/);
  assert.equal(validateChange("调门槛", { a: 1 }), null);
});

test("待确认的改动发起人本人不能确认或驳回", () => {
  const v = { id: 3, status: "pending", requested_by: "admin1" };
  assert.equal(canDecide(v, "admin2"), true);
  assert.equal(canDecide(v, "admin1"), false);
  assert.equal(canDecide({ ...v, status: "applied" }, "admin2"), false);
});

test("回滚目标只有已生效版本和 0（全部恢复 .env）", () => {
  const vs = [{ id: 4, status: "pending" }, { id: 3, status: "applied" }, { id: 2, status: "rejected" },
    { id: 1, status: "applied" }];
  assert.deepEqual(rollbackTargets(vs), [3, 1, 0]);
  assert.deepEqual(rollbackTargets([]), [0]);
});

test("版本标题与取值显示", () => {
  assert.equal(versionTitle({ id: 5, rollback_to: null }), "版本 5：改动");
  assert.equal(versionTitle({ id: 6, rollback_to: 3 }), "版本 6：回滚到版本 3");
  assert.equal(versionTitle({ id: 7, rollback_to: 0 }), "版本 7：全部恢复 .env");
  assert.equal(valueText(null), "（恢复 .env）");
  assert.equal(valueText(true), "是");
  assert.equal(valueText(false), "否");
  assert.equal(valueText(0.5), "0.5");
});
