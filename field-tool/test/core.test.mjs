// 现场工具核心逻辑测试：node --test field-tool/test/（不需要安装依赖）
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { inflateRawSync, deflateRawSync } from "node:zlib";
import vm from "node:vm";

const here = new URL(".", import.meta.url);
const html = readFileSync(new URL("../field-tool.html", here), "utf8");
const core = html.match(/<script id="core">([\s\S]*?)<\/script>/)[1];
vm.runInThisContext(core);
const F = globalThis.FieldCore;
const C = F.compile(null);
const inflate = async (u8) => new Uint8Array(inflateRawSync(u8));

test("页面不联网：CSP 禁止外连，没有外部资源", () => {
  assert.match(html, /connect-src 'none'/);
  assert.match(html, /default-src 'none'/);
  assert.doesNotMatch(html, /<(?:script|link|img|iframe)[^>]+(?:src|href)=["']?(?:https?:)?\/\//i);
  assert.doesNotMatch(core, /\bfetch\(|XMLHttpRequest|WebSocket|localStorage|sendBeacon/);
});

test("颜色令牌与 frontend/src/theme.css 一致", () => {
  const theme = readFileSync(new URL("../../frontend/src/theme.css", here), "utf8");
  const root = (s) => s.match(/:root\s*\{([\s\S]*?)\}/)[1].replace(/\s+/g, " ").trim();
  assert.equal(root(html), root(theme));
  const style = html.match(/<style>([\s\S]*?)<\/style>/)[1].replace(/:root\s*\{[\s\S]*?\}/, "");
  assert.doesNotMatch(style, /#[0-9a-fA-F]{3,8}\b|rgba?\(/, "样式里只能用令牌变量");
});

test("摘要卡规则与平台一致（共用 cases.json，平台侧由 tests/test_field_tool.py 核对）", () => {
  const cases = JSON.parse(readFileSync(new URL("cases.json", here), "utf8"));
  for (const c of cases) assert.deepEqual(F.cardProblems(c.text, C, []).filter((k) => k !== "机密级"), c.kinds, c.text);
});

test("机密级简化检查和对照表真名", () => {
  assert.ok(F.cardProblems("管理员口令为 Abc12345", C, []).includes("机密级"));
  assert.ok(F.cardProblems("-----BEGIN RSA PRIVATE KEY-----", C, []).includes("机密级"));
  const map = [{ placeholder: "【系统A】", real: "天枢平台", kind: "系统" }];
  assert.deepEqual(F.cardProblems("天枢平台的特权账号未复核", C, map), ["系统名称"]);
  assert.deepEqual(F.cardProblems("【系统A】的特权账号未复核", C, map), []);
});

test("整张卡检查：必填、证据编号（含三段）、审阅日期", () => {
  const ok = { item: "特权账号管理", finding: "【系统A】特权账号约 30 个，未定期复核", magnitude: "约 30 个",
    onsite_refs: "D-03 I-04-07，o-02", client_reviewed_on: "2026-11-01" };
  assert.deepEqual(F.checkCard(ok, C, []), []);
  assert.deepEqual(F.normRefs(ok.onsite_refs), ["D-03", "I-04-07", "O-02"]);
  const bad = F.checkCard({ ...ok, item: "", onsite_refs: "第3页", client_reviewed_on: "2999-01-01" }, C, []);
  assert.deepEqual(bad.map((p) => p.field).sort(), ["client_reviewed_on", "item", "onsite_refs"]);
  assert.deepEqual(F.uploadBody([ok]).cards[0].onsite_refs, ["D-03", "I-04-07", "O-02"]);
});

test("加密包：往返、口令错、类型不能换", async () => {
  const body = { project_code: "GRID-B", cards: [{ item: "x" }] };
  await assert.rejects(F.encryptPackage("upload", body, "short"), /12/);
  const pkg = await F.encryptPackage("upload", body, "correct horse battery", 1000);
  assert.equal(pkg.format, "cp-field-package");
  assert.doesNotMatch(JSON.stringify(pkg), /GRID-B/);
  assert.deepEqual((await F.decryptPackage(pkg, "correct horse battery")).body, body);
  await assert.rejects(F.decryptPackage(pkg, "wrong horse battery"), /口令不对/);
  await assert.rejects(F.decryptPackage({ ...pkg, kind: "work" }, "correct horse battery"), /口令不对/);
});

function zip(files) { // 用 deflate 写一个测试 docx
  return F.writeZip(files.map(([name, text]) => ({ name, data: new TextEncoder().encode(text) })));
}

test("Word 回填：替换正文、页眉、脚注，转义特殊字符，报告拆开的占位符", async () => {
  const doc = '<w:document><w:body><w:p><w:r><w:t>【客户】的【系统A】未复核</w:t></w:r></w:p>' +
    '<w:p><w:r><w:t>【人员</w:t></w:r><w:r><w:t>1】确认</w:t></w:r></w:p></w:body></w:document>';
  const raw = zip([["[Content_Types].xml", "<Types/>"], ["word/document.xml", doc],
    ["word/header1.xml", "<w:hdr><w:p><w:r><w:t>【客户】</w:t></w:r></w:p></w:hdr>"], ["word/media/a.bin", "【客户】"]]);
  const map = [{ placeholder: "【客户】", real: "A&B 公司", kind: "机构" }, { placeholder: "【系统A】", real: "天枢", kind: "系统" },
    { placeholder: "【人员1】", real: "某某", kind: "人员" }];
  const r = await F.fillDocx(raw, map, inflate);
  assert.equal(r.count, 3);
  assert.deepEqual(r.leftovers, ["【人员1】"]);
  const out = Object.fromEntries((await F.readZip(r.bytes, inflate)).map((e) => [e.name, new TextDecoder().decode(e.data)]));
  assert.match(out["word/document.xml"], /A&amp;B 公司的天枢未复核/);
  assert.match(out["word/header1.xml"], /A&amp;B 公司/);
  assert.equal(out["word/media/a.bin"], "【客户】");
});

test("zip 读取 deflate 压缩的条目，CRC 正确", async () => {
  const data = new TextEncoder().encode("<w:t>【客户】</w:t>".repeat(50));
  const comp = deflateRawSync(data);
  const plain = zip([["word/document.xml", ""]]);
  // 把存储条目改造成 deflate 条目：重写一个最小 zip
  const name = new TextEncoder().encode("word/document.xml");
  const lh = Buffer.alloc(30); lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4); lh.writeUInt16LE(8, 8);
  lh.writeUInt32LE(F.crc32(data), 14); lh.writeUInt32LE(comp.length, 18); lh.writeUInt32LE(data.length, 22); lh.writeUInt16LE(name.length, 26);
  const ch = Buffer.alloc(46); ch.writeUInt32LE(0x02014b50, 0); ch.writeUInt16LE(8, 10); ch.writeUInt32LE(F.crc32(data), 16);
  ch.writeUInt32LE(comp.length, 20); ch.writeUInt32LE(data.length, 24); ch.writeUInt16LE(name.length, 28);
  const end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10);
  end.writeUInt32LE(46 + name.length, 12); end.writeUInt32LE(30 + name.length + comp.length, 16);
  const buf = Buffer.concat([lh, name, comp, ch, name, end]);
  const [e] = await F.readZip(buf, inflate);
  assert.equal(new TextDecoder().decode(e.data), new TextDecoder().decode(data));
  assert.ok(plain.length > 0);
  assert.equal(F.crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
});
