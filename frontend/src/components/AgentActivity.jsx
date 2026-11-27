// 页面右上角“查看 Agent 运行情况”（四期 A29，《平台规划刷新设计》2.2）：默认关，每次登录重新关上，只读，按角色可见。
// 项目页里打开：全景多出“Agent 运行”一行、驾驶舱出现“交付链”抽屉（项目经理可重跑 / 暂停，成员只读）。
// 项目页外打开：知识侧运行记录（知识管理员可重跑、去来源登记暂停来源；咨询业务负责人只读）、
// 跨项目运行状态（咨询业务负责人，只看步骤和失败类型）。运行失败或卡住时不管开没开，都推卡到“我的任务”。
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { Link, useLocation } from "react-router";
import { api } from "../api.js";
import { agentScopes, showAgentButton } from "../agentScope.js";
import { useAuth } from "../auth.jsx";

const Ctx = createContext({ on: false, setOn: () => {} });

export function AgentViewProvider({ children }) {
  const [on, setOn] = useState(false);
  return <Ctx.Provider value={{ on, setOn }}>{children}</Ctx.Provider>;
}

export const useAgentView = () => useContext(Ctx);

const STATUS = { queued: ["排队", "c-n"], running: ["在跑", "c-c"], waiting: ["等人处理", "c-a"], done: ["完成", "c-g"],
  failed: ["失败", "c-r"], cancelled: ["已暂停", "c-n"] };
const fmt = (t) => (t ? new Date(t).toLocaleString("zh-CN", { hour12: false }) : "—");

function Status({ s }) {
  const [label, cls] = STATUS[s] || [s, "c-n"];
  return <span className={`chip ${cls}`}>{label}</span>;
}

function KnowledgeRuns() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const load = useCallback(() => { api.agentKnowledge().then(setD).catch((e) => setErr(e.message)); }, []);
  useEffect(load, [load]);
  const retry = async (id) => {
    setErr("");
    try { await api.agentRetry(id); load(); } catch (e) { setErr(e.message); }
  };
  if (!d) return <div className="sub">{err || "加载中…"}</div>;
  return (
    <section aria-label="知识侧运行记录">
      <div className="sb"><h2 className="h2">知识侧运行记录</h2>
        {d.can_manage ? <Link className="btn sm" to="/kb/sources">暂停来源（来源登记）</Link> : <span className="sub">只读</span>}</div>
      {err && <div className="err" role="alert">{err}</div>}
      {d.jobs.length ? (
        <table className="tbl">
          <thead><tr><th>编号</th><th>类别</th><th>状态</th><th>次数</th><th>失败类型</th><th>时间</th>{d.can_manage && <th />}</tr></thead>
          <tbody>{d.jobs.map((j) => (
            <tr key={j.id}>
              <td className="mono">#{j.id}</td><td className="mono">{j.kind}</td><td><Status s={j.status} /></td>
              <td className="mono">{j.attempts}</td><td className="mono">{j.failure || "—"}</td><td className="sub">{fmt(j.created_at)}</td>
              {d.can_manage && <td>{j.status === "failed" && <button className="btn sm" onClick={() => retry(j.id)}>重跑</button>}</td>}
            </tr>
          ))}</tbody>
        </table>
      ) : <div className="sub">最近没有知识侧运行记录。</div>}
    </section>
  );
}

function ProjectRuns() {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => { api.agentProjects().then(setRows).catch((e) => setErr(e.message)); }, []);
  if (!rows) return <div className="sub">{err || "加载中…"}</div>;
  return (
    <section aria-label="各项目运行状态">
      <div className="sb"><h2 className="h2">各项目运行状态</h2><span className="sub">只看步骤和失败类型，不看内容；进项目仍须是成员</span></div>
      {rows.length ? (
        <table className="tbl">
          <thead><tr><th>项目</th><th>状态</th><th>步骤</th><th>失败类型</th><th>更新</th></tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={r.project}>
              <td className="mono">{r.project}</td><td><Status s={r.status} /></td><td>{r.step_name || r.step}</td>
              <td className="mono">{r.failure || "—"}</td><td className="sub">{fmt(r.updated_at)}</td>
            </tr>
          ))}</tbody>
        </table>
      ) : <div className="sub">还没有项目运行过交付链。</div>}
    </section>
  );
}

export function AgentBar() {
  const { user } = useAuth();
  const { on, setOn } = useAgentView();
  const { pathname } = useLocation();
  const inProject = pathname.startsWith("/project/");
  const scopes = agentScopes(user?.roles, inProject);
  if (!showAgentButton(scopes)) return null;
  return (
    <>
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button type="button" className={on ? "btnp sm" : "btn sm"} aria-pressed={on} onClick={() => setOn(!on)}
          title="只读；默认关闭，每次登录重新关上">{on ? "收起 Agent 运行情况" : "查看 Agent 运行情况"}</button>
      </div>
      {on && !inProject && (scopes.knowledge || scopes.portfolio) && (
        <section className="pnl pad" aria-label="Agent 运行情况" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {scopes.knowledge && <KnowledgeRuns />}
          {scopes.portfolio && <ProjectRuns />}
        </section>
      )}
    </>
  );
}
