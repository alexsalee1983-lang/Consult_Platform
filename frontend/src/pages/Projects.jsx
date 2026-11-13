import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, KIND_CLS, RETENTION, TIER } from "../api.js";
import { Loading, PhaseBar, TopBar } from "../components/common.jsx";

function ProjectCard({ p }) {
  const tier = TIER[p.tier] || TIER.standard;
  const scoreColor = p.posture_score == null ? "var(--tx3)" : p.posture_score < 60 ? "var(--crim)" : "var(--amber)";
  return (
    <Link to={`/project/${encodeURIComponent(p.code)}`} className="pnl" style={{ padding: "14px 16px", display: "grid", gridTemplateColumns: "minmax(0,1fr) 220px 120px", gap: 18, alignItems: "center", color: "inherit", textDecoration: "none" }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span className="mono" style={{ fontWeight: 700, color: "var(--amber)" }}>{p.code}</span>
          <span style={{ fontSize: 15, fontWeight: 700 }}>{p.service}</span>
          <span className={`chip ${tier.cls}`}>{tier.label}</span>
        </div>
        <div className="row sub" style={{ flexWrap: "wrap" }}>
          <span>{p.industry}</span>·<span>第 {p.week} / {p.total_weeks} 周</span>·<span>下一步：{p.next_milestone}</span>
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <PhaseBar phase={p.phase} />
        <div className="sb sub"><span>{p.phase_name}阶段</span><span>验收</span></div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
        <span className="mono" style={{ fontSize: 20, fontWeight: 700, color: scoreColor }}>{p.posture_score ?? "—"}</span>
        {p.open_confirmations > 0 && <span className="chip c-a">待你确认 {p.open_confirmations}</span>}
        {["due_soon", "due"].includes(p.retention_state) && (
          <span className={`chip ${RETENTION[p.retention_state].cls}`} title={`到期日 ${p.deletion_due_on}`}>
            {p.retention_state === "due" ? "保留期已到期" : `${p.days_to_deletion} 天后到期`}
          </span>
        )}
      </div>
    </Link>
  );
}

export default function Projects() {
  const [projects, setProjects] = useState(null);
  const [confs, setConfs] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([api.projects(), api.confirmations()])
      .then(([p, c]) => { setProjects(p); setConfs(c); })
      .catch(setError);
  }, []);

  if (!projects) return <Loading error={error} />;

  return (
    <>
      <TopBar cap={`交付 · ${projects.length} 个进行中项目`} title="我的项目">
        <button className="btnp" disabled title="M1 开放">＋ 新建项目</button>
      </TopBar>
      <div className="grid-main">
        <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
          <div className="sb sub" style={{ padding: "0 4px" }}><span>项目 · 阶段 · 态势评分</span><span>点击进入项目驾驶舱</span></div>
          {projects.map((p) => <ProjectCard key={p.code} p={p} />)}
          <div className="sub" style={{ padding: "4px" }}>严格档项目的资料不出客户现场，平台只接收客户书面同意后的去标识摘要卡。</div>
        </div>
        <section id="confirm" className="pnl pad" aria-label="待我确认">
          <div className="sb"><h2 className="h2">待我确认</h2><span className="mono" style={{ color: "var(--amber)" }}>{confs.length}</span></div>
          <ul className="lst">
            {confs.map((c) => (
              <li key={c.id} style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <div className="sb"><span className={`chip ${KIND_CLS[c.kind] || "c-n"}`}>{c.kind}</span><span className="sub">{c.project_code} · {c.agent}</span></div>
                <Link to={`/project/${encodeURIComponent(c.project_code)}`} style={{ color: "var(--tx)", textDecoration: "none" }}>{c.title}</Link>
              </li>
            ))}
            {confs.length === 0 && <li className="sub">没有待确认事项</li>}
          </ul>
        </section>
      </div>
    </>
  );
}
