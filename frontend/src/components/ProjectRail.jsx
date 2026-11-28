// 项目栏（四期 A37，docs/交付链输入输出方案.md 8.1，示意 11 左栏）：项目卡（名称、档位、进度环）、新建项目、全景、
// “输入 → 需求 → 分析 → 输出”竖向交付链（已过绿点、当前琥珀点、待办数），D1～D8 挂在“输出”下带状态点，调研助手、设置。
// 取代 A25 的横向五导航；交付件页算在“输出”下。
import { useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router";
import { api, TIER } from "../api.js";
import { chainSteps, delivDot, progress, todos } from "../cockpit/view.js";

const cls = ({ isActive }) => (isActive ? "on" : undefined);

export default function ProjectRail({ code }) {
  const { pathname } = useLocation();
  const [p, setP] = useState(null);
  const [pano, setPano] = useState(null);
  const [cust, setCust] = useState(null);
  useEffect(() => {      // 换页时刷新待办数与状态点
    let live = true;
    api.project(code).then((d) => live && setP(d)).catch(() => {});
    api.panorama(code).then((d) => live && setPano(d)).catch(() => {});
    api.customizations(code).then((d) => live && setCust(d.items)).catch(() => {});
    return () => { live = false; };
  }, [code, pathname]);

  const base = `/project/${encodeURIComponent(code)}`;
  const ring = progress(p);
  const tier = TIER[p?.tier] || TIER.standard;
  const steps = chainSteps(p, pano, cust);
  const todo = pano ? todos(pano).length : 0;
  const inDeliv = pathname.includes("/deliverables");
  return (
    <nav className="pnl rail" aria-label="项目导航">
      <div className="pcard">
        <span className="ring" style={{ "--v": ring.pct }} role="img" aria-label={`已签署 ${ring.signed} / ${ring.all}`}>
          <i>{ring.signed}/{ring.all}</i>
        </span>
        <div style={{ minWidth: 0 }}>
          <div className="mono" style={{ fontWeight: 700, color: "var(--tx)" }}>{code}</div>
          <div className="sub" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p?.service || ""}</div>
          {p && <span className={`chip ${p.tier === "strict" ? "c-r" : "c-c"}`}>{tier.label}</span>}
        </div>
      </div>
      <Link className="it" to="/projects?new=1">＋ 新建项目</Link>
      <NavLink className={cls} to={base} end>
        <span>◎ 全景</span>{todo > 0 && <span className="n">{todo}</span>}
      </NavLink>
      <div className="cap nav-group">交付链</div>
      <div className="chain" aria-label="交付链">
        {steps.map((s) => (
          <NavLink key={s.seg} to={`${base}/${s.seg}`} data-state={s.state || "todo"}
            className={({ isActive }) => [`s-${s.state || "todo"}`, isActive || (s.seg === "output" && inDeliv) ? "on" : ""].join(" ").trim()}>
            <span>{s.label}</span>{s.n > 0 && <span className="n">{s.n}</span>}
          </NavLink>
        ))}
      </div>
      <div className="sub2" aria-label="交付件">
        {(p?.deliverables || []).map((d) => (
          <NavLink key={d.code} className={cls} to={`${base}/deliverables/${d.code}`} title={d.title}>
            <span><span className="mono">{d.code}</span> {d.title}</span><i className={`sd sd-${delivDot(d.status) || "todo"}`} aria-hidden="true" />
          </NavLink>
        ))}
      </div>
      <div className="cap nav-group">现场</div>
      <Link to={`${base}/survey`} title="现场用，全屏打开">调研助手 ⤢</Link>
      <NavLink className={cls} to={`${base}/settings`}>设置</NavLink>
    </nav>
  );
}
