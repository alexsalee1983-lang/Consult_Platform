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
    super(typeof detail === "string" ? detail
      : Array.isArray(detail?.reasons) ? detail.reasons.join("；")   // 门禁不过：{gate, reasons}
      : detail?.blocked && detail.summary ? detail.summary              // 机密级拦截：{blocked, summary, findings}
      : Array.isArray(detail) ? "提交的内容格式不正确" : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function request(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  // 上传文件（FormData）时由浏览器自动设置 multipart 边界，不能写死 JSON
  const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers = { ...(isForm ? {} : { "Content-Type": "application/json" }), ...(options.headers || {}) };
  if (UNSAFE.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !options.skipAuthRedirect) onUnauthorized();
    throw new ApiError(res.status, data?.detail ?? data);
  }
  return data;
}

// 流式接口返回 Server-Sent Events（event: delta / done / error）。
// onDelta(text) 每收到一段调用一次；返回 done 事件的数据。拦截（422）、无权限等在开始输出前以 ApiError 抛出。
export function chatStream(project_code, message, onDelta) {
  return streamPost("/api/chat/stream", { project_code, message }, onDelta);
}

// Agent 对话：发一条消息，流式返回
export function sendMessage(conversationId, message, onDelta) {
  return streamPost(`/api/conversations/${conversationId}/messages`, { message }, onDelta);
}

async function streamPost(url, body, onDelta) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    if (res.status === 401) onUnauthorized();
    throw new ApiError(res.status, data?.detail ?? data);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      const event = /^event: (.*)$/m.exec(block)?.[1];
      const raw = /^data: (.*)$/m.exec(block)?.[1];
      if (!event || raw == null) continue;
      const data = JSON.parse(raw);
      if (event === "delta") onDelta(data.text);
      else if (event === "done") return data;
      else if (event === "error") throw new ApiError(502, data.detail);
    }
  }
  throw new ApiError(502, "模型输出中断，请重试");
}

function dpath(code, dcode) {
  return `/api/projects/${encodeURIComponent(code)}/deliverables/${encodeURIComponent(dcode)}`;
}

