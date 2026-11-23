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
    </div>
  );
}
