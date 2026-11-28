// 项目内五个导航页（四期 A25②，见 docs/驾驶舱改版方案.md 第二、六节）：全景 | 输入 | 分析 | 输出 | 设置。
// 各页复用已有面板组件，只挪位置；条目上的“⌖ 全景”回全景并高亮对应节点（节点 id 与 GET .../panorama 一致）。
// 调研助手全屏单开（现场用），交付件页挂在“输出”下。
import { Fragment, useCallback, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useParams, useSearchParams } from "react-router";
import { api, DELIV_STATUS } from "../api.js";
import { useAuth } from "../auth.jsx";
import Assessment from "../components/Assessment.jsx";
import { Loading, TopBar } from "../components/common.jsx";
import { groupCards } from "../components/CardEvidence.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import Evidence from "../components/Evidence.jsx";
import Members from "../components/Members.jsx";
import Retention from "../components/Retention.jsx";
import RunPanel from "../components/RunPanel.jsx";
import StrictCards from "../components/StrictCards.jsx";
import Survey from "../components/Survey.jsx";

const base = (code) => `/project/${encodeURIComponent(code)}`;
export const NAV = [["", "全景"], ["input", "输入"], ["analysis", "分析"], ["output", "输出"], ["settings", "设置"]];

export function LocateLink({ code, node }) {
  return <Link className="locate" to={`${base(code)}?focus=${encodeURIComponent(node)}`} title="回全景并高亮这个节点">⌖ 全景</Link>;
}

// 项目内导航条：交付件页（/deliverables/Dx）算在“输出”下，并显示 项目 / 输出 / Dx
export function ProjectLayout() {
  const { code } = useParams();
  const { pathname } = useLocation();
  const dcode = pathname.match(/\/deliverables\/([^/]+)/)?.[1];
  const inOutput = pathname.includes("/deliverables");
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
    <>
      <nav className="tabs" aria-label="项目导航">
        {NAV.map(([seg, label]) => (
          <NavLink key={label} to={seg ? `${base(code)}/${seg}` : base(code)} end={!seg}
            className={({ isActive }) => (isActive || (seg === "output" && inOutput) ? "on" : undefined)}>{label}</NavLink>
        ))}
        <Link to={`${base(code)}/survey`} style={{ marginLeft: "auto" }} title="现场用，全屏打开">调研助手 ⤢</Link>
      </nav>
      {alone && !onSettings && (
        <div className="note-amber" role="status" aria-label="待配项目成员">
          这个项目还没有其他成员，<Link to={`${base(code)}/settings?new=1`}>先去设置里勾选项目成员</Link>，再开始启动包。
        </div>
      )}
      {dcode && <div className="sub">{code} / <Link to={`${base(code)}/output`}>输出</Link> / {decodeURIComponent(dcode)}</div>}
      <Outlet />
    </>
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
  const [evs, setEvs] = useState(null);
  const loadEvs = useCallback(() => { api.evidences(code).then(setEvs).catch(() => setEvs([])); }, [code]);
  useEffect(loadEvs, [loadEvs]);
  if (!p) return <Loading error={error} />;
  const strict = p.tier === "strict";
  return (
    <>
      {head(p, "输入")}
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

export function OutputPage({ code }) {
  const { p, error, load } = useProject(code);
  if (!p) return <Loading error={error} />;
  return (
    <>
      {head(p, "输出")}
      <div className="grid-main">
        <section className="pnl pad" aria-label="交付件">
          <h2 className="h2">交付件</h2>
          <ul className="lst">
            {p.deliverables.map((d) => {
              const st = DELIV_STATUS[d.status] || DELIV_STATUS.not_started;
              return (
                <li key={d.code} className="sb">
                  <Link to={`${base(p.code)}/deliverables/${d.code}`} style={{ color: "var(--tx)", textDecoration: "none" }}>
                    <span className="mono sub" style={{ marginRight: 8 }}>{d.code}</span>{d.title}
                  </Link>
                  <span className="row"><span className={`chip ${st.cls}`}>{st.label}</span><LocateLink code={p.code} node={`deliv:${d.code}`} /></span>
                </li>
              );
            })}
          </ul>
        </section>
        <RunPanel code={p.code} strict={p.tier === "strict"} onChange={load} />
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
