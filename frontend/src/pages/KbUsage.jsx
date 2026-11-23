// 知识使用统计（三期 B29 最小版）：每条知识的引用次数、采纳率和“有用 / 过时 / 不对”计数。只给知识专家、知识管理员。
import { useEffect, useState } from "react";
import { api } from "../api.js";

const pct = (x) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
const RECHECK = { pending: "待复查", failed: "复查不通过", passed: "复查通过" };

export default function KbUsage() {
  const [days, setDays] = useState(90);
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
    api.kbUsage(days).then(setD).catch((e) => setError(e.message));
  }, [days]);
  return (
    <section className="pnl" aria-label="知识使用统计">
      <div className="sb">
        <h1 className="h1">知识使用统计</h1>
        <select className="sm" aria-label="统计区间" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[30, 90, 180, 365].map((n) => <option key={n} value={n}>近 {n} 天</option>)}
        </select>
      </div>
      <div className="sub">引用次数 = Agent 写入章节里核对得上的引用 + 对话回复附的参考知识；采纳率 = Agent 引用它的章节里顾问没删掉的比例。
        被标“过时 / 不对”的条目进待复查，复查通过前不计入就绪度。</div>
      {error && <div className="err">{error}</div>}
      {d && (
        <>
          <div className="row" style={{ gap: 24, margin: "12px 0" }}>
            <span>引用次数 <b className="mono">{d.totals.citations}</b></span>
            <span>采纳率 <b className="mono">{pct(d.totals.adoption_rate)}</b></span>
            <span>有用 / 过时 / 不对 <b className="mono">{d.totals.feedback.useful} / {d.totals.feedback.outdated} / {d.totals.feedback.wrong}</b></span>
          </div>
          {d.items.length === 0 ? <div className="sub">区间内没有引用和反馈。</div> : (
            <table className="tbl">
              <thead><tr><th>编号</th><th>标题</th><th>引用（章节 / 对话）</th><th>采纳率</th><th>有用</th><th>过时</th><th>不对</th><th>状态</th></tr></thead>
              <tbody>
                {d.items.map((x) => (
                  <tr key={x.doc_id}>
                    <td className="mono">{x.doc_id}</td>
                    <td>{x.title}</td>
                    <td className="mono">{x.citations}（{x.section_citations} / {x.chat_citations}）</td>
                    <td className="mono">{pct(x.adoption_rate)}</td>
                    <td className="mono">{x.feedback.useful}</td>
                    <td className="mono" style={x.feedback.outdated ? { color: "var(--amber)" } : undefined}>{x.feedback.outdated}</td>
                    <td className="mono" style={x.feedback.wrong ? { color: "var(--crim)" } : undefined}>{x.feedback.wrong}</td>
                    <td>{x.retired ? "已下线" : RECHECK[x.recheck_state] || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