// 下载文件（导出 Word）：出错时按接口的 detail 抛 ApiError，成功时让浏览器保存
async function download(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    if (res.status === 401) onUnauthorized();
    throw new ApiError(res.status, data?.detail ?? data);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : "export.docx";
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { name, exportId: res.headers.get("X-Export-Id") || "" };
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
  // 处理确认卡：选一个选项（choice）或写意见（resolution），或两者都有
  resolve: (id, { choice = "", resolution = "" }) =>
    request(`/api/confirmations/${id}/resolve`, { method: "POST", body: JSON.stringify({ choice, resolution }) }),
  projectConfirmations: (code, status = "open") =>
    request(`/api/projects/${encodeURIComponent(code)}/confirmations?status=${status}`),
  // 项目证据（D 资料 / I 访谈 / O 现场观察）
  evidences: (code) => request(`/api/projects/${encodeURIComponent(code)}/evidences`),
  evidence: (code, ev) => request(`/api/projects/${encodeURIComponent(code)}/evidences/${encodeURIComponent(ev)}`),
  uploadDocument: (code, file, title = "") => {
    const fd = new FormData();
    fd.append("file", file);
    if (title) fd.append("title", title);
    return request(`/api/projects/${encodeURIComponent(code)}/documents`, { method: "POST", body: fd });
  },
  addNote: (code, note) =>
    request(`/api/projects/${encodeURIComponent(code)}/notes`, { method: "POST", body: JSON.stringify(note) }),
  // 交付链运行（标准档 3→4→7）
  runs: (code) => request(`/api/projects/${encodeURIComponent(code)}/runs`),
  run: (code, id) => request(`/api/projects/${encodeURIComponent(code)}/runs/${id}`),
  startRun: (code) => request(`/api/projects/${encodeURIComponent(code)}/runs`, { method: "POST" }),
  cancelRun: (code, id) => request(`/api/projects/${encodeURIComponent(code)}/runs/${id}/cancel`, { method: "POST" }),
  chat: (project_code, message) =>
    request("/api/chat", { method: "POST", body: JSON.stringify({ project_code, message }) }),
  agents: () => request("/api/agents"),
  conversations: (code) => request(`/api/projects/${encodeURIComponent(code)}/conversations`),
  createConversation: (code, agent_code) =>
    request(`/api/projects/${encodeURIComponent(code)}/conversations`, { method: "POST", body: JSON.stringify({ agent_code }) }),
  conversation: (id) => request(`/api/conversations/${id}`),
  audit: (params) => request(`/api/audit?${new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v != null))}`),
  changePassword: (old_password, new_password) =>
    request("/api/auth/password", { method: "POST", body: JSON.stringify({ old_password, new_password }) }),
  // 项目成员（项目经理可调整）与用户目录
  members: (code) => request(`/api/projects/${encodeURIComponent(code)}/members`),
  putMember: (code, username, project_role) =>
    request(`/api/projects/${encodeURIComponent(code)}/members/${encodeURIComponent(username)}`,
      { method: "PUT", body: JSON.stringify({ project_role }) }),
  removeMember: (code, username) =>
    request(`/api/projects/${encodeURIComponent(code)}/members/${encodeURIComponent(username)}`, { method: "DELETE" }),
  lookupUsers: (q) => request(`/api/users/lookup?q=${encodeURIComponent(q)}`),
  // 系统管理员：账号与角色
  adminUsers: () => request("/api/admin/users"),
  createUser: (username, display_name, roles) =>
    request("/api/admin/users", { method: "POST", body: JSON.stringify({ username, display_name, roles }) }),
  setRoles: (username, roles) =>
    request(`/api/admin/users/${encodeURIComponent(username)}/roles`, { method: "PUT", body: JSON.stringify({ roles }) }),
  userAction: (username, action) =>   // disable / enable / unlock / reset-password / reset-totp
    request(`/api/admin/users/${encodeURIComponent(username)}/${action}`, { method: "POST" }),
  // ---- 知识服务 ----
  kbMeta: () => request("/api/kb/meta"),
  kbStats: () => request("/api/kb/stats"),
  kbUpload: (file) => {
    const fd = new FormData();
    fd.append("file", file);
    return request("/api/kb/intake", { method: "POST", body: fd });
  },
  kbUploads: () => request("/api/kb/intake"),
  kbUpload1: (id) => request(`/api/kb/intake/${id}`),
  kbDeid: (id) => request(`/api/kb/intake/${id}/deid`),
  kbDeidRerun: (id, extra_terms) =>
    request(`/api/kb/intake/${id}/deid/rerun`, { method: "POST", body: JSON.stringify({ extra_terms }) }),
  kbDeidDecide: (id, decision, comment) =>
    request(`/api/kb/intake/${id}/deid/decision`, { method: "POST", body: JSON.stringify({ decision, comment }) }),
  kbItems: (params = {}) => request(`/api/kb/items?${new URLSearchParams(Object.entries(params).filter(([, v]) => v))}`),
  kbItem: (docId) => request(`/api/kb/items/${encodeURIComponent(docId)}`),
  kbCreateItem: (body) => request("/api/kb/items", { method: "POST", body: JSON.stringify(body) }),
  kbRevise: (docId, body) =>
    request(`/api/kb/items/${encodeURIComponent(docId)}/versions`, { method: "POST", body: JSON.stringify(body) }),
  kbRetire: (docId, reason) =>
    request(`/api/kb/items/${encodeURIComponent(docId)}/retire`, { method: "POST", body: JSON.stringify({ reason }) }),
  kbReconfirm: (docId) => request(`/api/kb/items/${encodeURIComponent(docId)}/reconfirm`, { method: "POST" }),
  kbVersion: (id) => request(`/api/kb/versions/${id}`),
  kbVersions: (params = {}) => request(`/api/kb/versions?${new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null))}`),
  kbEditVersion: (id, changes) => request(`/api/kb/versions/${id}`, { method: "PATCH", body: JSON.stringify(changes) }),
  kbSubmit: (id) => request(`/api/kb/versions/${id}/submit`, { method: "POST" }),
  kbReview: (id, role, decision, comment) =>
    request(`/api/kb/versions/${id}/review`, { method: "POST", body: JSON.stringify({ role, decision, comment }) }),
  kbQueue: () => request("/api/kb/review-queue"),
  kbDue: (days = 30) => request(`/api/kb/review-due?days=${days}`),
  kbSearch: (params) => request(`/api/kb/search?${new URLSearchParams(Object.entries(params).filter(([, v]) => v))}`),
  kbBasis: (q) => request(`/api/kb/basis?q=${encodeURIComponent(q)}`),
  // ---- 交付件（M3）：章节与版本、质量评审、签署、导出 ----
  deliverable: (code, dcode) => request(dpath(code, dcode)),
  putOutline: (code, dcode, titles) =>
    request(`${dpath(code, dcode)}/outline`, { method: "PUT", body: JSON.stringify({ titles }) }),
  section: (code, dcode, seq, version) =>
    request(`${dpath(code, dcode)}/sections/${seq}${version ? `?version=${version}` : ""}`),
  putSection: (code, dcode, seq, body, note, base_version) =>
    request(`${dpath(code, dcode)}/sections/${seq}`, { method: "PUT", body: JSON.stringify({ body, note, base_version }) }),
  sectionDiff: (code, dcode, seq, a, b) => request(`${dpath(code, dcode)}/sections/${seq}/diff?a=${a}&b=${b}`),
  reviews: (code, dcode) => request(`${dpath(code, dcode)}/reviews`),
  review: (code, dcode, id) => request(`${dpath(code, dcode)}/reviews/${id}`),
  startReview: (code, dcode) => request(`${dpath(code, dcode)}/reviews`, { method: "POST" }),
  waive: (code, dcode, id, issue, reason) =>   // 项目经理驳回评审 Agent 的一条阻断
    request(`${dpath(code, dcode)}/reviews/${id}/waivers`, { method: "POST", body: JSON.stringify({ issue, reason }) }),
  signoff: (code, dcode) => request(`${dpath(code, dcode)}/signoff`),
  signoffAction: (code, dcode, action, comment = "") =>   // submit / sign / return
    request(`${dpath(code, dcode)}/signoff/${action}`, { method: "POST", body: JSON.stringify({ comment }) }),
  exportWord: (code, dcode) => download(`${dpath(code, dcode)}/export`),
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
  review: { label: "待签署", cls: "c-a" },
  drafting: { label: "起草中", cls: "c-c" },
  not_started: { label: "未开始", cls: "c-n" },
};
export const KIND_CLS = { 方案取舍: "c-a", 来源冲突: "c-v", 证据不足: "c-r", 入库确认: "c-g", 范围确认: "c-c" };
export const CONCLUSION_CLS = { pass: "c-g", revise: "c-a", fail: "c-r" };
export const SEVERITY_CLS = { 阻断: "c-r", 重要: "c-a", 建议: "c-n" };
export const EVIDENCE_STATUS = {
  received: { label: "待解析", cls: "c-a" }, parsed: { label: "已解析", cls: "c-g" },
  failed: { label: "解析失败", cls: "c-r" }, ready: { label: "已录入", cls: "c-g" },
};
export const RUN_STATUS = {
  queued: { label: "排队中", cls: "c-c" }, running: { label: "运行中", cls: "c-c" },
  waiting: { label: "等你确认", cls: "c-a" }, done: { label: "已完成", cls: "c-g" },
  failed: { label: "失败", cls: "c-r" }, cancelled: { label: "已取消", cls: "c-n" },
};
export const RUN_ACTIVE = new Set(["queued", "running", "waiting"]);
