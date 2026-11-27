// 四期 E2②b：调研助手现场端的离线壳与严格档定时导出提醒（纯逻辑，node --test 覆盖）。
// 离线壳只缓存前端静态资源与页面壳（index.html、/assets/*、manifest）；接口响应、答卷内容一律不进缓存。
// 普通档答卷草稿仍按 E2②a 存浏览器本地（draft.js）；严格档只在内存，不写 storage、不写 cache（决策记录 2026-11-24）。
// 本文件同时被打进 /sw.js（vite.config.js 的 swPlugin 去掉 export 后拼接），所以不能 import 别的模块。

export const CACHE_PREFIX = "cp-shell-";
export const SHELL_PATHS = ["/", "/index.html", "/manifest.webmanifest"];

export const cacheName = (version) => `${CACHE_PREFIX}${version}`;

// 只有本站静态资源可缓存：GET、同源、不是 /api/，路径是页面壳或 /assets/ 下的构建产物
export function isShellAsset(method, url, origin) {
  if (method !== "GET") return false;
  let u;
  try {
    u = new URL(url, origin);
  } catch {
    return false;
  }
  if (u.origin !== origin || u.search) return false;
  if (u.pathname.startsWith("/api/") || u.pathname === "/api") return false;
  return SHELL_PATHS.includes(u.pathname) || /^\/assets\/[\w.-]+$/.test(u.pathname);
}

// service worker 对一个请求的处理方式：
//   bypass   —— 不拦截，浏览器照常走网络（接口、跨站、非 GET、其他路径），绝不写缓存
//   navigate —— 页面导航：先走网络，断网时回落到缓存的 index.html
//   asset    —— 构建产物（文件名带哈希）：先读缓存，没有再走网络并写入缓存
export function route(method, url, origin, mode) {
  if (method !== "GET") return "bypass";
  let u;
  try {
    u = new URL(url, origin);
  } catch {
    return "bypass";
  }
  if (u.origin !== origin || u.pathname.startsWith("/api/") || u.pathname === "/api") return "bypass";
  if (mode === "navigate") return "navigate";
  return isShellAsset(method, url, origin) ? "asset" : "bypass";
}

// 只有完整的同源 200 响应才写缓存（不缓存错误页、重定向、不透明响应）
export const cacheableResponse = (res) => Boolean(res) && res.status === 200 && res.type === "basic";

// 升级后清掉旧版本的壳缓存（只动本平台前缀的缓存）
export const staleCaches = (names, current) => names.filter((n) => n.startsWith(CACHE_PREFIX) && n !== current);

// 预缓存清单：页面壳 + 构建产物里的 js/css/字体/图片（不含 sw.js 自己和 source map）
export function precacheList(files) {
  const assets = files
    .filter((f) => /^assets\/[\w.-]+\.(js|css|woff2?|svg|png|ico)$/.test(f))
    .map((f) => `/${f}`);
  return [...SHELL_PATHS, ...[...new Set(assets)].sort()];
}

// 是否注册离线壳：只给普通档；严格档不注册（现场端不留任何本机副本）
export const shellAllowed = (tier) => Boolean(tier) && tier !== "strict";

export async function registerShell(nav, tier) {
  if (!shellAllowed(tier) || !nav || !nav.serviceWorker) return false;
  await nav.serviceWorker.register("/sw.js", { scope: "/" });
  return true;
}

// —— 严格档定时导出摘要卡提醒（只在内存计时，不落盘）——
export const REMIND_DEFAULT_MIN = 20;
export const REMIND_MIN = 5;
export const REMIND_MAX = 120;
export const REMIND_CHOICES = [10, 20, 30, 60];

export function clampInterval(min) {
  const n = Math.round(Number(min));
  if (!Number.isFinite(n)) return REMIND_DEFAULT_MIN;
  return Math.min(REMIND_MAX, Math.max(REMIND_MIN, n));
}

// since：上次导出（或开始填答）的毫秒时间戳；now：当前毫秒时间戳
export function reminderState(since, now, intervalMin) {
  const ms = clampInterval(intervalMin) * 60000;
  const elapsed = Math.max(0, now - since);
  return {
    due: elapsed >= ms,
    elapsedMin: Math.floor(elapsed / 60000),
    remainMin: Math.max(0, Math.ceil((ms - elapsed) / 60000)),
  };
}
