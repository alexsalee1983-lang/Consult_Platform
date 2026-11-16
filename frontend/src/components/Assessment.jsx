import { useEffect, useState } from "react";
import { api } from "../api.js";

// 成熟度与符合性（二期 A10）：成熟度初判（现状 → 目标）、按能力域的符合性条、3×3 风险矩阵。
// 数据来自最近一次差距分析；D4 签署后分数以 D4 的“成熟度初判”表为准。只是初判，不是测评或等级结论。
const KINDS = [
  ["met", "符合", "var(--ok)"],
  ["partial", "部分符合", "var(--amber)"],
  ["unmet", "不符合", "var(--crim)"],
  ["insufficient", "证据不足", "var(--tx3)"],
];
const LV = ["高", "中", "低"];
const CONCL = { unmet: "不符合", partial: "部分符合", insufficient: "证据不足" };

function Scores({ scores }) {
  return (
    <ul className="lst">
      {scores.map((s) => (
        <li key={s.domain} style={{ display: "grid", gap: 4 }}>
          <div className="sb"><span>{s.domain}</span><span className="mono">{s.current.toFixed(1)} → {s.target.toFixed(1)}</span></div>
          <div style={{ position: "relative", height: 8, background: "var(--s3)", borderRadius: 4 }}>
            <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${s.target * 20}%`, background: "var(--cyan-d)", borderRadius: 4 }} />
            <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${s.current * 20}%`, background: "var(--cyan)", borderRadius: 4 }} />
          </div>
          {s.basis && <span className="sub">依据：{s.basis}</span>}
        </li>
      ))}
    </ul>
  );
}

function Bars({ domains, findings }) {
  const [open, setOpen] = useState(null);
  return (
    <ul className="lst">
      {domains.map((d) => {
        const total = KINDS.reduce((n, [k]) => n + d.counts[k], 0) || 1;
        const gaps = findings.filter((f) => f.domain === d.domain);
        return (
          <li key={d.domain} style={{ display: "grid", gap: 4 }}>
            <button className="sb" style={{ border: 0, background: "transparent", color: "var(--tx)", padding: 0, textAlign: "left" }}
              onClick={() => setOpen(open === d.domain ? null : d.domain)} aria-expanded={open === d.domain}>
              <span>{d.domain}</span>
              <span className="sub">{KINDS.map(([k, n]) => d.counts[k] ? `${n} ${d.counts[k]}` : "").filter(Boolean).join(" · ")}</span>
            </button>
            <div style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden", background: "var(--s3)" }}>
              {KINDS.map(([k, n, c]) => d.counts[k] ? <div key={k} title={`${n} ${d.counts[k]}`} style={{ width: `${d.counts[k] * 100 / total}%`, background: c }} /> : null)}
            </div>
            {open === d.domain && (
              <ul className="lst sub">
                {gaps.map((f) => <li key={f.code + f.item}><span className="mono">{f.code}</span> {f.item} · {CONCL[f.conclusion]} · 影响{f.impact} 可能性{f.likelihood}</li>)}
                {gaps.length === 0 && <li>本域没有标了影响和可能性的差距</li>}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Matrix({ findings }) {
  const cell = (i, l) => findings.filter((f) => f.impact === i && f.likelihood === l);
  const tone = (a, b) => (a + b <= 1 ? "var(--crim-d)" : a + b <= 2 ? "var(--amber-d)" : "var(--s2)");
  return (
    <div style={{ display: "grid", gridTemplateColumns: "auto repeat(3, 1fr)", gap: 4, alignItems: "stretch" }}>
      <span className="sub" />
      {LV.map((l) => <span key={l} className="sub" style={{ textAlign: "center" }}>可能性{l}</span>)}
      {LV.map((i, a) => [
        <span key={i} className="sub" style={{ alignSelf: "center" }}>影响{i}</span>,
        ...LV.map((l, b) => {
          const xs = cell(i, l);
          return (
            <div key={i + l} title={xs.map((f) => `${f.code} ${f.item}`).join("\n")}
              style={{ background: tone(a, b), border: "1px solid var(--ln)", borderRadius: 4, padding: 6, minHeight: 36, textAlign: "center" }}>
              <span className="mono">{xs.length || ""}</span>
            </div>
          );
        }),
      ])}
    </div>
  );
}

export default function Assessment({ code }) {
  const [a, setA] = useState(null);
  const [tab, setTab] = useState("scores");
  useEffect(() => { api.assessment(code).then(setA).catch(() => setA(null)); }, [code]);
  if (!a || (!a.source && a.scores.length === 0)) {
    return (
      <section className="pnl pad" aria-label="成熟度与符合性">
        <h2 className="h2">成熟度与符合性</h2>
        <span className="sub">交付链跑完差距分析后显示</span>
      </section>
    );
  }
  const tabs = [a.scores_enabled && ["scores", a.scores_label], ["bars", "符合性"], ["risk", "风险矩阵"]].filter(Boolean);
  const cur = tabs.some(([k]) => k === tab) ? tab : tabs[0][0];
  return (
    <section className="pnl pad" aria-label="成熟度与符合性">
      <div className="sb">
        <h2 className="h2">成熟度与符合性</h2>
        <span className="sub">{a.scores_source === "D4" ? "分数取自已签署 D4" : "取自最近一次差距分析"} · 初判，非测评结论</span>
      </div>
      <div className="tabs">
        {tabs.map(([k, n]) => <button key={k} className={cur === k ? "on" : ""} onClick={() => setTab(k)}>{n}</button>)}
      </div>
      {cur === "scores" && (a.scores.length ? <Scores scores={a.scores} /> : <span className="sub">差距分析里没有{a.scores_label}表</span>)}
      {cur === "bars" && (a.domains.length ? <Bars domains={a.domains} findings={a.findings} /> : <span className="sub">差距分析表里没有能力域列</span>)}
      {cur === "risk" && <Matrix findings={a.findings} />}
    </section>
  );
}
