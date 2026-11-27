import { readFileSync } from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { buildSw, MANIFEST } from "./src/survey/swBuild.js";

// 四期 E2②b：构建时按产物清单生成离线壳 /sw.js 和 /manifest.webmanifest（只缓存静态资源，不缓存接口）
function swPlugin() {
  return {
    name: "cp-offline-shell",
    apply: "build",
    generateBundle(_, bundle) {
      const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
      const { code } = buildSw(read("./src/survey/offline.js"), read("./src/survey/sw.template.js"), Object.keys(bundle));
      this.emitFile({ type: "asset", fileName: "sw.js", source: code });
      this.emitFile({ type: "asset", fileName: "manifest.webmanifest", source: JSON.stringify(MANIFEST, null, 2) });
    },
  };
}

// 本地开发：npm run dev，/api 转发到本机 8000 端口的后端
export default defineConfig({
  plugins: [react(), swPlugin()],
  // 组件测试（Vitest + jsdom）：只收 *.test.jsx；*.test.js 仍由 node --test 跑
  test: { include: ["src/**/*.test.jsx"], environment: "jsdom" },
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
