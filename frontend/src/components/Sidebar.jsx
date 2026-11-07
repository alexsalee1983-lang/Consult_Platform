import { useEffect, useState } from "react";
import { api } from "../api.js";

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

export default function Sidebar({ page, code }) {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ status: "down", checks: {} }));
  }, []);

  const items = [
    { href: "#/projects", icon: "proj", label: "我的项目", on: page === "projects" },
    { href: `#/project/${code}`, icon: "dash", label: "项目驾驶舱", on: page === "project" },
    { href: `#/chat/${code}`, icon: "chat", label: "顾问对话", on: page === "chat" },
  ];
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
          <a key={it.label} href={it.href} className={it.on ? "on" : undefined} aria-current={it.on ? "page" : undefined}>
            <Icon name={it.icon} />
            {it.label}
          </a>
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
              <span style={{ color: String(v).startsWith("ok") || v === "configured" ? "var(--ok)" : "var(--amber)" }}>{String(v).split(" ")[0]}</span>
            </div>
          ))}
      </section>

      <div className="row" style={{ marginTop: "auto", gap: 10, paddingTop: 10, borderTop: "1px solid var(--ln)" }}>
        <div style={{ width: 30, height: 30, borderRadius: "50%", background: "var(--s3)", border: "1px solid var(--ln2)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, color: "var(--amber)" }}>李</div>
        <div style={{ lineHeight: 1.3 }}>
          <div>李明</div>
          <div className="sub">高级顾问 · CISP</div>
        </div>
      </div>
    </aside>
  );
}
