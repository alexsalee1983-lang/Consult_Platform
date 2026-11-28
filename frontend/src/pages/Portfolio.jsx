import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, DELETION_STATUS, RETENTION, TIER } from "../api.js";
import { TopBar } from "../components/common.jsx";

const DSTAT = [["signed", "已签署"], ["review", "待签署"], ["drafting", "编写中"], ["not_started", "未开始"]];

// 咨询业务负责人项目总览（决策记录 2026-11-15 方案 A）：只看全部项目的元数据，不看证据、交付件正文和对话；
// 只有本人是成员的项目可以进入驾驶舱
export default function Portfolio() {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { api.portfolio().then(setRows).catch((e) => setError(e.message)); }, []);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="咨询业务负责人" title="项目总览" />
      <div className="sub">全部项目的进度、交付件状态、保留期和删除情况（只有元数据）。项目内容只对项目成员开放；需要查看时请项目经理把你加入项目。</div>
      {error && <div className="err" role="alert">{error}</div>}
      {!rows && !error && <section className="pnl pad sub">加载中…</section>}
      {rows && !rows.length && <section className="pnl pad sub">还没有项目。</section>}
      {rows && rows.length > 0 && (
        <section className="pnl pad">
          <table className="tbl">
            <thead>
              <tr><th>项目</th><th>产品</th><th>档位</th><th>阶段</th><th>项目经理</th><th>成员</th><th>交付件</th><th>保留期</th><th>删除</th><th /></tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const t = TIER[p.tier] || TIER.standard;
                const r = RETENTION[p.retention_state] || RETENTION.active;
                const d = p.deletion_status && DELETION_STATUS[p.deletion_status];
                return (
                  <tr key={p.code}>
                    <td className="mono">{p.code}{p.industry && <span className="sub"> · {p.industry}</span>}</td>
                    <td>{p.product_name}</td>
                    <td><span className={`chip ${t.cls}`}>{t.label}</span></td>
                    <td>{p.deleted ? "—" : `${p.phase_name || ""} 第 ${p.week}/${p.total_weeks} 周`}</td>
                    <td>{p.managers.join("、") || <span className="sub">未指定</span>}</td>
                    <td className="mono">{p.member_count}</td>
                    <td className="sub">{DSTAT.filter(([k]) => p.deliverables[k]).map(([k, n]) => `${n} ${p.deliverables[k]}`).join(" · ") || "—"}</td>
                    <td><span className={`chip ${r.cls}`}>{r.label}</span>{p.deletion_due_on && <span className="sub mono"> {p.deletion_due_on}</span>}</td>
                    <td>{d ? <span className={`chip ${d.cls}`}>{d.label}</span> : <span className="sub">—</span>}</td>
                    <td>{p.is_member && !p.deleted ? <Link className="btn sm" to={`/project/${p.code}`}>进入</Link> : <span className="sub">非成员</span>}</td>
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
