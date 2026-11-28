import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, TIER } from "../api.js";
import { Loading, PhaseBar, TopBar } from "../components/common.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import Closeout from "../components/Closeout.jsx";
import Drawer from "../components/Drawer.jsx";
import Panorama, { todos } from "../components/Panorama.jsx";
import RunPanel from "../components/RunPanel.jsx";
import { useAgentView } from "../components/AgentActivity.jsx";

// 全景（四期 A25②）：页头 + 行动条 + 泳道全景图；确认卡、交付链、待办在右侧抽屉里办，资料、交付件等跳到对应导航页。
// 证据、调研、评估、交付件、成员与保留期已挪到 输入 / 分析 / 输出 / 设置 页。

// 行动条一张卡（设计稿 07 .act）：标签 + 大数字 + 一行说明；负责人 2026-11-26：原八个方块太占地方、字体不统一。
function Card({ cap, value, sub, color, onClick, children }) {
  const body = (
    <>
      <span className="cap">{cap}</span>
      <b className="act-v" style={{ color: color || "var(--tx)" }}>{value}</b>
      {sub && <span className="sub act-s" title={sub}>{sub}</span>}
      {children}
    </>
  );
  const style = { "--accent": color || "var(--ln2)" };   // 卡片顶边强调色随数值状态变
  return onClick
    ? <button type="button" className="pnl act-card act-btn" style={style} onClick={onClick}>{body}</button>
    : <div className="pnl act-card" style={style}>{body}</div>;
}

