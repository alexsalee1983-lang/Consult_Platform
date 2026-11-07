import { useCallback, useEffect, useState } from "react";
import { api, DELIV_STATUS, KIND_CLS, TIER } from "../api.js";
import { Loading, PhaseBar, TopBar } from "../components/common.jsx";

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
  const [busy, setBusy] = useState(null);

  const load = useCallback(() => {
    setError(null);
    api.project(code).then(setP).catch(setError);
  }, [code]);
  useEffect(load, [load]);

  async function resolve(c) {
    setBusy(c.id);
    try {
      await api.resolve(c.id, "已在驾驶舱确认");
      load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  }

  if (!p) return <Loading error={error} />;
  const tier = TIER[p.tier] || TIER.standard;
  const signed = p.deliverables.filter((d) => d.status === "signed").length;

  return (
    <>
      <TopBar cap={`${p.code} · ${p.industry} · ${tier.label} · 阶段 ${p.phase_name} · 第 ${p.week} / ${p.total_weeks} 周`} title={`项目驾驶舱 · ${p.service}`}>
        <a className="btnp" href={`#/chat/${p.code}`} style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>打开顾问对话</a>
      </TopBar>
      {error && <div className="err">{error.message}</div>}
      <div className="kpis">
        <Kpi cap="态势评分" value={p.posture_score ?? "—"} sub="满分 100" color="var(--amber)" />
        <Kpi cap="交付件已签署" value={`${signed} / ${p.deliverables.length}`} sub="按 D1～D8 统计" />
        <Kpi cap="待你确认" value={p.open_confirmations} sub="Agent 需要你判断的事项" color={p.open_confirmations ? "var(--amber)" : "var(--ok)"} />
        <Kpi cap="数据档位" value={tier.label} sub={p.tier === "strict" ? "资料不出场 · 只收摘要卡" : "模型在公司内部 · 资料不出公司"} color={p.tier === "strict" ? "var(--crim)" : "var(--cyan)"} />
      </div>
      <div className="grid-main">
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <section className="pnl pad" aria-label="阶段">
            <div className="sb"><h2 className="h2">交付阶段</h2><span className="sub">下一个里程碑：{p.next_milestone}</span></div>
            <PhaseBar phase={p.phase} />
            <div className="sb sub">{["启动", "调研", "分析", "设计", "汇报", "验收"].map((n) => <span key={n}>{n}</span>)}</div>
          </section>
          <section className="pnl pad" aria-label="交付件">
            <h2 className="h2">交付件</h2>
            <ul className="lst" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", columnGap: 16 }}>
              {p.deliverables.map((d) => {
                const st = DELIV_STATUS[d.status] || DELIV_STATUS.not_started;
                return (
                  <li key={d.code} className="sb">
                    <span><span className="mono sub" style={{ marginRight: 8 }}>{d.code}</span>{d.title}</span>
                    <span className={`chip ${st.cls}`}>{st.label}</span>
                  </li>
                );
              })}
            </ul>
          </section>
        </div>
        <section className="pnl pad" aria-label="待你确认">
          <div className="sb"><h2 className="h2">待你确认</h2><span className="mono" style={{ color: "var(--amber)" }}>{p.confirmations.length}</span></div>
          <ul className="lst">
            {p.confirmations.map((c) => (
              <li key={c.id} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div className="sb"><span className={`chip ${KIND_CLS[c.kind] || "c-n"}`}>{c.kind}</span><span className="sub">{c.agent}</span></div>
                <div>{c.title}</div>
                <div><button className="btn" disabled={busy === c.id} onClick={() => resolve(c)}>{busy === c.id ? "处理中…" : "标记已处理"}</button></div>
              </li>
            ))}
            {p.confirmations.length === 0 && <li className="sub">没有待确认事项</li>}
          </ul>
        </section>
      </div>
    </>
  );
}
