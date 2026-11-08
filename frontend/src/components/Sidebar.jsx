import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";

const ICONS = {
  chat: <path d="M4 5h16v11H8l-4 4z" />,
  proj: <><path d="M3 7h18v13H3z" /><path d="M8 7V4h8v3" /></>,
  dash: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
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
  if (s.startsWith("not enabled")) return "M1 启用";
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

  const items = [
    { to: "/projects", icon: "proj", label: "我的项目" },
    { to: `/project/${code}`, icon: "dash", label: "项目驾驶舱" },
    { to: `/chat/${code}`, icon: "chat", label: "顾问对话" },
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
        {items.map((it) => (
          <NavLink key={it.label} to={it.to} className={({ isActive }) => (isActive ? "on" : undefined)}>
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
              <span>{{ database: "数据库", redis: "缓存", object_storage: "对象存储", llm: "大模型" }[k] || k}</span>
              <span style={{ color: statusColor(v) }}>{statusText(v)}</span>
            </div>
          ))}
      </section>

      <div className="row" style={{ marginTop: "auto", gap: 10, paddingTop: 10, borderTop: "1px solid var(--ln)" }}>
        <div aria-hidden="true" style={{ flex: "none", width: 30, height: 30, borderRadius: "50%", background: "var(--s3)", border: "1px solid var(--ln2)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, color: "var(--amber)" }}>{name.slice(0, 1)}</div>
        <div style={{ lineHeight: 1.3, minWidth: 0, flex: 1 }}>
          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</div>
          <div className="sub">{user?.username}</div>
        </div>
        <button type="button" className="btn" style={{ height: 28, padding: "0 10px" }} onClick={logout}>退出</button>
      </div>
    </aside>
  );
}
