import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  cacheableResponse, clampInterval, precacheList, registerShell, reminderState, route, shellAllowed, staleCaches,
} from "./offline.js";
import { buildSw, stripExports } from "./swBuild.js";
import { saveDraft } from "./draft.js";

const O = "https://cp.example";
const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

test("接口、跨站、非 GET 一律不拦截", () => {
  assert.equal(route("GET", `${O}/api/projects/BANK-A/survey/packs`, O, "cors"), "bypass");
  assert.equal(route("GET", `${O}/api`, O, "cors"), "bypass");
  assert.equal(route("GET", `${O}/api/me`, O, "navigate"), "bypass");
  assert.equal(route("POST", `${O}/assets/index-abc.js`, O, "cors"), "bypass");
  assert.equal(route("GET", "https://evil.example/assets/x.js", O, "cors"), "bypass");
  assert.equal(route("GET", `${O}/assets/x.js?v=1`, O, "cors"), "bypass");
  assert.equal(route("GET", `${O}/favicon-other.txt`, O, "no-cors"), "bypass");
});

test("页面导航走网络优先，构建产物走缓存", () => {
  assert.equal(route("GET", `${O}/project/BANK-A`, O, "navigate"), "navigate");
  assert.equal(route("GET", `${O}/assets/index-abc123.js`, O, "cors"), "asset");
  assert.equal(route("GET", `${O}/manifest.webmanifest`, O, "cors"), "asset");
});

test("只缓存完整的同源 200 响应", () => {
  assert.equal(cacheableResponse({ status: 200, type: "basic" }), true);
  assert.equal(cacheableResponse({ status: 404, type: "basic" }), false);
  assert.equal(cacheableResponse({ status: 0, type: "opaque" }), false);
  assert.equal(cacheableResponse(null), false);
});

test("预缓存清单只含页面壳与静态资源", () => {
  const list = precacheList(["assets/index-a.js", "assets/index-a.js.map", "index.html", "assets/index-b.css", "sw.js", "assets/index-a.js"]);
  assert.deepEqual(list, ["/", "/index.html", "/manifest.webmanifest", "/assets/index-a.js", "/assets/index-b.css"]);
  assert.ok(list.every((p) => !p.startsWith("/api")));
});

test("升级只清本平台旧壳缓存", () => {
  assert.deepEqual(staleCaches(["cp-shell-old", "cp-shell-new", "other"], "cp-shell-new"), ["cp-shell-old"]);
});

test("严格档不注册离线壳", async () => {
  const calls = [];
  const nav = { serviceWorker: { register: async (...a) => { calls.push(a); } } };
  assert.equal(shellAllowed("strict"), false);
  assert.equal(await registerShell(nav, "strict"), false);
  assert.equal(await registerShell(nav, ""), false);
  assert.equal(calls.length, 0);
  assert.equal(await registerShell(nav, "standard"), true);
  assert.deepEqual(calls, [["/sw.js", { scope: "/" }]]);
  assert.equal(await registerShell({}, "standard"), false);
});

test("严格档草稿不写 storage", () => {
  const writes = [];
  const store = { setItem: (...a) => writes.push(a) };
  assert.equal(saveDraft(store, { tier: "strict", project: "BANK-A", pack_hash: "h" }, [], "t"), false);
  assert.equal(writes.length, 0);
});

test("提醒间隔限定在 5～120 分钟", () => {
  assert.equal(clampInterval(1), 5);
  assert.equal(clampInterval(500), 120);
  assert.equal(clampInterval("30"), 30);
  assert.equal(clampInterval("x"), 20);
});

test("到点提醒导出摘要卡", () => {
  const t0 = 1_000_000;
  assert.deepEqual(reminderState(t0, t0 + 5 * 60000, 20), { due: false, elapsedMin: 5, remainMin: 15 });
  assert.equal(reminderState(t0, t0 + 20 * 60000, 20).due, true);
  assert.equal(reminderState(t0, t0 - 1000, 20).due, false);
});

// 把构建出的 /sw.js 放进假的 service worker 环境跑一遍：接口请求不拦截、不写缓存
function runSw() {
  const files = ["assets/index-a.js", "assets/index-b.css"];
  const { code, precache } = buildSw(read("./offline.js"), read("./sw.template.js"), files);
  const handlers = {};
  const puts = [];
  const cache = { addAll: async () => {}, put: async (req) => { puts.push(req.url); } };
  const caches = { open: async () => cache, keys: async () => [], delete: async () => true, match: async () => undefined };
  const self = {
    location: { origin: O },
    addEventListener: (t, fn) => { handlers[t] = fn; },
    skipWaiting: async () => {},
    clients: { claim: async () => {} },
  };
  const fetch = async (req) => ({ status: 200, type: "basic", url: req.url, clone() { return this; } });
  new Function("self", "caches", "fetch", code)(self, caches, fetch);
  return { handlers, puts, precache };
}

test("生成的 sw.js 不拦截接口、只缓存静态资源", async () => {
  const { handlers, puts, precache } = runSw();
  assert.ok(precache.includes("/assets/index-a.js"));
  const fire = async (method, url, mode) => {
    let p = null;
    handlers.fetch({ request: { method, url, mode }, respondWith: (x) => { p = x; } });
    if (p) await p;
    await new Promise((r) => setTimeout(r, 0));
    return p !== null;
  };
  assert.equal(await fire("GET", `${O}/api/projects/BANK-A/survey/packs`, "cors"), false);
  assert.equal(await fire("POST", `${O}/api/projects/BANK-A/survey/packs/P1/results`, "cors"), false);
  assert.deepEqual(puts, []);
  assert.equal(await fire("GET", `${O}/assets/index-a.js`, "cors"), true);
  assert.deepEqual(puts, [`${O}/assets/index-a.js`]);
});

test("去 export 后不残留模块语法", () => {
  const src = stripExports(read("./offline.js"));
  assert.ok(!/^\s*(export|import)\s/m.test(src));
});
