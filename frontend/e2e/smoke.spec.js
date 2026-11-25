// 登录测试账号 → 我的项目 → 虚拟项目 BANK-A 的驾驶舱，全程浏览器无 console error、无未捕获异常。
// 账号来自 CI 随机生成的 .env（E2E_USER / E2E_PASS），动态码由 CI 在新的 30 秒窗口里算好传入（E2E_TOTP）。
import { expect, test } from "@playwright/test";

test("登录 → 我的项目 → 项目驾驶舱", async ({ page }) => {
  const errors = [];
  await page.goto("/login");
  await page.getByLabel("用户名").fill(process.env.E2E_USER);
  await page.getByLabel("口令").fill(process.env.E2E_PASS);
  await page.getByLabel(/动态码/).fill(process.env.E2E_TOTP);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).not.toHaveURL(/\/login/);
  // 登录前 /api/auth/me 返回 401 属预期，从登录后开始收集
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "我的项目" })).toBeVisible();
  await page.locator('a[href="/project/BANK-A"]').click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("项目驾驶舱");
  await page.waitForTimeout(3000);   // 等驾驶舱各面板的接口都回来
  expect(errors).toEqual([]);
});
