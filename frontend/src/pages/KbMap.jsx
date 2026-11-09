import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";
import { Loading, TopBar } from "../components/common.jsx";
import { useKbMeta } from "../components/kb.jsx";

const SOURCE_LABEL = { public: "公开资料", expert: "专家萃取", project: "项目沉淀", internal: "内部制度改造" };

function Kpi({ cap, value, sub, color }) {
  return (
    <div className="pnl pad" style={{ gap: 4 }}>
      <div className="cap">{cap}</div>
      <div className="kpi-v" style={{ color: color || "var(--tx)" }}>{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

// 覆盖矩阵着色：充足 ≥ 8 / 薄弱 3–7 / 缺口 0–2（设计稿 06）
function level(n) {
  if (n >= 8) return "cov-ok";
  if (n >= 3) return "cov-weak";
  return "cov-gap";
}

export default function KbMap() {
  const meta = useKbMeta();
  const [s, setS] = useState(null);
  const [due, setDue] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.kbStats().then(setS).catch(setError);
    api.kbDue(30).then(setDue).catch(() => setDue([]));
  }, []);

  if (!s || !meta) return <Loading error={error} />;
  const stages = meta.stages;
  const products = Object.keys(meta.products);
  const sources = Object.entries(s.by_source).map(([k, n]) => `${SOURCE_LABEL[k] || k} ${n}`).join(" · ") || "暂无";

  return (
    <>
      <TopBar cap="知识 · 一期产品" title="知识地图与健康" />
      <div className="kpis">
        <Kpi cap="已发布条目" value={s.published} sub={sources} />
        <Kpi cap="待审 / 草稿" value={`${s.in_review} / ${s.drafts}`} sub="在审批队列处理" color="var(--amber)" />
        <Kpi cap="待复审" value={s.review_due_30d} sub={`已到期 ${s.review_due} 条 · 30 天内到期`} color={s.review_due ? "var(--crim)" : "var(--tx)"} />
        <Kpi cap="30 天零命中检索" value={s.zero_hit_searches_30d} sub={`共 ${s.searches_30d} 次检索 · 知识库答不上的问题`} color={s.zero_hit_searches_30d ? "var(--amber)" : "var(--ok)"} />
      </div>
      <div className="grid-main">
        <section className="pnl pad" aria-label="覆盖矩阵">
          <div className="sb"><h2 className="h2">覆盖矩阵 · 服务产品 × 交付阶段</h2>
            <span className="row sub" style={{ gap: 8 }}><span className="cov-ok legend">充足 ≥ 8</span><span className="cov-weak legend">薄弱 3–7</span><span className="cov-gap legend">缺口 0–2</span></span>
          </div>
          <div style={{ overflowX: "auto" }}>
            <table className="tbl cov">
              <thead><tr><th>服务产品</th>{stages.map((st) => <th key={st}>{st}</th>)}</tr></thead>
              <tbody>
                {products.map((p) => (
                  <tr key={p}>
                    <td>{meta.products[p]}</td>
                    {stages.map((st) => {
                      const n = s.coverage[p]?.[st] || 0;
                      return <td key={st} className={`mono ${level(n)}`}>{n}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="sub">按已发布、未下线的现行版本统计。“通用”阶段和通用知识（GEN）检索时会自动带上。</div>
        </section>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <section className="pnl pad" aria-label="待复审">
            <div className="sb"><h2 className="h2">待复审</h2><span className="mono" style={{ color: "var(--amber)" }}>{due.length}</span></div>
            <ul className="lst">
              {due.map((v) => (
                <li key={v.id} className="sb">
                  <Link to={`/kb/review?version=${v.id}`} style={{ textDecoration: "none", color: "var(--tx)" }}>
                    <span className="mono sub">{v.doc_id}</span> {v.title}
                  </Link>
                  <span className={`chip ${v.review_date < new Date().toISOString().slice(0, 10) ? "c-r" : "c-a"}`}>{v.review_date}</span>
                </li>
              ))}
              {due.length === 0 && <li className="sub">30 天内没有到期的条目</li>}
            </ul>
          </section>
          <section className="pnl pad" aria-label="本月知识运营">
            <h2 className="h2">本月知识运营</h2>
            <div className="sb"><span className="sub">新发布</span><span className="mono">{s.published_30d}</span></div>
            <div className="sb"><span className="sub">检索次数</span><span className="mono">{s.searches_30d}</span></div>
            <div className="sb"><span className="sub">检索命中率</span>
              <span className="mono">{s.searches_30d ? `${Math.round(100 * (1 - s.zero_hit_searches_30d / s.searches_30d))}%` : "—"}</span></div>
            <div className="sb"><span className="sub">已下线</span><span className="mono">{s.retired}</span></div>
            <div className="sub">“顾问问过、答不上的问题”需要记录检索词原文，按数据保护要求不记录，这里只统计次数。</div>
          </section>
        </div>
      </div>
    </>
  );
}