export default function Overview({ code, focus }) {
  const [p, setP] = useState(null);
  const [error, setError] = useState(null);
  const [external, setExternal] = useState(false);   // 当前接的是外部测试模型
  useEffect(() => { api.health().then((h) => setExternal(Boolean(h.llm_external))).catch(() => {}); }, []);

  const [evs, setEvs] = useState(null);
  const [pano, setPano] = useState(null);
  const [members, setMembers] = useState(null);
  const [drawer, setDrawer] = useState(null);         // "todo" | "drawer:run" | "drawer:card:{id}" | "drawer:deliv:{code}"
  const navigate = useNavigate();
  const { on: agentView } = useAgentView();            // 右上角“查看 Agent 运行情况”（四期 A29）
  const load = useCallback(() => {
    setError(null);
    api.project(code).then(setP).catch(setError);
    api.evidences(code).then(setEvs).catch(() => setEvs([]));
    api.panorama(code, agentView).then(setPano).catch(() => setPano(null));
    api.members(code).then(setMembers).catch(() => setMembers(null));
  }, [code, agentView]);
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
  const alone = members?.length === 1;
  const blocked = pano ? pano.nodes.filter((n) => n.status === "blk").length : 0;
  const card = drawer?.startsWith("drawer:card:") ? p.confirmations.find((c) => `drawer:card:${c.id}` === drawer) : null;
  const doTodo = (t) => (t.target.startsWith("drawer:") ? setDrawer(t.target) : navigate(t.target));

  return (
    <>
      <TopBar cap={`${p.code} · ${p.industry} · ${tier.label} · 阶段 ${p.phase_name} · 第 ${p.week} / ${p.total_weeks} 周`} title={`项目驾驶舱 · ${p.service}`}>
        {agentView && <button type="button" className="btn" onClick={() => setDrawer("drawer:run")}>交付链</button>}
        <Link className="btnp" to={`/chat/${encodeURIComponent(p.code)}`} style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>打开顾问对话</Link>
      </TopBar>
      {error && <div className="err">{error.message}</div>}
      <div className="row" aria-label="项目概况" style={{ flexWrap: "wrap", gap: 6 }}>
        <span className={`chip ${p.tier === "strict" ? "c-r" : "c-c"}`}>{tier.label} · {p.tier === "strict" ? "资料不出场" : (external ? "当前为外部测试模型 · 只用虚拟数据" : "模型在公司内部")}</span>
        <span className="chip c-n">已签署 {signed} / {p.deliverables.length}{pending.length ? ` · ${pending.join("、")} 待签` : ""}{drafting.length ? ` · ${drafting.join("、")} 起草中` : ""}</span>
        <span className={`chip ${p.open_confirmations ? "c-a" : "c-n"}`}>待你确认 {p.open_confirmations}</span>
        <span className="chip c-n">证据 {evs ? evs.length : "—"}{strict ? " · 只收摘要卡" : ` · 访谈 ${count("interview")} · 资料 ${count("document")} · 观察 ${count("observation")}`}</span>
      </div>
      <section className="act" aria-label="行动条">
        <div className="pnl act-card act-next">
          <span className="cap"><i className="live" aria-hidden="true" />下一步 · 现在在 <span style={{ color: "var(--amber)" }}>{p.phase_name}</span></span>
          {alone   // 新项目只有项目经理一人时先提示加成员（负责人 2026-11-26）
            ? <Link className="act-next-t" to={`${base}/settings`} style={{ color: "var(--tx)" }}>先添加项目成员 → 去设置页勾选同事</Link>
            : <span className="act-next-t">{todo[0] ? todo[0].title : "当前没有卡住的事项，按计划推进"}</span>}
          <span className="sub">下一个里程碑：{p.next_milestone}</span>
        </div>
        <Card cap="待你处理" value={todo.length} color={todo.length ? "var(--amber)" : "var(--ok)"}
          sub={todo[0] ? `最先：${todo[0].title}` : "没有待你处理的事项"} onClick={() => setDrawer("todo")} />
        <Card cap="卡点" value={blocked} color={blocked ? "var(--crim)" : "var(--ok)"} sub="全景里标红的节点" />
        <Card cap="本周自动处理" value={pano ? pano.auto_done : "—"} color="var(--ok)" sub="近 7 天 AI 自动完成的步骤" />
        <Card cap="交付件" value={`${signed} / ${p.deliverables.length}`} sub="已签署 / 全部" color="var(--cyan)">
          <div className="bar" style={{ marginTop: 4 }}><i style={{ width: `${p.deliverables.length ? (100 * signed) / p.deliverables.length : 0}%`, background: "var(--cyan)" }} /></div>
        </Card>
      </section>
      <Panorama code={p.code} data={pano} focus={focus} onOpen={setDrawer} />
      <Closeout p={p} onChange={load} />
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
          <RunPanel code={p.code} strict={strict} onChange={load} canManage={Boolean(pano?.can_manage_runs)} />
        </Drawer>
      )}
      {drawer?.startsWith("drawer:deliv:") && (
        <Drawer title="已完成交付件 · 只读" onClose={close} more={{ to: `${base}/deliverables`, label: "全部交付件" }}>
          <SignedPanel code={p.code} dcode={drawer.slice("drawer:deliv:".length)} />
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

const fmtDate = (t) => (t ? new Date(t).toLocaleDateString("zh-CN") : "—");

// 已完成交付件的只读面板（四期 A29，《平台规划刷新设计》4.1）：签署版、签署人与日期、交客户与回执、引用依据是否仍有效。
// 已签署的不能直接改；要改走“发起勘误”（勘误流程待后续工作项接入）。
function SignedPanel({ code, dcode }) {
  const [d, setD] = useState(null);
  const [so, setSo] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    api.deliverable(code, dcode).then(setD).catch((e) => setErr(e.message));
    api.signoff(code, dcode).then(setSo).catch(() => setSo({ history: [] }));
  }, [code, dcode]);
  if (!d) return <div className="sub">{err || "加载中…"}</div>;
  const sign = [...(so?.history || [])].reverse().find((h) => h.action === "sign");
  const updates = d.totals?.knowledge_updates || 0;
  const page = `/project/${encodeURIComponent(code)}/deliverables/${encodeURIComponent(dcode)}`;
  const download = () => api.exportWord(code, dcode).catch((e) => setErr(e.message));
  return (
    <section aria-label="已完成交付件" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="h2">{d.code} · {d.title}</div>
      <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", margin: 0 }}>
        <dt>签署版</dt><dd className="mono">{sign ? `r${sign.revision}` : d.revision ? `r${d.revision}` : "—"}</dd>
        <dt>签署人与日期</dt><dd>{sign ? `${sign.actor} · ${fmtDate(sign.created_at)}` : "—"}</dd>
        <dt>交客户与回执</dt><dd className="sub">交付登记暂未接入，以项目经理线下记录为准</dd>
        <dt>引用依据</dt><dd style={{ color: updates ? "var(--amber)" : "var(--ok)" }}>{updates ? `${updates} 条依据已有更新，建议发起勘误` : "仍有效"}</dd>
      </dl>
      {err && <div className="err" role="alert">{err}</div>}
      <div className="sub">已签署的不能直接改；要改请发起勘误。</div>
      <div className="row">
        <Link className="btn sm" to={page}>查看</Link>
        <button type="button" className="btn sm" onClick={download}>下载</button>
        <Link className="btn sm" to={page} title="签署流程一栏里有每一版的记录">版本历史</Link>
        <button type="button" className="btn sm" disabled title="勘误流程待后续工作项接入">发起勘误</button>
      </div>
    </section>
  );
}
