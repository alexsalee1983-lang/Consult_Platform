// 现场工具（field-tool/field-tool.html）导出的加密包：在浏览器里解密，口令和明文都不发给服务器。
// 格式与算法见 field-tool/README.md（PBKDF2-SHA256 → AES-256-GCM，AAD = cp-field-package:1:<kind>）；
// field-tool/test/core.test.mjs 用现场工具加密、用本文件解密，核对两边一致。

const unb64 = (s) => Uint8Array.from(atob(s), (ch) => ch.charCodeAt(0));

// Web Crypto 只在 HTTPS 或 localhost 下可用
export const canDecrypt = () => Boolean(globalThis.crypto?.subtle);

export async function decryptPackage(pkg, pass) {
  if (!pkg || pkg.format !== "cp-field-package" || pkg.version !== 1) throw new Error("不是现场工具导出的加密包");
  if (!canDecrypt()) throw new Error("当前页面不是 HTTPS，浏览器不能解密；请在现场工具“查看加密包”解密后粘贴内容");
  const subtle = globalThis.crypto.subtle;
  const base = await subtle.importKey("raw", new TextEncoder().encode(pass || ""), "PBKDF2", false, ["deriveKey"]);
  const key = await subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: unb64(pkg.kdf.salt), iterations: pkg.kdf.iterations },
    base, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  let plain;
  try {
    plain = await subtle.decrypt({ name: "AES-GCM", iv: unb64(pkg.cipher.iv),
      additionalData: new TextEncoder().encode("cp-field-package:1:" + pkg.kind) }, key, unb64(pkg.data));
  } catch {
    throw new Error("口令不对或文件已损坏");
  }
  return { kind: pkg.kind, body: JSON.parse(new TextDecoder().decode(plain)) };
}

// 上传包正文 → 上传接口请求体；不是上传包、项目不对、格式不对都拒绝
export function uploadCards(kind, body, code) {
  if (kind !== "upload") throw new Error("这是现场工作文件（含对照表），不能上传；请导出上传包");
  if (!body || !Array.isArray(body.cards)) throw new Error("内容格式不对：没有 cards");
  if (body.project_code && String(body.project_code).toUpperCase() !== String(code).toUpperCase()) {
    throw new Error(`这个上传包属于项目 ${body.project_code}，不是 ${code}`);
  }
  return body.cards.map((c) => ({ item: c.item || "", finding: c.finding || "", magnitude: c.magnitude || "",
    onsite_refs: Array.isArray(c.onsite_refs) ? c.onsite_refs : [], client_reviewed_on: c.client_reviewed_on || null }));
}
