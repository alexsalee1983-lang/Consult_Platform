import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";

// 我的任务（三期 B17②）：七类待办合一，按影响 × 不确定度排好；超出每日额度的折叠；拿不准的字段标黄；可退回由 Agent 重写
function Fields({ fields }) {
  if (!fields?.length) return null;
  return (
    <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 4 }}>
      {fields.map((f) => (
        <span key={f.name} className={`chip ${f.uncertain ? "c-a" : "c-n"}`} title={f.uncertain ? "拿不准，请核对" : undefined}>
          {f.label}：{f.value || "待补"}
        </span>
      ))}
    </div>
  );
}

function TaskRow({ t, kindName, onReturned }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.returnTask(t.kind, t.ref_id, reason.trim());
      onReturned();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <li style={t.blocked ? { opacity: 0.55 } : undefined}>
      <div className="sb">
        <span>
          <span className="chip c-n">{kindName}</span>{" "}
          {t.project_code && <span className="mono sub">{t.project_code} · </span>}{t.title}
          {t.uncertainty > 0 && <> <span className="chip c-a">存疑</span></>}
        </span>
        <span className="row" style={{ gap: 6 }}>
          <span className="sub mono" title="影响 × 不确定度">影响 {t.impact} · 分 {t.score}</span>
          {t.blocked ? <span className="sub">{t.blocked}</span> : (
            <>
              {t.returnable && <button className="btn sm" onClick={() => setOpen((v) => !v)}>退回</button>}
              <Link className="btn sm" to={t.link}>去处理</Link>
            </>
          )}
        </span>
      </div>
      <Fields fields={t.fields} />
      {open && (
        <div className="row" style={{ gap: 6, marginTop: 6 }}>
          <input className="inp" style={{ flex: 1 }} maxLength={200} placeholder="退回意见（交 Agent 重写，勿写客户原文）"
            value={reason} onChange={(e) => setReason(e.target.value)} />
          <button className="btnp sm" disabled={busy || !reason.trim()} onClick={submit}>确认退回</button>
        </div>
      )}
      {err && <div className="err" role="alert">{err}</div>}
    </li>
  );
}

export default function MyTasks() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [showFolded, setShowFolded] = useState(false);
  const load = useCallback(() => { api.myTasks().then(setD).catch((e) => setError(e.message)); }, []);
  useEffect(load, [load]);
  if (error) return <section className="pnl pad err" role="alert">{error}</section>;
  if (!d) return <section className="pnl pad sub">加载中…</section>;
  const name = (k) => d.kinds[k]?.name || k;
  const today = d.tasks.filter((t) => !t.blocked && !t.folded);
  const later = d.tasks.filter((t) => !t.blocked && t.folded);
  const blocked = d.tasks.filter((t) => t.blocked);
  const list = (rows) => (
    <ul className="lst">{rows.map((t) => <TaskRow key={`${t.kind}-${t.ref_id}`} t={t} kindName={name(t.kind)} onReturned={load} />)}</ul>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="七类待办合一，按影响 × 不确定度排序" title="我的任务" />
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
        {Object.entries(d.kinds).map(([k, v]) => (
          <span key={k} className="chip c-n" style={v.available ? undefined : { opacity: 0.55 }}>
            {v.name} {v.available ? v.count : "未接入"}
          </span>
        ))}
      </div>
      <section className="pnl pad" aria-label="今天">
        <div className="sb"><h2 className="h2">今天 <span className="sub mono">{today.length}/{d.quota}</span></h2>
          <span className="sub">每日额度 {d.quota} 项；标黄的字段拿不准，请核对</span></div>
        {today.length ? list(today) : <div className="sub">没有待你处理的事项。</div>}
      </section>
      {later.length > 0 && (
        <section className="pnl pad" aria-label="超出额度">
          <div className="sb"><h2 className="h2">超出今日额度 <span className="sub mono">{later.length}</span></h2>
            <button className="btn sm" onClick={() => setShowFolded((v) => !v)}>{showFolded ? "收起" : "展开"}</button></div>
          {showFolded && list(later)}
        </section>
      )}
      {blocked.length > 0 && (
        <section className="pnl pad" aria-label="须由别人处理">
          <div className="sb"><h2 className="h2">须由别人处理 <span className="sub mono">{blocked.length}</span></h2>
            <span className="sub">按规则你不能处理，列出供了解</span></div>
          {list(blocked)}
        </section>
      )}
    </div>
  );
}
