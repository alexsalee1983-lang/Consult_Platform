import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";
import { hasRole, ROLE_NAMES, useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";
import { filterChips, splitTasks, toggleFilter } from "../tasks/view.js";

// 我的任务（三期 B17②；四期试用反馈 11-25 并入原工作台）：平台唯一的待办入口。
// 各类待办合一，按影响 × 不确定度排好；超出每日额度的折叠；拿不准的字段标黄；可退回由 Agent 重写；
// 顶部按类型筛选。
// 四期 A29（《平台规划刷新设计》3.2）：知识侧待办也在这里；别人的卡直接隐藏，不置灰；卡片统一格式：
// 标题、情况 / 补充两栏、推荐按键在前、影响说明、预计分钟；顶部写待处理数、预计分钟与每日额度、本周 AI 已自动办完

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
    <li aria-label="任务卡">
      <div className="sb">
        <span>
          <span className="chip c-n">{kindName}</span>{" "}
          {t.project_code && <span className="mono sub">{t.project_code} · </span>}{t.title}
          {t.uncertainty > 0 && <> <span className="chip c-a">存疑</span></>}
        </span>
        <span className="sub mono" title="预计用时；影响 × 不确定度">约 {t.minutes} 分钟 · 分 {t.score}</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 4 }}>
        <div><div className="cap">情况</div><div className="sub">{t.situation || "—"}</div></div>
        <div><div className="cap">补充</div><div className="sub">{t.supplement || "—"}</div></div>
      </div>
      <Fields fields={t.fields} />
      <div className="sb" style={{ marginTop: 6 }}>
        <span className="sub">{t.impact_text}</span>
        <span className="row" style={{ gap: 6 }}>
          <Link className="btnp sm" to={t.link}>{t.action || "去处理"}</Link>
          {t.returnable && <button className="btn sm" onClick={() => setOpen((v) => !v)}>退回</button>}
        </span>
      </div>
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
  const { user } = useAuth();
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [showFolded, setShowFolded] = useState(false);
  const [filter, setFilter] = useState("");
  const load = useCallback(() => { api.myTasks().then(setD).catch((e) => setError(e.message)); }, []);
  useEffect(load, [load]);
  if (error) return <section className="pnl pad err" role="alert">{error}</section>;
  if (!d) return <section className="pnl pad sub">加载中…</section>;
  const name = (k) => d.kinds[k]?.name || k;
  const { today, later } = splitTasks(d.tasks, filter);
  const roles = (user?.roles || []).map((r) => ROLE_NAMES[r] || r).join("、");
  const list = (rows) => (
    <ul className="lst">{rows.map((t) => <TaskRow key={`${t.kind}-${t.ref_id}`} t={t} kindName={name(t.kind)} onReturned={load} />)}</ul>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap={roles ? `${roles} · 按影响 × 不确定度排序` : "按影响 × 不确定度排序"} title="我的任务" />
      <div className="row" aria-label="任务概况" style={{ gap: 16, flexWrap: "wrap" }}>
        <span>待处理 <b className="mono">{d.pending}</b> 项</span>
        <span>预计 <b className="mono">{d.minutes}</b> 分钟 · 每日额度 <span className="mono">{d.quota}</span> 项</span>
        <span>本周 AI 已自动办完 <b className="mono" style={{ color: "var(--ok)" }}>{d.auto_done}</b> 项</span>
      </div>
      <div className="row" role="group" aria-label="按类型筛选" style={{ gap: 6, flexWrap: "wrap" }}>
        {filterChips(d.kinds).map((c) => (
          <button key={c.key} type="button" className={filter === c.key ? "btnp sm" : "btn sm"} aria-pressed={filter === c.key}
            onClick={() => setFilter((f) => toggleFilter(f, c.key))}>
            {c.name} <span className="mono">{c.count}</span>
          </button>
        ))}
        {filter && <span className="sub">只看“{name(filter)}”，再点一次取消</span>}
      </div>
      {!d.member_projects && (
        <section className="pnl pad">
          {hasRole(user, "project_lead")
            ? <div className="row"><span className="sub">你还不是任何项目的成员。立项时把自己指定为项目经理，或请项目经理把你加入项目。</span>
                <Link className="btnp sm" to="/projects">新建项目</Link></div>
            : <span className="sub">你还不是任何项目的成员，请项目经理把你加入项目；知识检索和投料箱不需要项目。</span>}
        </section>
      )}
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
    </div>
  );
}
