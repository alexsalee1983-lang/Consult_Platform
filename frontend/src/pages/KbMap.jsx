import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";
import { Loading, TopBar } from "../components/common.jsx";
import { useKbMeta } from "../components/kb.jsx";

const ROW_STATUS = { draft: "配置草稿", unconfigured: "未配置" };
const KIND_LABEL = { product: "服务产品", general: "通用知识", module: "模块（按标签）" };
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
  const stages = s.stages || meta.stages;
  const matrix = s.matrix || [];
  const sources = Object.entries(s.by_source).map(([k, n]) => `${SOURCE_LABEL[k] || k} ${n}`).join(" · ") || "暂无";

  return (
    <>
      <TopBar cap="知识 · 按服务产品" title="知识地图与健康" />
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
                {matrix.map((r, i) => (
                  <tr key={`${r.kind}-${r.code}`}>
                    <td>
                      {(i === 0 || matrix[i - 1].kind !== r.kind) && r.kind !== "product" && <div className="cap">{KIND_LABEL[r.kind]}</div>}
                      {r.name} <span className="mono sub">{r.code}</span>
                      {ROW_STATUS[r.status] && <span className="chip c-a" style={{ marginLeft: 6 }}>{ROW_STATUS[r.status]}</span>}
                    </td>
                    {stages.map((st) => {
                      const n = r.cells[st] || 0;
                      return <td key={st} className={`mono ${level(n)}`}>{n}</td>;
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="sub">按已发布、未下线的现行版本统计。“通用”阶段和通用知识（GEN）检索时会自动带上。服务产品按产品配置列出，新产品加了配置就出现；模块行统计带模块检索标签的条目，不分产品。</div>
          <h2 className="h2" style={{ marginTop: 14 }}>贡献统计</h2>
          <table className="tbl">
            <thead><tr><th>作者 / 审核人</th><th>现行条目</th><th>30 天新发布</th><th>30 天审核</th><th>来源构成</th></tr></thead>
            <tbody>
              {(s.contributors || []).map((c) => (
                <tr key={c.username}>
                  <td>{c.display_name}</td>
                  <td className="mono">{c.published}</td>
                  <td className="mono">{c.published_30d}</td>
                  <td className="mono">{c.reviews_30d}</td>
                  <td className="sub">{Object.entries(c.by_source).map(([k, n]) => `${SOURCE_LABEL[k] || k} ${n}`).join(" · ") || "—"}</td>
                </tr>
              ))}
              {(s.contributors || []).length === 0 && <tr><td colSpan={5} className="sub">还没有发布或审核记录</td></tr>}
            </tbody>
          </table>
        </section>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <section className="pnl pad" aria-label="缺口 TOP">
            <div className="sb"><h2 className="h2">缺口 TOP {(s.gaps || []).length}</h2>
              <span className="sub">反复零命中的问题 <span className="mono" style={{ color: "var(--amber)" }}>{s.repeated_zero_hit_questions_30d || 0}</span></span></div>
            <ul className="lst">
              {(s.gaps || []).map((g) => (
                <li key={`${g.product}-${g.stage}`} className="sb">
                  <span>{g.product_name} · {g.stage}</span>
                  <span className="row" style={{ gap: 6 }}>
                    <span className={`mono ${level(g.items)} legend`}>{g.items} 条</span>
                    <span className="mono sub">零命中 {g.zero_hits_30d}</span>
                  </span>
                </li>
              ))}
              {(s.gaps || []).length === 0 && <li className="sub">各产品、各阶段都已充足</li>}
            </ul>
            <div className="sub">按近 30 天检索时所选产品、阶段的零命中次数和条目数排序。检索词原文按数据保护要求不记录，看不到具体问题。</div>
          </section>
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
          <FeedbackHealth f={s.feedback} />
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

// 四期 A17：顾问反馈进知识健康——近 30 天三键计数、因反馈待复查的条目、被标“过时 / 不对”最多的条目（不含附言原文）
function FeedbackHealth({ f }) {
  if (!f) return null;
  const c = f.counts_30d || {};
  return (
    <section className="pnl pad" aria-label="顾问反馈">
      <div className="sb"><h2 className="h2">顾问反馈 · 近 {f.days} 天</h2>
        <span className="sub">因反馈待复查 <span className="mono" style={{ color: f.flagged_recheck ? "var(--amber)" : "var(--ok)" }}>{f.flagged_recheck}</span></span></div>
      <div className="sb"><span className="sub">有用</span><span className="mono" style={{ color: "var(--ok)" }}>{c.useful || 0}</span></div>
      <div className="sb"><span className="sub">过时</span><span className="mono" style={{ color: "var(--amber)" }}>{c.outdated || 0}</span></div>
      <div className="sb"><span className="sub">不对</span><span className="mono" style={{ color: "var(--crim)" }}>{c.wrong || 0}</span></div>
      <ul className="lst">
        {(f.top_flagged || []).map((t) => (
          <li key={t.doc_id} className="sb">
            <span><span className="mono sub">{t.doc_id}</span> {t.title}</span>
            <span className="mono sub">过时 {t.outdated} · 不对 {t.wrong}{t.recheck_state === "pending" ? " · 待复查" : ""}</span>
          </li>
        ))}
        {(f.top_flagged || []).length === 0 && <li className="sub">近期没有被标过时或不对的条目</li>}
      </ul>
      <div className="sub">被标“过时 / 不对”的条目进待复查（复查通过前不计入就绪度），同时作为使用信号进缺口池。附言原文不在这里显示。</div>
    </section>
  );
}
