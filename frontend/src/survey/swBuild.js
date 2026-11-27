// 四期 E2②b：构建时生成 /sw.js 与 /manifest.webmanifest（由 vite.config.js 调用，node --test 覆盖）。
import { createHash } from "node:crypto";
import { precacheList } from "./offline.js";

export const stripExports = (src) => src.replace(/^export (?=(const|function|async function) )/gm, "");

export function buildSw(offlineSrc, templateSrc, files) {
  const precache = precacheList(files);
  const version = createHash("sha256").update(precache.join("\n")).digest("hex").slice(0, 12);
  const body = templateSrc
    .replace("const VERSION = __VERSION__;", `const VERSION = ${JSON.stringify(version)};`)
    .replace("const PRECACHE = __PRECACHE__;", `const PRECACHE = ${JSON.stringify(precache)};`);
  return { version, precache, code: `${stripExports(offlineSrc)}\n${body}` };
}

export const MANIFEST = {
  name: "安全咨询专家平台",
  short_name: "咨询平台",
  start_url: "/",
  scope: "/",
  display: "standalone",
  lang: "zh-CN",
};
