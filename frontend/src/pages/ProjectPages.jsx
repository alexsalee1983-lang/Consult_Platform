// 项目内各页（四期 A37，docs/交付链输入输出方案.md 8.1）：项目栏（左）+ 页面（右）。项目栏放全景、“输入 → 需求 → 分析 → 输出”
// 竖向交付链、D1～D8、调研助手、设置，取代 A25② 的横向五导航；各页复用已有面板组件。
// 条目上的“⌖ 全景”回全景并带 focus（节点 id 与 GET .../panorama 一致）。调研助手全屏单开（现场用），交付件页挂在“输出”下。
import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, Outlet, useLocation, useParams, useSearchParams } from "react-router";
import { api, DELIV_STATUS } from "../api.js";
import { useAuth } from "../auth.jsx";
import Assessment from "../components/Assessment.jsx";
import { Loading, TopBar } from "../components/common.jsx";
import { groupCards } from "../components/CardEvidence.jsx";
import Closeout from "../components/Closeout.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import Evidence from "../components/Evidence.jsx";
import Intake from "../components/Intake.jsx";
import Members from "../components/Members.jsx";
import ProjectRail from "../components/ProjectRail.jsx";
import Retention from "../components/Retention.jsx";
import RunPanel from "../components/RunPanel.jsx";
import StrictCards from "../components/StrictCards.jsx";
import Survey from "../components/Survey.jsx";
import { useAgentView } from "../components/AgentActivity.jsx";

const base = (code) => `/project/${encodeURIComponent(code)}`;

export function LocateLink({ code, node }) {
  return <Link className="locate" to={`${base(code)}?focus=${encodeURIComponent(node)}`} title="回全景并高亮这个节点">⌖ 全景</Link>;
}

// 项目栏 + 页面；交付件页（/deliverables/Dx）面包屑显示 项目 / 输出 / Dx
export function ProjectLayout() {
  const { code } = useParams();
  const { pathname } = useLocation();
  const dcode = pathname.match(/\/deliverables\/([^/]+)/)?.[1];
  // 负责人 11-28：立项后第一件事是配人。项目经理进项目时若项目里只有他一人，各页顶部都提示（谁立的项都一样）
  const { user } = useAuth();
  const [alone, setAlone] = useState(false);
  useEffect(() => {
    let live = true;
    setAlone(false);
    api.members(code).then((ms) => {
      if (live) setAlone(ms.length === 1 && ms[0].username === user?.username && ms[0].project_role === "manager");
    }).catch(() => {});
    return () => { live = false; };
  }, [code, user?.username]);
  const onSettings = pathname.endsWith("/settings");
  return (
    <div className="proj-layout">
      <ProjectRail code={code} />
      <div className="content">
        {alone && !onSettings && (
          <div className="note-amber" role="status" aria-label="待配项目成员">
            这个项目还没有其他成员，<Link to={`${base(code)}/settings?new=1`}>先去设置里勾选项目成员</Link>，再开始启动包。
          </div>
        )}
        {dcode && <div className="sub">{code} / <Link to={`${base(code)}/output`}>输出</Link> / {decodeURIComponent(dcode)}</div>}
        <Outlet />
      </div>
    </div>
  );
}

function useProject(code) {
  const [p, setP] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(() => { api.project(code).then(setP).catch(setError); }, [code]);
  useEffect(load, [load]);
  return { p, error, load };
}

const head = (p, title) => <TopBar cap={`${p.code} · ${p.industry} · 阶段 ${p.phase_name}`} title={`${title} · ${p.service}`} />;

export function InputPage({ code }) {
  const { p, error, load } = useProject(code);
  const [params] = useSearchParams();
  const step = params.get("new") ? 2 : 0;          // 新建项目三步：第 1 步在立项弹窗，第 2、3 步在这里
  const [evs, setEvs] = useState(null);
  const loadEvs = useCallback(() => { api.evidences(code).then(setEvs).catch(() => setEvs([])); }, [code]);
  useEffect(loadEvs, [loadEvs]);
  if (!p) return <Loading error={error} />;
  const strict = p.tier === "strict";
  return (
    <>
      {head(p, "输入")}
      <Intake code={p.code} step={step} />
      <section className="pnl pad" aria-label="调研">
        <div className="sb"><h2 className="h2">调研</h2><LocateLink code={p.code} node="client:evidence" /></div>
        <div className="sub">按受访角色生成调研包、现场填答与回传在调研助手里做（全屏，现场用）。</div>
        <Link className="btnp" to={`${base(p.code)}/survey`} style={{ alignSelf: "flex-start", display: "inline-flex", alignItems: "center", textDecoration: "none" }}>打开调研助手</Link>
      </section>
      {strict ? <StrictCards code={p.code} onChange={load} />
        : <Evidence code={p.code} strict={strict} items={evs} onChange={() => { load(); loadEvs(); }} />}
    </>
  );
}

