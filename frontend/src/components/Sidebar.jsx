import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router";
import { api } from "../api.js";
import { hasBusinessRole, hasRole, isAuditor, isKnowledge, isSysAdmin, PASSWORD_PATH, ROLE_NAMES, useAuth } from "../auth.jsx";

const ICONS = {
  chat: <path d="M4 5h16v11H8l-4 4z" />,
  proj: <><path d="M3 7h18v13H3z" /><path d="M8 7V4h8v3" /></>,
  dash: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
  audit: <><path d="M12 2l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V5z" /><path d="M9 12h6M9 8h6M9 16h4" /></>,
  users: <><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20c.6-3.6 3.2-5.5 6.5-5.5s5.9 1.9 6.5 5.5" /><path d="M16 4.6a3.5 3.5 0 010 6.8M18 14.8c2 .7 3.2 2.5 3.5 5.2" /></>,
  doc: <><path d="M6 3h8l4 4v14H6z" /><path d="M14 3v4h4M9 12h6M9 16h6" /></>,
  key: <><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M17 6l3 3M15 8l2 2" /></>,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="M20 20l-4.2-4.2" /></>,
  inbox: <><path d="M3 13l3-8h12l3 8v6H3z" /><path d="M3 13h5l1.5 2.5h5L16 13h5" /></>,
  trash: <><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></>,
  review: <><path d="M9 11l2.5 2.5L16 9" /><rect x="4" y="3" width="16" height="18" rx="2" /></>,
  map: <><path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z" /><path d="M9 4v14M15 6v14" /></>,
};

function Icon({ name }) {
  return (
    <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round">
      {ICONS[name]}
    </svg>
  );
}

function statusText(v) {
  const s = String(v);
  if (s === "ok") return "正常";
  if (s === "configured") return "已接入";
  if (s.startsWith("demo")) return "演示模式";
  if (s.startsWith("not enabled")) return "未启用";
  return "异常";
}

function statusColor(v) {
  const s = String(v);
  if (s === "ok" || s === "configured") return "var(--ok)";
  if (s.startsWith("down")) return "var(--crim)";
  return "var(--tx3)";
}

// 驾驶舱和对话页沿用当前项目；在“我的项目”页时默认第一个虚拟客户
function currentCode(pathname) {
  const m = pathname.match(/^\/(?:project|chat)\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : "BANK-A";
}

export default function Sidebar() {
  const { user, logout } = useAuth();
  const { pathname } = useLocation();
  const code = encodeURIComponent(currentCode(pathname));
  const [health, setHealth] = useState(null);
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ status: "down", checks: {} }));
  }, []);

  // 导航按角色显示：业务角色看交付页面，安全审计员只看审计日志
  const items = [
    ...(hasBusinessRole(user) ? [
      { to: "/projects", icon: "proj", label: "我的项目" },
      { to: `/project/${code}`, icon: "dash", label: "项目驾驶舱", end: true },
      { to: `/project/${code}/deliverables`, icon: "doc", label: "交付件审阅" },
      { to: `/chat/${code}`, icon: "chat", label: "顾问对话" },
      ...(hasRole(user, "project_lead") ? [{ to: "/deletions", icon: "trash", label: "资料删除" }] : []),
    ] : []),
    ...(hasBusinessRole(user) ? [
      { group: "知识" },
      { to: "/kb/search", icon: "search", label: "知识检索" },
      { to: "/kb/intake", icon: "inbox", label: "投料箱" },
      ...(isKnowledge(user) ? [
        { to: "/kb/review", icon: "review", label: "审批队列" },
        { to: "/kb/map", icon: "map", label: "知识地图与健康" },
      ] : []),
    ] : []),
    ...(isAuditor(user) ? [{ to: "/audit", icon: "audit", label: "审计日志" }] : []),
    ...(isSysAdmin(user) ? [{ to: "/admin/users", icon: "users", label: "用户与角色" }] : []),
    { to: PASSWORD_PATH, icon: "key", label: "修改口令" },
  ];
  const name = user?.displayName || user?.username || "";
  const ok = health?.status === "ok";

  return (
    <aside className="pnl side" aria-label="导航">
      <div className="logo">
        <div className="logo-mark">
          <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#0A0D12" strokeWidth="2.4" strokeLinejoin="round">
            <path d="M12 2l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V5z" />
            <path d="M8.5 12l2.5 2.5 4.5-5" strokeLinecap="round" />
          </svg>
        </div>
        <div style={{ lineHeight: 1.2 }}>
          <div style={{ fontWeight: 700, fontSize: 14 }}>安全咨询专家平台</div>
          <div style={{ fontSize: 10, letterSpacing: ".16em", color: "var(--tx3)" }}>ADVISORY · 内部</div>
        </div>
      </div>

      <nav className="nav" aria-label="交付导航">
        {items.map((it) => (it.group ? <div key={it.group} className="cap nav-group">{it.group}</div> :
          <NavLink key={it.label} to={it.to} end={it.end} className={({ isActive }) => (isActive ? "on" : undefined)}>
            <Icon name={it.icon} />
            {it.label}
          </NavLink>
        ))}
      </nav>

      <section className="pad" style={{ padding: 12, borderRadius: 8, background: "var(--s2)", border: "1px solid var(--ln)", gap: 6 }}>
        <div className="sb">
          <span className="cap">系统状态</span>
          <span className={`chip ${ok ? "c-g" : health ? "c-r" : "c-n"}`}>{health ? (ok ? "正常" : "异常") : "检查中"}</span>
        </div>
        {health &&
          Object.entries(health.checks || {}).map(([k, v]) => (
            <div key={k} className="sb sub">
              <span>{{ database: "数据库", redis: "缓存", object_storage: "对象存储", worker: "后台任务", llm: "大模型", embedding: "向量服务" }[k] || k}</span>
              <span style={{ color: statusColor(v) }}>{statusText(v)}</span>
            </div>
          ))}
      </section>

      <div className="row" style={{ marginTop: "auto", gap: 10, paddingTop: 10, borderTop: "1px solid var(--ln)" }}>
        <div aria-hidden="true" style={{ flex: "none", width: 30, height: 30, borderRadius: "50%", background: "var(--s3)", border: "1px solid var(--ln2)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, color: "var(--amber)" }}>{name.slice(0, 1)}</div>
        <div style={{ lineHeight: 1.3, minWidth: 0, flex: 1 }}>
          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</div>
          <div className="sub">{(user?.roles || []).map((r) => ROLE_NAMES[r] || r).join("、") || user?.username}</div>
        </div>
        <button type="button" className="btn" style={{ height: 28, padding: "0 10px" }} onClick={logout}>退出</button>
      </div>
    </aside>
  );
}
