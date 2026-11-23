// node --test（不引入新依赖）：来源登记页面按钮规则与后端 source_registry.py 一致
import { test } from "node:test";
import assert from "node:assert/strict";
import { sourceActions, validateSource, TRANSITIONS, AUTHORITY_KINDS } from "./sourceRegistry.js";

const admin = { username: "kadmin", roles: ["knowledge_admin"] };
const expert = { username: "kexpert", roles: ["knowledge_expert"] };

test("转正式只限知识管理员", () => {
  const a = sourceActions({ status: "trial", created_by: "kexpert" }, expert).find((x) => x.to === "active");
  assert.equal(a.disabled, true);
  assert.match(a.hint, /知识管理员/);
  const b = sourceActions({ status: "trial", created_by: "kexpert" }, admin).find((x) => x.to === "active");
  assert.equal(b.disabled, false);
});

test("登记人不能批准自己登记的来源", () => {
  const a = sourceActions({ status: "demoted", created_by: "kadmin" }, admin).find((x) => x.to === "active");
  assert.equal(a.disabled, true);
  assert.match(a.hint, /登记人/);
});

test("降级、停用必须写理由，其余不必", () => {
  const acts = sourceActions({ status: "active", created_by: "x" }, admin);
  assert.deepEqual(acts.map((x) => [x.to, x.needsReason]), [["demoted", true], ["retired", true]]);
  const c = sourceActions({ status: "candidate", created_by: "x" }, expert).find((x) => x.to === "trial");
  assert.equal(c.needsReason, false);
});

test("流转表与后端一致；停用后只能重新登记为候选", () => {
  assert.deepEqual(TRANSITIONS.retired, ["candidate"]);
  const [r] = sourceActions({ status: "retired", created_by: "x" }, expert);
  assert.equal(r.label, "重新登记为候选");
  assert.equal(AUTHORITY_KINDS.length, 7);
});

test("登记表单：许可、权威类别必填，网址须 http(s)", () => {
  const ok = { name: "示例来源", url: "https://example.org/a", license: "可内部引用，须注明出处", authority_kind: "国家标准" };
  assert.equal(validateSource(ok), "");
  assert.match(validateSource({ ...ok, license: " " }), /许可必填/);
  assert.match(validateSource({ ...ok, authority_kind: "" }), /权威类别必填/);
  assert.match(validateSource({ ...ok, url: "ftp://x" }), /http/);
});
