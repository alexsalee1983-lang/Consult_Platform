import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, DELIV_STATUS, TIER } from "../api.js";
import { Loading, PhaseBar, TopBar } from "../components/common.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import Evidence from "../components/Evidence.jsx";
import Members from "../components/Members.jsx";
import Retention from "../components/Retention.jsx";
import RunPanel from "../components/RunPanel.jsx";
import StrictCards from "../components/StrictCards.jsx";

function Kpi({ cap, value, sub, color }) {
  return (
    <div className="pnl pad" style={{ gap: 4 }}>
      <div className="cap">{cap}</div>
      <div className="kpi-v" style={{ color: color || "var(--tx)" }}>{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

export default function Overview({ code }) {
  const [p, setP] = useState(null);
  const [error, setError] = useState(null);
  const [external, setExternal] = useState(false);   // 当前接的是外部测试模型
  useEffect(() => { api.health().then((h) => setExternal(Boolean(h.llm_external))).catch(() => {}); }, []);

  const [evs, setEvs] = useState(null);
  const load = useCallback(() => {
    setError(null);
    api.project(code).then(setP).catch(setError);
    api.evidences(code).then(setEvs).catch(() => setEvs([]));
  }, [code]);
  useEffect(load, [load]);

  if (!p) return <Loading error={error} />;
  const tier = TIER[p.tier] || TIER.standard;
  const signed = p.deliverables.filter((d) => d.status === "signed").length;
  const strict = p.tier === "strict";
  const count = (k) => (evs || []).filter((e) => e.kind === k).length;
  const pending = p.deliverables.filter((d) => d.status === "review").map((d) => d.code);
  const drafting = p.deliverables.filter((d) => d.status === "drafting").map((d) => d.code);

  return (
    <>
      <TopBar cap={`${p.code} · ${p.industry} · ${tier.label} · 阶段 ${p.phase_name} · 第 ${p.week} / ${p.total_weeks} 周`} title={`项目驾驶舱 · ${p.service}`}>
        <Link className="btnp" to={`/chat/${encodeURIComponent(p.code)}`} style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>打开顾问对话</Link>
      </TopBar>
      {error && <div className="err">{error.message}</div>}
      <div className="kpis">
        <Kpi cap="证据" value={evs ? evs.length : "—"} sub={strict ? "严格档只收摘要卡 S-xx" : `访谈 ${count("interview")} · 资料 ${count("document")} · 观察 ${count("observation")}`} />
        <Kpi cap="交付件已签署" value={`${signed} / ${p.deliverables.length}`}
          sub={[pending.length && `${pending.join("、")} 待签署`, drafting.length && `${drafting.join("、")} 起草中`].filter(Boolean).join(" · ") || "按本项目交付件统计"} />
        <Kpi cap="待你确认" value={p.open_confirmations} sub="Agent 需要你判断的事项" color={p.open_confirmations ? "var(--amber)" : "var(--ok)"} />
        <Kpi cap="数据档位" value={tier.label} sub={p.tier === "strict" ? "资料不出场 · 只收摘要卡" : (external ? "当前为外部测试模型 · 只用虚拟数据" : "模型在公司内部 · 资料不出公司")} color={p.tier === "strict" ? "var(--crim)" : "var(--cyan)"} />
      </div>
      <div className="grid-main">
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <section className="pnl pad" aria-label="阶段">
            <div className="sb"><h2 className="h2">交付阶段</h2><span className="sub">下一个里程碑：{p.next_milestone}</span></div>
            <PhaseBar phase={p.phase} />
            <div className="sb sub">{["启动", "调研", "分析", "设计", "汇报", "验收"].map((n) => <span key={n}>{n}</span>)}</div>
          </section>
          {strict ? <StrictCards code={p.code} onChange={load} /> : <Evidence code={p.code} strict={strict} items={evs} onChange={load} />}
          <section className="pnl pad" aria-label="交付件">
            <h2 className="h2">交付件</h2>
            <ul className="lst" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", columnGap: 16 }}>
              {p.deliverables.map((d) => {
                const st = DELIV_STATUS[d.status] || DELIV_STATUS.not_started;
                return (
                  <li key={d.code}>
                    <Link className="sb" to={`/project/${encodeURIComponent(p.code)}/deliverables/${d.code}`} style={{ color: "var(--tx)", textDecoration: "none" }}>
                      <span><span className="mono sub" style={{ marginRight: 8 }}>{d.code}</span>{d.title}</span>
                      <span className={`chip ${st.cls}`}>{st.label}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </section>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
        <RunPanel code={p.code} strict={strict} onChange={load} />
        <section className="pnl pad" aria-label="待你确认">
          <div className="sb"><h2 className="h2">待你确认</h2><span className="mono" style={{ color: "var(--amber)" }}>{p.confirmations.length}</span></div>
          <ul className="lst">
            {p.confirmations.map((c) => <ConfirmCard key={c.id} c={c} onDone={load} />)}
            {p.confirmations.length === 0 && <li className="sub">没有待确认事项</li>}
          </ul>
        </section>
        <Retention p={p} onChange={load} />
        <Members code={p.code} />
        </div>
      </div>
    </>
  );
}
