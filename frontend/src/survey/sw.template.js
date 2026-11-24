// 四期 E2②b：离线壳 service worker 的事件处理（构建时与 offline.js 拼成 /sw.js，见 vite.config.js）。
// 版本号与预缓存清单由构建注入（swBuild.js）。只缓存页面壳与静态资源；接口和答卷内容不拦截、不缓存。
const VERSION = __VERSION__;
const PRECACHE = __PRECACHE__;
const CURRENT = cacheName(VERSION);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CURRENT).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(staleCaches(names, CURRENT).map((n) => caches.delete(n))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const how = route(req.method, req.url, self.location.origin, req.mode);
  if (how === "bypass") return;
  if (how === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match("/index.html", { cacheName: CURRENT })));
    return;
  }
  event.respondWith(
    caches.match(req, { cacheName: CURRENT }).then((hit) => hit || fetch(req).then((res) => {
      if (cacheableResponse(res)) {
        const copy = res.clone();
        caches.open(CURRENT).then((c) => c.put(req, copy));
      }
      return res;
    })),
  );
});
