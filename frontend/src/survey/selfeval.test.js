import { test } from "node:test";
import assert from "node:assert/strict";
import { confirmText, importBody, importSummary } from "./selfeval.js";

const PACK = { code: "S-01", version: 2 };
const BODY = { format: "cp-selfeval-answers", code: "S-01", selfeval_version: 2, pack_hash: "h",
  answers: [{ qid: "SOC-Q01", value: "B", note: "有分级表" }, { qid: "SOC-Q02" }] };

test("解密后的答卷转成导入请求体", () => {
  const b = importBody("selfeval-answers", BODY, PACK, true);
  assert.deepEqual(b, { version: 2, pack_hash: "h", confirm: true, answers: [
    { qid: "SOC-Q01", value: "B", note: "有分级表" }, { qid: "SOC-Q02", value: "", note: "" }] });
});

test("不是自评答卷、包号或版本不对在本机拦下", () => {
  assert.throws(() => importBody("upload", BODY, PACK), /不是客户自评答卷/);
  assert.throws(() => importBody("selfeval-answers", { ...BODY, answers: null }, PACK), /格式不对/);
  assert.throws(() => importBody("selfeval-answers", { ...BODY, code: "S-02" }, PACK), /S-02/);
  assert.throws(() => importBody("selfeval-answers", { ...BODY, selfeval_version: 1 }, PACK), /第 1 版/);
});

test("需确认的 409 给出说明，其他错误为空", () => {
  assert.match(confirmText({ needs_confirm: true, total: 3, unanswered: 3, comparable: 0, mismatch: 0, message: "请核对" }), /共 3 题，未答 3 题。请核对/);
  assert.match(confirmText({ needs_confirm: true, total: 4, unanswered: 0, comparable: 3, mismatch: 2 }), /不一致 2 题/);
  assert.equal(confirmText("别的错误"), "");
  assert.match(importSummary({ facts: 2, auto_confirmed: 0, unclear: 1, unanswered: 0, conflicts: 0 }), /事实 2 条/);
});