export function AnalysisPage({ code }) {
  const { p, error, load } = useProject(code);
  // 四期 A32②：证据原文并排与同源合卡（只管展示，处理仍逐张）
  const [ev, setEv] = useState({ groups: [], evidence: {} });
  const loadEv = useCallback(() => { api.cardEvidence(code).then(setEv).catch(() => setEv({ groups: [], evidence: {} })); }, [code]);
  useEffect(loadEv, [loadEv]);
  const done = async () => { await load(); loadEv(); };
  if (!p) return <Loading error={error} />;
  const card = (c) => (
    <Fragment key={c.id}>
      <li style={{ textAlign: "right" }}><LocateLink code={p.code} node={`card:${c.id}`} /></li>
      <ConfirmCard c={c} evidence={ev.evidence[String(c.id)]} onDone={done} />
    </Fragment>
  );
  return (
    <>
      {head(p, "分析")}
      <section className="pnl pad" aria-label="待你确认">
        <div className="sb"><h2 className="h2">待你确认 · 检查点</h2><span className="mono" style={{ color: "var(--amber)" }}>{p.confirmations.length}</span></div>
        <ul className="lst">
          {groupCards(p.confirmations, ev.groups).map((g) => (g.cards.length > 1 ? (
            <li key={`g-${g.cards[0].id}`} className="card card-group pnl pad" aria-label={`同源确认卡：${g.source}`}>
              <div className="sb"><span>同一份资料 <span className="mono" style={{ color: "var(--cyan)" }}>{g.source}</span> {g.title}</span>
                <span className="sub">{g.cards.length} 张，逐张处理</span></div>
              <ul className="lst">{g.cards.map(card)}</ul>
            </li>
          ) : card(g.cards[0])))}
          {p.confirmations.length === 0 && <li className="sub">没有待确认事项</li>}
        </ul>
      </section>
      <Assessment code={p.code} />
    </>
  );
}

// 需求页（A37 新增）：客户和项目组“要什么、定了什么”。每条标来源与生效状态；所有入口进来的要求先回显确认再生效。
// 定制项沿用 A30 接口；修订指令、决定记录、变更单随 A33～A36 接口接入，先占位。
const CUST_CLS = { active: "c-g", pending_pm: "c-a", proposed: "c-a", unacceptable: "c-r" };
const REQ_TABS = [["cust", "定制项"], ["rev", "修订指令"], ["dec", "决定记录"], ["chg", "变更单"]];

export function RequirementsPage({ code }) {
  const { p, error } = useProject(code);
  const [cust, setCust] = useState(null);
  const [tab, setTab] = useState("cust");
  useEffect(() => { api.customizations(code).then((d) => setCust(d.items)).catch(() => setCust([])); }, [code]);
  if (!p) return <Loading error={error} />;
  const n = { cust: cust ? cust.length : "—", rev: 0, dec: 0, chg: 0 };
  return (
    <>
      {head(p, "需求")}
      <div className="sub">客户和项目组“要什么、定了什么”都在这里；每条标来源和生效状态，所有入口进来的要求先回显确认再生效。</div>
      <nav className="tabs" aria-label="需求分类">
        {REQ_TABS.map(([k, t]) => (
          <button key={k} type="button" className={tab === k ? "on" : undefined} aria-pressed={tab === k} onClick={() => setTab(k)}>
            {t} <span className="mono">{n[k]}</span>
          </button>
        ))}
      </nav>
      {tab === "cust" ? (
        <section className="pnl pad" aria-label="定制项">
          <table className="tbl">
            <thead><tr><th>定制项</th><th>入口</th><th>类型</th><th>状态</th><th>交付件</th></tr></thead>
            <tbody>
              {(cust || []).map((i) => (
                <tr key={i.id}>
                  <td><span className="mono sub">{i.code}</span> {i.text}{i.reason && <div className="sub">{i.reason}</div>}</td>
                  <td>{i.source_name}</td>
                  <td>{i.kind_name}</td>
                  <td><span className={`chip ${CUST_CLS[i.status] || "c-n"}`}>{i.superseded ? "已被覆盖" : i.status_name}</span></td>
                  <td>{i.deliverable ? `${i.deliverable}${i.section ? ` 第 ${i.section} 章` : ""}` : "全部"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cust && cust.length === 0 && <div className="sub">还没有定制项。客户要求可在 D1 客户要求栏、顾问对话或改稿时提出。</div>}
        </section>
      ) : (
        <section className="pnl pad" aria-label={REQ_TABS.find(([k]) => k === tab)[1]}>
          <div className="sub">暂无。{{ rev: "签署人退回、客户意见、上游变更、顾问对话四条回路的修订指令将汇到这里。",
            dec: "合同要点卡、签署与项目经理确认的决定将记在这里。",
            chg: "超出合同范围的需求将生成变更单，由项目经理确认。" }[tab]}</div>
        </section>
      )}
    </>
  );
}

const FORMAT = { docx: "Word", pptx: "PPT", xlsx: "Excel", platform: "平台内" };

// 四期 A33：输出页按阶段列 D1～D8 输出包（组件清单来自产品配置），已签署的给只读查看；每个阶段一键导出阶段包（未签署带“草稿”水印）。
function Components({ items }) {
  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 4, marginTop: 4 }}>
      {items.map((x) => (
        <span key={x.id} className="chip" title={`${x.id}${x.client_signs ? " · 客户签字" : ""}${x.default ? "" : " · 可选"}`}>
          {x.name} · {FORMAT[x.format] || x.format}{x.default ? "" : "（可选）"}
        </span>
      ))}
    </div>
  );
}

