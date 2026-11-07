import { PHASES } from "../api.js";

export function PhaseBar({ phase }) {
  return (
    <div aria-label={`阶段：${PHASES[phase]}`} style={{ display: "flex", gap: 3 }}>
      {PHASES.map((name, i) => (
        <i key={name} title={name} style={{ flex: 1, height: 4, borderRadius: 2,
          background: i < phase ? "var(--ok)" : i === phase ? "var(--amber)" : "var(--s3)" }} />
      ))}
    </div>
  );
}

export function Loading({ error }) {
  if (error) return <div className="pnl pad err">加载失败：{error.message || String(error)}</div>;
  return <div className="pnl pad sub">加载中……</div>;
}

export function TopBar({ cap, title, children }) {
  return (
    <div className="sb" style={{ flexWrap: "wrap" }}>
      <div style={{ lineHeight: 1.3, minWidth: 0 }}>
        <div className="cap">{cap}</div>
        <h1 className="h1">{title}</h1>
      </div>
      <div className="row">{children}</div>
    </div>
  );
}
