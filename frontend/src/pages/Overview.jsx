import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, TIER } from "../api.js";
import { Loading, TopBar } from "../components/common.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import Drawer from "../components/Drawer.jsx";
import { gates, mine, progress, remaining, todos } from "../cockpit/view.js";
import { useAgentView } from "../components/AgentActivity.jsx";

// 全景（四期 A37，docs/交付链输入输出方案.md 8.1“全景三块”）：首屏只回答“在哪个关口、还差什么、卡在谁”。
// ① 顶部一行：进度环、第 N 周 · 阶段、过关口还差几件、按期 / 预警；② 关口进度线（按产品配置，点关口跳对应页）；
// ③ “过关口还差”卡片 + “待我处理”（一行一件事，带“只看我的”）。后台自动完成的事不上全景，只进健康看板（负责人 11-27）。
// 取代 A25 的行动条与泳道全景；结项移到设置页（8.3）。确认卡在右侧抽屉就地办。

const KIND = { card: ["确认卡", "c-a"], signoff: ["签署", "c-v"] };

// 点关口跳哪一页：有交付件的跳该关口第一份交付件，否则回输入页
const gateTo = (base, g) => (g.outputs[0] ? `${base}/deliverables/${g.outputs[0]}` : `${base}/input`);

export default function Overview({ code, focus }) {
  const [p, setP] = useState(null);
  const [error, setError] = useState(null);
  const [external, setExternal] = useState(false);   // 当前接的是外部测试模型
  useEffect(() => { api.health().then((h) => setExternal(Boolean(h.llm_external))).catch(() => {}); }, []);

  const [pano, setPano] = useState(null);
  const [members, setMembers] = useState(null);
  const [onlyMine, setOnlyMine] = useState(false);
  const [drawer, setDrawer] = useState(focus?.startsWith("card:") ? `drawer:${focus}` : null);   // "drawer:card:{id}"
  const navigate = useNavigate();
  const { on: agentView } = useAgentView();            // 右上角“查看 Agent 运行情况”（四期 A29）
  const load = useCallback(() => {
    setError(null);
    api.project(code).then(setP).catch(setError);
    api.panorama(code, agentView).then(setPano).catch(() => setPano(null));
    api.members(code).then(setMembers).catch(() => setMembers(null));
  }, [code, agentView]);
  useEffect(load, [load]);
  const close = useCallback(() => setDrawer(null), []);

  if (!p) return <Loading error={error} />;
  const tier = TIER[p.tier] || TIER.standard;
  const base = `/project/${encodeURIComponent(p.code)}`;
  const ring = progress(p);
  const gl = gates(pano);
  const curGate = gl.find((g) => g.state === "cur");
  const left = remaining(p, pano);
  const all = pano ? todos(pano) : [];
  const rows = onlyMine && pano ? mine(pano, all) : all;
  const late = left.some((r) => r.blocked);
  const alone = members?.length === 1;
  const card = drawer?.startsWith("drawer:card:") ? p.confirmations.find((c) => `drawer:card:${c.id}` === drawer) : null;
  const doTodo = (t) => (t.target.startsWith("drawer:") ? setDrawer(t.target) : navigate(t.target));
  const doneW = gl.length > 1 ? (100 * gl.filter((g) => g.state === "ok").length) / (gl.length - 1) : 0;

  return (
    <>
      <TopBar cap={`${p.code} · ${p.industry} · ${tier.label}`} title={`项目驾驶舱 · ${p.service}`}>
        <span className={`chip ${p.tier === "strict" ? "c-r" : "c-c"}`}>{p.tier === "strict" ? "资料不出场" : (external ? "当前为外部测试模型 · 只用虚拟数据" : "模型在公司内部")}</span>
      </TopBar>
      {error && <div className="err">{error.message}</div>}

      <section className="pnl hero" aria-label="项目进度">
        <span className="ring lg" style={{ "--v": ring.pct }} role="img" aria-label={`已签署 ${ring.signed} / ${ring.all}`}><i>{ring.pct}%</i></span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="hero-t">第 {p.week} 周 · {p.phase_name}阶段</div>
          {alone   // 新项目只有项目经理一人时先提示加成员（负责人 2026-11-26）
            ? <Link to={`${base}/settings`}>先添加项目成员 → 去设置页勾选同事</Link>
            : <div className="sub">{curGate
              ? <>过关口 {curGate.n}「{curGate.name}」还差 <b style={{ color: "var(--amber)" }}>{left.length} 件事</b> · 下一个里程碑：{p.next_milestone}</>
              : "关口已全部通过"}</div>}
        </div>
        <span className={`chip ${late ? "c-a" : "c-g"}`}>● {late ? "预警" : "按期"}</span>
      </section>

      <section className="pnl pad" aria-label="关口进度线">
        <div className="metro" style={{ "--n": gl.length, "--done": `${doneW}%` }}>
          <div className="trk"><i /></div>
          {gl.map((g) => (
            <Link key={g.id} to={gateTo(base, g)} className={`stn${g.state ? ` ${g.state}` : ""}`} aria-current={g.state === "cur" ? "step" : undefined}>
              <span className="nd">{g.state === "ok" ? "✓" : g.n}</span>
              <b>{g.name}</b>
              <small className="sub">{g.state === "ok" ? "已过" : g.state === "cur" ? "进行中" : g.outputs.join("、")}</small>
            </Link>
          ))}
        </div>
      </section>

      {curGate && (
        <section aria-label="过关口还差">
          <h2 className="h2">过关口 {curGate.n} 还差</h2>
          <div className="blk">
            {left.map((r) => (
              <Link key={r.id} className={`pnl pad bt${focus === r.id ? " flash" : ""}`} to={r.target} data-node={r.id}>
                <div className="sb"><b>{r.title}</b><span className={`chip ${r.blocked ? "c-r" : "c-n"}`}>{r.note}</span></div>
                <div className="bar"><i style={{ width: `${r.pct}%`, background: r.blocked ? "var(--crim)" : "var(--amber)" }} /></div>
                <div className="sub">卡在：{r.who}</div>
              </Link>
            ))}
            {left.length === 0 && <div className="sub">本关口要人做的事都已办完，等待过关。</div>}
          </div>
        </section>
      )}

      <section aria-label="待我处理">
        <div className="sb">
          <h2 className="h2">待我处理</h2>
          <span className="row" style={{ gap: 4 }}>
            {[[false, "全部"], [true, "只看我的"]].map(([v, t]) => (
              <button key={t} type="button" className="btn sm" aria-pressed={onlyMine === v} onClick={() => setOnlyMine(v)}
                style={onlyMine === v ? { borderColor: "var(--amber)", color: "var(--amber)" } : undefined}>{t}</button>
            ))}
            <span className="sub">按影响排，只列要人做的事</span>
          </span>
        </div>
        <ol className="lst" style={{ gap: 6 }}>
          {rows.map((t) => (
            <li key={t.id} className="todo">
              <span className={`chip ${KIND[t.type]?.[1] || "c-n"}`}>{KIND[t.type]?.[0] || "待办"}</span>
              <span style={{ flex: 1 }}>{t.title}</span>
              <span className="sub mono" title="下游受影响的节点数">影响 {t.impact}</span>
              <button type="button" className="btn sm" onClick={() => doTodo(t)}>处理</button>
            </li>
          ))}
          {rows.length === 0 && <li className="sub">没有待你处理的事项</li>}
        </ol>
      </section>

      {card && (
        <Drawer title="确认卡" onClose={close} more={{ to: `${base}/analysis`, label: "查看全部待确认" }}>
          <ul className="lst"><ConfirmCard c={card} onDone={() => { close(); load(); }} /></ul>
        </Drawer>
      )}
    </>
  );
}

