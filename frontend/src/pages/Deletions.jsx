import { useCallback, useEffect, useState } from "react";
import { api, DELETION_COUNTS, DELETION_KIND, DELETION_STATUS, FLOWBACK } from "../api.js";
import { TopBar } from "../components/common.jsx";
import { fmtDate } from "../components/kb.jsx";

const FILTERS = [["open", "待处理"], ["done", "已删除"], ["cancelled", "已撤销"], ["all", "全部"]];

function Counts({ counts }) {
  const rows = Object.entries(DELETION_COUNTS).filter(([k]) => counts?.[k]);
  if (!rows.length) return <div className="sub">没有要删除的内容。</div>;
  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
      {rows.map(([k, name]) => <span key={k} className="chip c-n">{name} {counts[k]}</span>)}
    </div>
  );
}

// 可交给客户的删除记录：只有编号、日期、计数和回流情况，不含客户内容
function Record({ rec }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }} className="sub">
      <span>删除记录 #{rec.deletion_id} · 项目 <span className="mono">{rec.project_code}</span> · {rec.kind_name}</span>
      {rec.request_ref && <span>客户书面要求 {rec.request_ref} · 收到日 {rec.received_on}</span>}
      {rec.accepted_on && <span>验收日 {rec.accepted_on} · 保留 {rec.retention_months} 个月 · 到期日 {rec.deletion_due_on}</span>}
      <span>应完成 {rec.due_by} · 完成 {fmtDate(rec.completed_at)} · {rec.on_time ? "按期完成" : "逾期完成"}</span>
      <span>确认人 {rec.confirmed_by} · 经验回流：{rec.flowback_name}
        {rec.flowback_refs?.length ? `（投料编号 ${rec.flowback_refs.join("、")}）` : ""}</span>
      <Counts counts={rec.counts} />
      <span>已删除：{rec.scope.join("；")}</span>
      <span>保留：{rec.kept.join("；")}</span>
      {rec.backups_purged_by && <span>备份中的副本随备份轮换清除，最迟 {rec.backups_purged_by}</span>}
    </div>
  );
}

function Detail({ id, onChange }) {
  const [d, setD] = useState(null);
  const [flowback, setFlowback] = useState("");
  const [refs, setRefs] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => api.deletion(id).then((x) => {
    setD(x);
    setFlowback(x.flowback_allowed ? "" : "client_refused");
  }).catch((e) => setError(e.message)), [id]);
  useEffect(() => { load(); }, [load]);

  async function act(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await load();
      onChange();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const refList = refs.split(/[\s,，、]+/).filter(Boolean).map(Number);
  const refsOk = flowback !== "done" || (refList.length > 0 && refList.every((n) => Number.isInteger(n) && n > 0));

  if (!d) return error ? <div className="err" role="alert">{error}</div> : null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, paddingTop: 8 }}>
      {d.status === "pending" && (
        <>
          <div className="sub">确认后平台删除以下内容（不可恢复）。已签署的交付件也会删除，客户需要的版本请提前导出。</div>
          <Counts counts={d.pending_counts} />
          <label className="sub">经验回流情况（必选，不影响删除）
            <select className="inp" value={flowback} disabled={!d.flowback_allowed} onChange={(e) => setFlowback(e.target.value)}>
              <option value="">请选择</option>
              {Object.entries(FLOWBACK).map(([k, v]) => (
                <option key={k} value={k} disabled={!d.flowback_allowed && k !== "client_refused"}>{v}</option>))}
            </select>
          </label>
          {!d.flowback_allowed && <div className="sub">项目经理已标记客户不同意回流，只能选“客户不同意回流”。</div>}
          {flowback === "done" && (
            <label className="sub">投料编号（投料箱里的编号，多个用逗号分隔）
              <input className="inp" value={refs} onChange={(e) => setRefs(e.target.value)} placeholder="如 12, 15" />
            </label>
          )}
          <div className="row">
            <button className="btnp" disabled={busy || !flowback || !refsOk}
              onClick={() => act(() => api.confirmDeletion(d.id, flowback, flowback === "done" ? refList : []))}>
              确认删除
            </button>
          </div>
          {d.kind === "early" && (
            <div className="row">
              <input className="inp" style={{ flex: 1 }} maxLength={200} value={reason}
                placeholder="撤销理由（如客户书面撤回），不写客户名称" onChange={(e) => setReason(e.target.value)} />
              <button className="btn" disabled={busy || reason.trim().length < 4}
                onClick={() => act(() => api.cancelDeletion(d.id, reason.trim()))}>撤销申请</button>
            </div>
          )}
        </>
      )}
      {d.status === "confirmed" && <div className="sub">已确认（{d.confirmed_by}），删除正在后台执行。</div>}
      {d.status === "cancelled" && <div className="sub">已撤销（{d.cancelled_by}）：{d.cancel_reason || "保留期已更正"}</div>}
      {d.record && <Record rec={d.record} />}
      {error && <div className="err" role="alert">{error}</div>}
    </div>
  );
}

// 项目负责人：项目资料删除确认队列（M4 ②）
export default function Deletions() {
  const [filter, setFilter] = useState("open");
  const [rows, setRows] = useState(null);
  const [openId, setOpenId] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(() => api.deletions(filter).then(setRows).catch((e) => setError(e.message)), [filter]);
  useEffect(() => { load(); }, [load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="项目负责人" title="项目资料删除">
        {FILTERS.map(([k, v]) => (
          <button key={k} className={k === filter ? "btnp sm" : "btn sm"} onClick={() => setFilter(k)}>{v}</button>))}
      </TopBar>
      <div className="sub">保留期到期由平台自动发起；客户书面要求提前删除由项目经理发起，10 个工作日内完成。发起人不能自己确认。</div>
      {error && <div className="err" role="alert">{error}</div>}
      {rows && !rows.length && <section className="pnl pad sub">没有删除申请。</section>}
      {rows?.map((d) => {
        const st = DELETION_STATUS[d.status] || DELETION_STATUS.pending;
        return (
          <section key={d.id} className="pnl pad" aria-label={`删除申请 ${d.id}`}>
            <div className="sb">
              <div className="row" style={{ flexWrap: "wrap" }}>
                <span className="mono">{d.project_code}</span>
                <span className={`chip ${st.cls}`}>{st.label}</span>
                <span className="chip c-n">{d.tier === "strict" ? "严格档" : "标准档"}</span>
                <span className="sub">{DELETION_KIND[d.kind]} · 发起 {d.requested_by === "platform" ? "平台" : d.requested_by}
                  · 应完成 {d.due_by}</span>
                {d.overdue && <span className="chip c-r">已逾期</span>}
                {!d.flowback_allowed && <span className="chip c-n">客户不同意回流</span>}
              </div>
              <button className="btn sm" onClick={() => setOpenId(openId === d.id ? null : d.id)}>
                {openId === d.id ? "收起" : "查看"}
              </button>
            </div>
            {openId === d.id && <Detail id={d.id} onChange={load} />}
          </section>
        );
      })}
    </div>
  );
}
