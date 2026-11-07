// 登录后服务端用 HttpOnly Cookie 维持会话；修改类请求另带 CSRF 令牌（登录和 /api/auth/me 返回）。
// 令牌只放在内存里，刷新页面后由 /api/auth/me 重新取回。
let csrfToken = "";
let onUnauthorized = () => {};

export function setCsrfToken(token) {
  csrfToken = token || "";
}

export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function request(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (UNSAFE.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !options.skipAuthRedirect) onUnauthorized();
    throw new ApiError(res.status, data?.detail ?? data);
  }
  return data;
}

export const api = {
  login: (username, password, totp) =>
    request("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password, totp }), skipAuthRedirect: true }),
  logout: () => request("/api/auth/logout", { method: "POST", skipAuthRedirect: true }),
  me: () => request("/api/auth/me", { skipAuthRedirect: true }),
  health: () => request("/api/health"),
  projects: () => request("/api/projects"),
  project: (code) => request(`/api/projects/${encodeURIComponent(code)}`),
  confirmations: () => request("/api/confirmations"),
  resolve: (id, resolution) =>
    request(`/api/confirmations/${id}/resolve`, { method: "POST", body: JSON.stringify({ resolution }) }),
  chat: (project_code, message) =>
    request("/api/chat", { method: "POST", body: JSON.stringify({ project_code, message }) }),
  audit: (params) => request(`/api/audit?${new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v != null))}`),
  verifyAudit: (expectHead) =>
    request(`/api/audit/verify${expectHead ? `?expect_head=${encodeURIComponent(expectHead)}` : ""}`),
};

export const PHASES = ["启动", "调研", "分析", "设计", "汇报", "验收"];
export const TIER = {
  standard: { label: "标准档", cls: "c-c" },
  strict: { label: "严格档", cls: "c-r" },
};
export const DELIV_STATUS = {
  signed: { label: "已签署", cls: "c-g" },
  review: { label: "待你审核", cls: "c-a" },
  drafting: { label: "起草中", cls: "c-c" },
  not_started: { label: "未开始", cls: "c-n" },
};
export const KIND_CLS = { 方案取舍: "c-a", 来源冲突: "c-v", 证据不足: "c-r", 入库确认: "c-g", 范围确认: "c-c" };
