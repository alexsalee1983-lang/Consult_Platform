import { defineConfig } from "@playwright/test";

// 浏览器端到端冒烟（四期 质量）：CI compose job 起整套（模拟模型）后跑；用 runner 自带的 Google Chrome，不下载浏览器
export default defineConfig({
  testDir: "e2e",
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://localhost:8080",
    channel: "chrome",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
