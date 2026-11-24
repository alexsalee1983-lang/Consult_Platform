import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Loading, TopBar } from "../components/common.jsx";
import { useKbMeta } from "../components/kb.jsx";

// 就绪度看板（三期 F2②）：章节 × 槽位；按条目类型的专家分钟 / 条、采纳率、修改率
const STATE = {
  ready: ["就绪", "cov-ok"], candidate: ["候选", "cov-weak"], recheck: ["待复查", "cov-weak"],
  retired: ["已下线", "cov-gap"], missing: ["缺槽", "cov-gap"],
};
const WINDOWS = [30, 90, 180];

function pct(v) {
  return v === null || v === undefined ? "暂无" : `${(v * 100).toFixed(0)}%`;
}

function level(v) {
  if (v === null || v === undefined) return "";
  if (v >= 0.7) return "cov-ok";
  if (v >= 0.4) return "cov-weak";
  return "cov-gap";
}

function Kpi({ cap, value, sub }) {
  return (
    <div className="pnl pad" style={{ gap: 4 }}>
      <div className="cap">{cap}</div>
      <div className="kpi-v">{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

export default function KbReadiness() {
  const meta = useKbMeta();
  const [days, setDays] = useState(90);
  const [r, setR] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setR(null);
    api.kbReadiness(days).then(setR).catch(setError);
  }, [days]);

  if (!r) return <Loading error={error} />;
  const typeName = (t) => (meta?.doc_types?.[t] ? `${meta.doc_types[t]} ${t}` : t);
  const chapters = r.evalsets.flatMap((es) => es.chapters);
  const lowest = chapters.length ? chapters.reduce((a, c) => (c.readiness < a.readiness ? c : a)) : null;
  const ex = r.experts;
  const basis = r.evalsets.map((es) => es.basis_readiness).filter((v) => v !== null);

  return (
    <>
      <TopBar cap="知识 · 按交付章节" title="就绪度看板" />
      <div className="kpis">
        <Kpi cap="章节就绪度（平均）" value={pct(r.readiness)} sub={`${chapters.length} 章 · 12-18 ≥ 40%，12-31 ≥ 70%`} />
        <Kpi cap="最低一章" value={lowest ? pct(lowest.readiness) : "暂无"} sub={lowest ? `${lowest.id} ${lowest.title}` : "没有评测集"} />
        <Kpi cap="依据类槽位" value={pct(basis.length ? Math.min(...basis) : null)} sub="要求 ≥ 95%（多个产品取最低）" />
        <Kpi cap="修改率提醒" value={ex.alerts.length} sub={ex.alerts.length ? "有类型修改率接近 0，建议抽查" : "暂无"} />
      </div>
      {r.errors.length > 0 && (
        <section className="pnl pad" aria-label="评测集问题">
          <h2 className="h2">评测集读不了</h2>
          <ul className="lst">{r.errors.map((e) => <li key={e.file}><span className="mono">{e.file}</span>：{e.error}</li>)}</ul>
        </section>
      )}
      {r.evalsets.map((es) => (
        <section key={es.file} className="pnl pad" aria-label={`章节 × 槽位 ${es.product}`}>
          <div className="sb"><h2 className="h2">章节 × 槽位 · {es.product}</h2>
            <span className="row sub" style={{ gap: 8 }}>
              {Object.entries(STATE).map(([k, [label, cls]]) => <span key={k} className={`${cls} legend`}>{label}</span>)}
            </span>
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="tbl cov">
              <thead><tr><th>章节</th><th>就绪度</th><th>槽位（应命中知识）</th></tr></thead>
              <tbody>
                {es.chapters.map((c) => (
                  <tr key={c.id}>
                    <td>{c.title} <span className="mono sub">{c.id}</span></td>
                    <td className={`mono ${level(c.readiness)}`}>{pct(c.readiness)} <span className="sub">{c.ready}/{c.slots.length}</span></td>
                    <td>
                      <span className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        {c.slots.map((s) => (
                          <span key={s.key} className={`mono ${STATE[s.state][1]} legend`} title={`${STATE[s.state][0]}${s.doc_type ? ` · ${typeName(s.doc_type)}` : ""}`}>
                            {s.key}{s.state !== "ready" ? ` · ${STATE[s.state][0]}` : ""}
                          </span>
                        ))}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="sub">评测集 {es.file}。就绪 = 已发布、未下线、不在待复查；候选（草稿、待审）不算。槽位规格（C10）出来前，一条应命中知识算一个槽位。</div>
        </section>
      ))}
      <section className="pnl pad" aria-label="专家指标">
        <div className="sb"><h2 className="h2">按类型的专家指标 · 近 {days} 天</h2>
          <span className="row" style={{ gap: 6 }}>
            {WINDOWS.map((d) => (
              <button key={d} type="button" className={d === days ? "btnp" : "btn"} onClick={() => setDays(d)}>{d} 天</button>
            ))}
          </span>
        </div>
        {ex.alerts.length > 0 && <ul className="lst">{ex.alerts.map((a) => <li key={a} style={{ color: "var(--amber)" }}>{a}</li>)}</ul>}
        <table className="tbl">
          <thead><tr><th>条目类型</th><th>人工审核</th><th>采纳</th><th>采纳率</th><th>专家分钟 / 条</th><th>修改率</th></tr></thead>
          <tbody>
            {ex.by_type.map((t) => (
              <tr key={t.doc_type}>
                <td>{typeName(t.doc_type)}</td>
                <td className="mono">{t.reviews}</td>
                <td className="mono">{t.approved}</td>
                <td className="mono">{pct(t.adopt_rate)}</td>
                <td className="mono">{t.minutes_per_item ?? "暂无"}</td>
                <td className="mono" style={t.low_edit ? { color: "var(--amber)" } : undefined}>{pct(t.edit_rate)}{t.edit_samples ? <span className="sub"> （{t.edit_samples} 条）</span> : null}</td>
              </tr>
            ))}
            {ex.by_type.length === 0 && <tr><td colSpan={6} className="sub">这段时间没有人工内容审核</td></tr>}
          </tbody>
        </table>
        <div className="sub">专家分钟 / 条和修改率由公司做法确认队列（B17）记录，之前显示“暂无”。有修改记录的采纳 ≥ {ex.edit_min_reviews} 条且修改率 ≤ {pct(ex.edit_low_rate)} 时提醒抽查。</div>
      </section>
    </>
  );
}
