// 一期还没接 SSO，用 X-User 头标识操作人，写入审计日志
const USER = "李明";

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-User": encodeURIComponent(USER), ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data?.detail ?? data);
  return data;
}

export const api = {
  health: () => request("/api/health"),
  projects: () => request("/api/projects"),
  project: (code) => request(`/api/projects/${encodeURIComponent(code)}`),
  confirmations: () => request("/api/confirmations"),
  resolve: (id, resolution) =>
    request(`/api/confirmations/${id}/resolve`, { method: "POST", body: JSON.stringify({ resolution }) }),
  chat: (project_code, message) =>
    request("/api/chat", { method: "POST", body: JSON.stringify({ project_code, message }) }),
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