export function OutputPage({ code }) {
  const { p, error, load } = useProject(code);
  const [out, setOut] = useState(null);
  const [msg, setMsg] = useState("");
  useEffect(() => { api.outputs(code).then(setOut).catch(() => setOut({ stages: [], closeout: [] })); }, [code]);
  const { on: agentView } = useAgentView();   // 交付链运行面板属 Agent 运行情况，默认隐藏；非项目经理只读（四期 A29）
  const [canManage, setCanManage] = useState(false);
  useEffect(() => {
    if (agentView) api.panorama(code, true).then((d) => setCanManage(Boolean(d.can_manage_runs))).catch(() => {});
  }, [code, agentView]);
  if (!p) return <Loading error={error} />;
  const status = Object.fromEntries(p.deliverables.map((d) => [d.code, d.status]));
  const stages = out?.stages?.length ? out.stages
    : [{ stage: "", deliverables: p.deliverables.map((d) => ({ ...d, components: [], signed: d.status === "signed" })) }];
  const exportStage = (stage) => {
    setMsg("");
    api.exportStage(p.code, stage).then((r) => setMsg(`已导出 ${r.name}`)).catch((e) => setMsg(e.message || "导出失败"));
  };
  return (
    <>
      {head(p, "输出")}
      <div className="grid-main">
        <section className="pnl pad" aria-label="交付件">
          <h2 className="h2">输出包</h2>
          {msg && <div className="sub" role="status">{msg}</div>}
          {stages.map((st) => (
            <Fragment key={st.stage || "all"}>
              {st.stage && (
                <div className="sb" style={{ marginTop: 12 }}>
                  <h3 className="h2">{st.stage}</h3>
                  <button type="button" className="btn" onClick={() => exportStage(st.stage)}>导出阶段包</button>
                </div>
              )}
              <ul className="lst">
                {st.deliverables.map((d) => {
                  const s = DELIV_STATUS[status[d.code] ?? d.status] || DELIV_STATUS.not_started;
                  return (
                    <li key={d.code}>
                      <div className="sb">
                        <Link to={`${base(p.code)}/deliverables/${d.code}`} style={{ color: "var(--tx)", textDecoration: "none" }}>
                          <span className="mono sub" style={{ marginRight: 8 }}>{d.code}</span>{d.title}
                          {d.signed && <span className="sub" style={{ marginLeft: 8 }}>只读查看</span>}
                        </Link>
                        <span className="row"><span className={`chip ${s.cls}`}>{s.label}</span><LocateLink code={p.code} node={`deliv:${d.code}`} /></span>
                      </div>
                      {d.components?.length > 0 && <Components items={d.components} />}
                    </li>
                  );
                })}
              </ul>
            </Fragment>
          ))}
          {out?.closeout?.length > 0 && (
            <>
              <h3 className="h2" style={{ marginTop: 12 }}>结项复盘</h3>
              <Components items={out.closeout} />
            </>
          )}
        </section>
        {agentView && <RunPanel code={p.code} strict={p.tier === "strict"} onChange={load} canManage={canManage} />}
      </div>
    </>
  );
}

export function SettingsPage({ code }) {
  const { p, error, load } = useProject(code);
  const [params] = useSearchParams();
  if (!p) return <Loading error={error} />;
  return (
    <>
      {head(p, "设置")}
      {params.get("new") === "1" && (
        <div className="pnl pad" role="status" aria-label="立项后下一步">
          立项完成。下一步：在下面勾选项目成员并添加，然后<Link to={base(p.code)}>回驾驶舱</Link>开始启动包。
        </div>
      )}
      <Members code={p.code} />
      <Retention p={p} onChange={load} />
      <Closeout p={p} onChange={load} />
    </>
  );
}

// 调研助手全屏：不带侧栏和项目导航，现场用
export function SurveyFull({ code }) {
  const { p, error } = useProject(code);
  if (!p) return <div className="full"><Loading error={error} /></div>;
  return (
    <div className="full">
      <div className="sb"><Link className="sub" to={`${base(p.code)}/input`}>← 返回 {p.code} · 输入</Link><span className="cap">{p.service}</span></div>
      <Survey code={p.code} tier={p.tier} />
    </div>
  );
}
