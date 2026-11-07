import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";

const ACTIONS = [
  ["", "全部操作"], ["auth.", "登录与退出"], ["user.", "账号与角色"], ["llm.", "模型调用"],
  ["guard.", "机密级检查"], ["project.", "项目成员"], ["confirmation.", "待确认事项"], ["audit.", "审计操作"],
];
const RESULTS = [["", "全部结果"], ["allowed", "通过"], ["blocked", "拒绝 / 拦截"], ["error", "错误"]];
const RESULT_CLS = { allowed: "c-g", blocked: "c-r", error: "c-a" };
const RESULT_LABEL = { allowed: "通过", blocked: "拒绝", error: "错误" };
const PAGE = 50;

function fmt(at) {
  return new Date(at).toLocaleString("zh-CN", { hour12: false });
}

function VerifyPanel() {
  const [expect, setExpect] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run() {
    setBusy(true);
    setError("");
    try {
      setResult(await api.verifyAudit(expect.trim()));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="哈希链校验">
      <div className="sb"><h2 className="h2">哈希链校验</h2>
        {result && <span className={`chip ${result.ok ? "c-g" : "c-r"}`}>{result.ok ? "完整" : "发现问题"}</span>}</div>
      <div className="sub">每条审计记录都含上一条的哈希，任何一条被改、被删或被插入都会在这里报出。
        只删末尾的记录链本身仍自洽，所以请记下每次的链头哈希，下次填在下面一起核对。</div>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <label className="field" style={{ flex: 1, minWidth: 260 }}>
          <span>上次记下的链头哈希（可选）</span>
          <input className="mono" style={{ letterSpacing: 0 }} value={expect} placeholder="64 位十六进制"
            onChange={(e) => setExpect(e.target.value.trim().toLowerCase())} />
        </label>
        <button className="btnp" type="button" onClick={run} disabled={busy || (expect && !/^[0-9a-f]{64}$/.test(expect))}
          style={{ alignSelf: "flex-end", height: 36 }}>{busy ? "校验中……" : "校验"}</button>
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {result && (
        <div role="status" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {!result.ok && <div className="err">{result.error}{result.error_id ? `（记录 #${result.error_id}）` : ""}</div>}
          <div className="sub">链上记录 {result.checked} 条 · 链启用前的旧记录 {result.legacy} 条</div>
          {result.head_hash && (
            <div className="sub">本次链头：#{result.head_id}　<span className="mono" style={{ color: "var(--tx2)", wordBreak: "break-all" }}>{result.head_hash}</span></div>
          )}
        </div>
      )}
    </section>
  );
}

export default function Audit() {
  // draft 是输入框里的值；filters 是生效的查询条件（文本框回车生效，下拉框选择即生效）
  const [draft, setDraft] = useState({ actor: "", action: "", result: "", project_code: "" });
  const [filters, setFilters] = useState(draft);
  const [rows, setRows] = useState([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (beforeId) => {
    setLoading(true);
    setError("");
    try {
      const page = await api.audit({ ...filters, limit: PAGE, before_id: beforeId });
      setRows((r) => (beforeId ? [...r, ...page] : page));
      setMore(page.length === PAGE);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => { load(); }, [load]);

  const set = (k) => (e) => setDraft((f) => ({ ...f, [k]: e.target.value }));
  const pick = (k) => (e) => {
    const next = { ...draft, [k]: e.target.value };
    setDraft(next);
    setFilters(next);
  };

  return (
    <>
      <TopBar cap="安全审计 · 只读" title="审计日志" />
      <VerifyPanel />
      <section className="pnl pad" aria-label="审计记录">
        <form className="row" style={{ flexWrap: "wrap", alignItems: "flex-end" }} onSubmit={(e) => { e.preventDefault(); setFilters({ ...draft }); }}>
          <label className="field"><span>操作人</span><input value={draft.actor} onChange={set("actor")} /></label>
          <label className="field"><span>操作</span>
            <select value={draft.action} onChange={pick("action")}>{ACTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          </label>
          <label className="field"><span>结果</span>
            <select value={draft.result} onChange={pick("result")}>{RESULTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          </label>
          <label className="field"><span>项目代号</span><input value={draft.project_code} onChange={set("project_code")} /></label>
          <button className="btn" type="submit" style={{ height: 36 }}>查询</button>
        </form>
        {error && <div className="err" role="alert">{error}</div>}
        <div style={{ overflowX: "auto" }}>
          <table className="tbl">
            <thead>
              <tr><th>#</th><th>时间</th><th>操作人</th><th>操作</th><th>结果</th><th>项目</th><th>详情（不含原文）</th><th>哈希</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.id}</td>
                  <td style={{ whiteSpace: "nowrap" }}>{fmt(r.at)}</td>
                  <td>{r.actor}</td>
                  <td className="mono">{r.action}</td>
                  <td><span className={`chip ${RESULT_CLS[r.result] || "c-n"}`}>{RESULT_LABEL[r.result] || r.result}</span></td>
                  <td className="mono">{r.project_code}</td>
                  <td className="mono detail">{r.detail}</td>
                  <td className="mono" title={r.hash || "哈希链启用前的旧记录"}>{r.hash ? r.hash.slice(0, 10) : "链前"}</td>
                </tr>
              ))}
              {!loading && rows.length === 0 && <tr><td colSpan={8} className="sub">没有符合条件的记录</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="row">
          {more && <button type="button" className="btn" disabled={loading} onClick={() => load(rows[rows.length - 1]?.id)}>加载更早的记录</button>}
          {loading && <span className="sub">加载中……</span>}
        </div>
      </section>
    </>
  );
}
