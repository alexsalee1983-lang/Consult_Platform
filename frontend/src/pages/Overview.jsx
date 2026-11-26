import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, TIER } from "../api.js";
import { Loading, PhaseBar, TopBar } from "../components/common.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import Drawer from "../components/Drawer.jsx";
import Panorama, { autoDone, todos } from "../components/Panorama.jsx";
import RunPanel from "../components/RunPanel.jsx";

// 全景（四期 A25②）：页头 + 行动条 + 泳道全景图；确认卡、交付链、待办在右侧抽屉里办，资料、交付件等跳到对应导航页。
// 证据、调研、评估、交付件、成员与保留期已挪到 输入 / 分析 / 输出 / 设置 页。

function Kpi({ cap, value, sub, color }) {
  return (
    <div className="pnl pad" style={{ gap: 4 }}>
      <div className="cap">{cap}</div>
      <div className="kpi-v" style={{ color: color || "var(--tx)" }}>{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

export default function Overview({ code, focus }) {
  const [p, setP] = useState(null);
  const [error, setError] = useState(null);
  const [external, setExternal] = useState(false);   // 当前接的是外部测试模型
  useEffect(() => { api.health().then((h) => setExternal(Boolean(h.llm_external))).catch(() => {}); }, []);

  const [evs, setEvs] = useState(null);
  const [pano, setPano] = useState(null);
  const [drawer, setDrawer] = useState(null);         // "todo" | "drawer:run" | "drawer:card:{id}"
  const navigate = useNavigate();
  const load = useCallback(() => {
    setError(null);
    api.project(code).then(setP).catch(setError);
    api.evidences(code).then(setEvs).catch(() => setEvs([]));
    api.panorama(code).then(setPano).catch(() => setPano(null));
  }, [code]);
  useEffect(load, [load]);
  const close = useCallback(() => setDrawer(null), []);

  if (!p) return <Loading error={error} />;
  const tier = TIER[p.tier] || TIER.standard;
  const base = `/project/${encodeURIComponent(p.code)}`;
  const signed = p.deliverables.filter((d) => d.status === "signed").length;
  const strict = p.tier === "strict";
  const pending = p.deliverables.filter((d) => d.status === "review").map((d) => d.code);
  const drafting = p.deliverables.filter((d) => d.status === "drafting").map((d) => d.code);
  const count = (k) => (evs || []).filter((e) => e.kind === k).length;
  const todo = pano ? todos(pano) : [];
  const blocked = pano ? pano.nodes.filter((n) => n.status === "blk").length : 0;
  const card = drawer?.startsWith("drawer:card:") ? p.confirmations.find((c) => `drawer:card:${c.id}` === drawer) : null;
  const doTodo = (t) => (t.target.startsWith("drawer:") ? setDrawer(t.target) : navigate(t.target));

  return (
    <>
      <TopBar cap={`${p.code} · ${p.industry} · ${tier.label} · 阶段 ${p.phase_name} · 第 ${p.week} / ${p.total_weeks} 周`} title={`项目驾驶舱 · ${p.service}`}>
        <button type="button" className="btn" onClick={() => setDrawer("drawer:run")}>交付链</button>
        <Link className="btnp" to={`/chat/${encodeURIComponent(p.code)}`} style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>打开顾问对话</Link>
      </TopBar>
      {error && <div className="err">{error.message}</div>}
      <section className="kpis" aria-label="行动条">
        <Kpi cap="下一步" value={p.phase_name} sub={`下一个里程碑：${p.next_milestone}`} />
        <button type="button" className="pnl pad" style={{ gap: 4, textAlign: "left", cursor: "pointer", font: "inherit", color: "inherit" }}
          onClick={() => setDrawer("todo")}>
          <span className="cap">待你处理 · 按影响排序</span>
          <span className="kpi-v" style={{ color: todo.length ? "var(--amber)" : "var(--ok)" }}>{todo.length}</span>
          <span className="sub">{todo[0] ? `最先：${todo[0].title}` : "没有待你处理的事项"}</span>
        </button>
        <Kpi cap="卡点" value={blocked} sub="全景里标红的节点" color={blocked ? "var(--crim)" : "var(--ok)"} />
        <Kpi cap="本周自动处理" value={pano ? autoDone(pano) : "—"} sub="暂按交付链已完成步骤计" color="var(--ok)" />
      </section>
      <div className="kpis">
        <Kpi cap="证据" value={evs ? evs.length : "—"} sub={strict ? "严格档只收摘要卡 S-xx" : `访谈 ${count("interview")} · 资料 ${count("document")} · 观察 ${count("observation")}`} />
        <Kpi cap="交付件已签署" value={`${signed} / ${p.deliverables.length}`} 
          sub={[pending.length && `${pending.join("、")} 待签署`, drafting.length && `${drafting.join("、")} 起草中`].filter(Boolean).join(" · ") || "按本项目交付件统计"} />
        <Kpi cap="待你确认" value={p.open_confirmations} sub="Agent 需要你判断的事项" color={p.open_confirmations ? "var(--amber)" : "var(--ok)"} />
        <Kpi cap="数据档位" value={tier.label} sub={p.tier === "strict" ? "资料不出场 · 只收摘要卡" : (external ? "当前为外部测试模型 · 只用虚拟数据" : "模型在公司内部 · 资料不出公司")} color={p.tier === "strict" ? "var(--crim)" : "var(--cyan)"} />
      </div>
      <Panorama code={p.code} data={pano} focus={focus} onOpen={setDrawer} />
      <section className="pnl pad" aria-label="阶段">
        <div className="sb"><h2 className="h2">交付阶段</h2><span className="sub">下一个里程碑：{p.next_milestone}</span></div>
        <PhaseBar phase={p.phase} />
      </section>
      {drawer === "todo" && (
        <Drawer title="待你处理 · 按影响排序" onClose={close} more={{ to: "/my-tasks", label: "我的任务（跨项目）" }}>
          <ol className="lst" style={{ gap: 6 }}>
            {todo.map((t) => (
              <li key={t.id}>
                <button type="button" className="opt" style={{ width: "100%", textAlign: "left" }} onClick={() => doTodo(t)}>
                  <span className="chip c-n">{t.type === "card" ? "确认卡" : "签署"}</span>
                  <span style={{ flex: 1 }}>{t.title}</span>
                  <span className="sub mono" title="下游受影响的节点数">影响 {t.impact}</span>
                </button>
              </li>
            ))}
            {todo.length === 0 && <li className="sub">没有待你处理的事项</li>}
          </ol>
        </Drawer>
      )}
      {drawer === "drawer:run" && (
        <Drawer title="交付链" onClose={close} more={{ to: `${base}/output`, label: "去输出页" }}>
          <RunPanel code={p.code} strict={strict} onChange={load} />
        </Drawer>
      )}
      {card && (
        <Drawer title="确认卡" onClose={close} more={{ to: `${base}/analysis`, label: "查看全部待确认" }}>
          <ul className="lst"><ConfirmCard c={card} onDone={() => { close(); load(); }} /></ul>
        </Drawer>
      )}
    </>
  );
}
