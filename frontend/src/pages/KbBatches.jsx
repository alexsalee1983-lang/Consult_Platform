import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { hasRole, useAuth } from "../auth.jsx";
import { Loading, TopBar } from "../components/common.jsx";

// 知识批次（三期 B18①）：列出切条、导入、起草、评测、依据核对等批次；页面只能发起导入（kb-import --submit）和评测（kb-eval），
// 都排后台任务由 worker 执行。列表有排队中、运行中的批次时每 15 秒刷新一次。
const STATUS = {
  queued: ["排队中", "c-n"],
  running: ["运行中", "c-c"],
  done: ["完成", "c-g"],
  failed: ["失败", "c-r"],
};
const COUNT_LABEL = {
  total: "合计", created: "新建", revised: "修订", unchanged: "无变化", skipped: "跳过", error: "失败",
  docs: "份数", entries: "条数", rejected: "退回", accepted: "起草", pending: "待核引用", calls: "模型调用",
  published: "自动发布", manual: "转人工", sampled: "抽检", status: "时效变更", new: "库内没有", review: "待复核",
};
const REFRESH_MS = 15000;

function pct(x) {
  return typeof x === "number" ? `${Math.round(x * 100)}%` : "—";
}

function Counts({ b }) {
  const c = b.counts || {};
  if (b.kind === "eval" && c.total !== undefined) {
    const acc = c.compare ? `用知识库 ${pct(c.accuracy_with_kb)} · 不用 ${pct(c.accuracy_no_kb)}` : `准确率 ${pct(c.accuracy)}`;
    const verdict = c.demo ? <span className="chip c-n">演示模式不判</span>
      : <span className={`chip ${c.passed ? "c-g" : "c-r"}`}>{c.passed ? "达标" : "未达标"}</span>;
    return <span className="row" style={{ gap: 6 }}><span className="mono">{c.total} 题 · {acc}</span>{verdict}</span>;
  }
  const parts = Object.entries(c).filter(([k, v]) => COUNT_LABEL[k] && typeof v === "number" && (v || k === "total"))
    .map(([k, v]) => `${COUNT_LABEL[k]} ${v}`);
  const flags = [c.submit && "已提交审核", c.apply && "已登记", c.suspended && "自动发布暂停"].filter(Boolean);
  if (!parts.length && !flags.length) return <span className="sub">—</span>;
  return <span className="mono">{[...parts, ...flags].join(" · ")}</span>;
}

function when(t) {
  return t ? new Date(t).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function StartForm({ sources, onStarted }) {
  const [kind, setKind] = useState("import");
  const [path, setPath] = useState("");
  const [submit, setSubmit] = useState(true);
  const [compare, setCompare] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const options = kind === "import" ? sources.dirs : sources.questions;

  useEffect(() => { setPath(""); }, [kind]);

  async function start(e) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.kbBatchStart({ kind, path, submit, compare });
      setMsg({ ok: true, text: `已排后台任务 #${r.job_id}，由 worker 执行` });
      onStarted();
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="pnl pad" aria-label="发起批次" onSubmit={start} style={{ gap: 10 }}>
      <h2 className="h2">发起批次</h2>
      <label className="field">种类
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="import">导入条目文件（kb-import）</option>
          <option value="eval">依据查询评测（kb-eval）</option>
        </select>
      </label>
      <label className="field">{kind === "import" ? "资料目录" : "题目文件"}
        <select value={path} onChange={(e) => setPath(e.target.value)} required>
          <option value="">请选择</option>
          {options.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </label>
      {options.length === 0 && <div className="sub">资料目录里没有可选的{kind === "import" ? "目录" : " .yaml 题目文件"}；先在部署机把私有资料仓拉到 KB_DOWNLOADS_DIR。</div>}
      {kind === "import"
        ? <label className="row sub" style={{ gap: 6 }}><input type="checkbox" checked={submit} onChange={(e) => setSubmit(e.target.checked)} />导入后一并提交审核（--submit）</label>
        : <label className="row sub" style={{ gap: 6 }}><input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />有无知识库对比评测（--compare，经网关调模型）</label>}
      <div className="row" style={{ gap: 8 }}>
        <button className="btnp" type="submit" disabled={busy || !path}>{busy ? "提交中…" : "排入后台"}</button>
        {msg && <span className="sub" style={{ color: msg.ok ? "var(--ok)" : "var(--crim)" }}>{msg.text}</span>}
      </div>
      <div className="sub">只收公开资料；导入的条目仍经审核流程发布。评测结果只记题数、准确率和是否达标，不记题目和答案。</div>
    </form>
  );
}

export default function KbBatches() {
  const { user } = useAuth();
  const admin = hasRole(user, "knowledge_admin");
  const [list, setList] = useState(null);
  const [sources, setSources] = useState({ dirs: [], questions: [] });
  const [error, setError] = useState(null);

  const load = useCallback(() => api.kbBatches().then(setList).catch(setError), []);

  useEffect(() => {
    if (!admin) return;
    load();
    api.kbBatchSources().then(setSources).catch(() => setSources({ dirs: [], questions: [] }));
  }, [admin, load]);

  const active = (list || []).some((b) => b.status === "queued" || b.status === "running");
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [active, load]);

  if (!admin) {
    return (
      <section className="pnl pad" aria-label="无权限">
        <h1 className="h1">只有知识管理员能使用知识批次</h1>
        <div className="sub" style={{ fontSize: 13 }}>知识专家可以在“审批队列”处理导入后待审的条目。</div>
      </section>
    );
  }
  if (!list) return <Loading error={error} />;

  return (
    <>
      <TopBar cap="知识 · 后台批次" title="知识批次">
        <button className="btn sm" type="button" onClick={load}>刷新</button>
      </TopBar>
      <div className="grid-main">
        <section className="pnl pad" aria-label="批次列表">
          <div className="sb"><h2 className="h2">最近批次</h2><span className="sub">{active ? "有批次在跑，每 15 秒自动刷新" : `共 ${list.length} 个`}</span></div>
          <div style={{ overflowX: "auto" }}>
            <table className="tbl">
              <thead><tr><th>种类</th><th>状态</th><th>资料</th><th>计数</th><th>发起人</th><th>发起</th><th>完成</th></tr></thead>
              <tbody>
                {list.map((b, i) => {
                  const [label, cls] = STATUS[b.status] || [b.status, "c-n"];
                  return (
                    <tr key={b.job_id ? `job-${b.job_id}` : `cli-${i}`}>
                      <td>{b.name}{b.source === "cli" && <span className="chip c-n" style={{ marginLeft: 6 }}>命令行</span>}
                        {b.job_id && <span className="mono sub" style={{ marginLeft: 6 }}>#{b.job_id}</span>}</td>
                      <td><span className={`chip ${cls}`}>{label}</span>
                        {b.error && <div className="sub" style={{ color: "var(--crim)" }}>{b.error}</div>}</td>
                      <td className="mono sub">{b.path || "—"}</td>
                      <td><Counts b={b} /></td>
                      <td>{b.created_by}</td>
                      <td className="mono sub">{when(b.created_at)}</td>
                      <td className="mono sub">{when(b.finished_at)}</td>
                    </tr>
                  );
                })}
                {list.length === 0 && <tr><td colSpan={7} className="sub">还没有批次记录</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="sub">页面发起的批次看后台任务状态；命令行跑的切条、导入、起草、评测、依据核对、自动核验发布跑完后也列在这里。</div>
        </section>
        <StartForm sources={sources} onStarted={load} />
      </div>
    </>
  );
}
