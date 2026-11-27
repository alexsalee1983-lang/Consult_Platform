import { useEffect, useState } from "react";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";

// 指标看板 v1（三期 F2①）：10 项精简版指标 + 自动发布率、人工分钟 / 百条，国标内容引用单列。
// 只有数字和口径，不含项目代号和内容；只给项目负责人。
const STATUS = {
  ok: { label: "达标", cls: "c-g" },
  breach: { label: "越红线", cls: "c-r" },
  trend: { label: "看趋势", cls: "c-c" },
  na: { label: "暂无数据", cls: "c-n" },
};
const WINDOWS = [[7, "近 7 天"], [30, "近 30 天"], [90, "近 90 天"]];
const DETAIL = {
  signed: "签署", deliverables: "交付件", eval: "评测", questions: "评测题", edits: "改稿次数",
  all_sections: "全部章节平均", published: "发布", auto: "自动发布", human_reviews_per_100: "人工审核次数 / 百条",
  human_reviews: "人工审核次数",
};

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 1000) / 10}%`);

// 四期 F2③：v4.0 验收口径（萃取跳过率、四格分列），口径见 backend/app/services/indicators_v4.py
function V4({ v4 }) {
  const s = v4.extraction_skip;
  const st = STATUS[s.status] || STATUS.na;
  return (
    <section className="pnl pad" aria-label="v4.0 验收口径">
      <h2 className="h2">v4.0 验收口径</h2>
      <div className="sub">
        经验萃取跳过率 <span className="mono">{pct(s.value)}</span>（已答 {s.answered}、跳过 {s.skipped}，阈值 30%）{" "}
        <span className={`chip ${st.cls}`}>{s.status === "breach" ? "超阈值" : st.label}</span>
      </div>
      <table className="tbl">
        <thead><tr><th>四格</th><th>签署</th><th>编造条款号</th><th>发布</th><th>自动发布率</th></tr></thead>
        <tbody>
          {v4.grids.map((g) => (
            <tr key={g.key}>
              <td>{g.key}</td>
              <td className="mono">{g.signed}</td>
              <td className={`mono ${g.fabricated_clauses ? "c-r" : ""}`}>{g.fabricated_clauses}</td>
              <td className="mono">{g.published}</td>
              <td className="mono">{pct(g.auto_publish_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="sub">编造条款号只算签署交付件，对比评测的编造条号在上表总数里；通用条目会同时算进多格。四格关键章节就绪度见就绪度看板。</div>
    </section>
  );
}

// 四期 F5：自动化验收三条与自适应抽检，口径见 backend/app/services/automation_ledger.py
const SPOT = { tight: "收紧（退回人工）", normal: "常规", relaxed: "放开" };
function Automation({ a }) {
  const rows = [
    [a.soc_manual, a.soc_manual.value ?? "—", `≤ ${a.soc_manual.threshold} 次（SOC 项目 ${a.soc_manual.projects} 个，取最多）`],
    [a.fact_auto, pct(a.fact_auto.value), `≥ ${pct(a.fact_auto.threshold)}（自动 ${a.fact_auto.auto}、人工 ${a.fact_auto.manual}）`],
    [a.spot_error, pct(a.spot_error.value), `≤ ${pct(a.spot_error.threshold)}（抽检 ${a.spot_error.checks} 条）`],
    [a.daily_manual, a.daily_manual.value ?? "—", `≤ ${a.daily_manual.threshold} 件`],
  ];
  return (
    <section className="pnl pad" aria-label="自动化验收">
      <h2 className="h2">自动化验收</h2>
      <table className="tbl">
        <thead><tr><th>指标</th><th>当前</th><th>目标</th><th>状态</th></tr></thead>
        <tbody>
          {rows.map(([m, v, goal]) => {
            const st = STATUS[m.status] || STATUS.na;
            return (
              <tr key={m.label}>
                <td>{m.label}</td>
                <td className="mono">{v}</td>
                <td className="sub">{goal}</td>
                <td><span className={`chip ${st.cls}`}>{st.label}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="sub">
        事实抽检比例 <span className="mono">{pct(a.spot.rate)}</span>：{SPOT[a.spot.state] || a.spot.state}（连续通过 {a.spot.streak} 条）。
        发现差错即每条都抽，连续通过 20 条恢复常规，连续 100 条无差错减半。
      </div>
    </section>
  );
}

function show(m) {
  if (m.value === null || m.value === undefined) return "—";
  if (m.unit === "比例") return `${Math.round(m.value * 1000) / 10}%`;
  return `${m.value} ${m.unit}`;
}

export default function Indicators() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    setData(null);
    setError(null);
    api.indicators(days).then(setData).catch((e) => setError(e.message));
  }, [days]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="项目负责人" title="指标看板" />
      <div className="sub">按时间窗现算的精简版指标（只有数字和口径，不含项目和交付件内容）。红线指标越线标红；暂无数据的写明何时有数。</div>
      <div style={{ display: "flex", gap: 8 }}>
        {WINDOWS.map(([d, label]) => (
          <button key={d} className="btn sm" disabled={d === days} onClick={() => setDays(d)}>{label}</button>
        ))}
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {!data && !error && <section className="pnl pad sub">加载中…</section>}
      {data && (
        <section className="pnl pad">
          <table className="tbl">
            <thead>
              <tr><th>类</th><th>指标</th><th>数值</th><th>目标</th><th>状态</th><th>口径与说明</th></tr>
            </thead>
            <tbody>
              {data.metrics.map((m) => {
                const s = STATUS[m.status] || STATUS.trend;
                const extra = Object.entries(m.detail || {}).map(([k, v]) => `${DETAIL[k] || k} ${v}`).join(" · ");
                return (
                  <tr key={m.key}>
                    <td className="sub">{m.category}</td>
                    <td>{m.name}</td>
                    <td className="mono">{show(m)}</td>
                    <td className="sub">{m.target}</td>
                    <td><span className={`chip ${s.cls}`}>{s.label}</span></td>
                    <td className="sub">{m.basis}{m.note && `。${m.note}`}{extra && <div className="mono">{extra}</div>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
      {data?.v4 && <V4 v4={data.v4} />}
      {data?.automation && <Automation a={data.automation} />}
    </div>
  );
}
