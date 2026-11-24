import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";
import {
  ACTIONS, CATEGORIES, LEVELS, LEVEL_CLS, NOTE_MAX, REF_KINDS, ROOT_CAUSES, STEPS, STEP_LABEL,
  dueText, eventPayload, nextStep, statusLabel, validateEvent,
} from "../qualityIncidents.js";

// AI 质量事件台账与处置（四期 F3②）：只给项目负责人。描述与处置说明只记长度和摘要哈希，页面不回显原文。
const STATUS_FILTER = [["", "全部"], ["open", "未关闭"], ["closed", "已关闭"]];

function fmt(t) {
  return t ? new Date(t).toLocaleString("zh-CN", { hour12: false }) : "—";
}

function Summary({ s }) {
  if (!s) return null;
  return (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      <span className="chip c-c">未关闭 {s.open}</span>
      {LEVELS.map((lv) => <span key={lv} className={`chip ${LEVEL_CLS[lv]}`}>{lv} {s.by_level?.[lv] ?? 0}</span>)}
      <span className="chip c-n">未分级 {s.by_level?.ungraded ?? 0}</span>
      <span className={`chip ${s.overdue ? "c-r" : "c-g"}`}>遏制超时 {s.overdue}</span>
    </div>
  );
}

function EventForm({ inc, onDone }) {
  const suggested = nextStep(inc.events || []);
  const [f, setF] = useState({ step: suggested || "assess", level: "", action: "", root_cause: "", note: "" });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  if (inc.status === "closed") return <div className="sub">已复盘关闭，处置记录只追加、不能再改。</div>;
  const submit = async () => {
    const msg = validateEvent(f);
    if (msg) return setError(msg);
    if (f.step === "review" && !window.confirm("复盘关闭后不能再追加处置，确定关闭？")) return;
    setBusy(true);
    setError(null);
    try {
      await api.addIncidentEvent(inc.id, eventPayload(f));
      setF({ step: "assess", level: "", action: "", root_cause: "", note: "" });
      onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        <select className="inp" aria-label="处置步骤" value={f.step} onChange={set("step")}>
          {STEPS.slice(1).map((s) => (
            <option key={s.key} value={s.key}>{s.label}{s.key === suggested ? "（建议）" : ""}</option>
          ))}
        </select>
        {f.step === "grade" && (
          <select className="inp" aria-label="分级" value={f.level} onChange={set("level")}>
            <option value="">选分级</option>
            {LEVELS.map((lv) => <option key={lv} value={lv}>{lv}</option>)}
          </select>
        )}
        {f.step === "contain" && (
          <select className="inp" aria-label="遏制措施" value={f.action} onChange={set("action")}>
            <option value="">选遏制措施</option>
            {Object.entries(ACTIONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        )}
        {f.step === "root_cause" && (
          <select className="inp" aria-label="根因类别" value={f.root_cause} onChange={set("root_cause")}>
            <option value="">选根因类别</option>
            {Object.entries(ROOT_CAUSES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        )}
      </div>
      <textarea className="inp" style={{ height: 72, padding: 8 }} maxLength={NOTE_MAX} value={f.note}
        onChange={set("note")} placeholder="处置说明（只存长度与摘要哈希，不存原文；不要写客户原文和机密级内容）" />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button className="btnp" disabled={busy} onClick={submit}>追加处置</button>
        {error && <span className="err" role="alert">{error}</span>}
      </div>
    </div>
  );
}

function Detail({ id, onChanged }) {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(() => {
    setError(null);
    api.qualityIncident(id).then(setD).catch((e) => setError(e.message));
  }, [id]);
  useEffect(() => { setD(null); load(); }, [load]);
  if (error) return <section className="pnl pad err" role="alert">{error}</section>;
  if (!d) return <section className="pnl pad sub">加载中…</section>;
  return (
    <section className="pnl pad" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <b>事件 #{d.id}</b>
        <span className="chip c-v">{d.category_label || CATEGORIES[d.category] || d.category}</span>
        {d.level ? <span className={`chip ${LEVEL_CLS[d.level]}`}>{d.level}</span> : <span className="chip c-n">未分级</span>}
        <span className={`chip ${d.status === "closed" ? "c-g" : "c-a"}`}>{statusLabel(d.status)}</span>
        <span className={`chip ${d.overdue ? "c-r" : "c-n"}`}>{dueText(d)}</span>
      </div>
      <div className="sub">
        挂在 {REF_KINDS[d.ref_kind] || d.ref_kind} <span className="mono">{d.ref_id}</span>
        {d.project && <> · 项目 <span className="mono">{d.project}</span></>} · 登记人 {d.reported_by} · {fmt(d.created_at)}
        · 描述 {d.description_len} 字 <span className="mono">{(d.description_sha || "").slice(0, 12)}</span>
      </div>
      <div>
        <div className="sub">影响面</div>
        {d.impact?.length ? d.impact.map((x) => (
          <Link key={`${x.project}/${x.deliverable}`} className="mono" style={{ marginRight: 12 }}
            to={`/project/${encodeURIComponent(x.project)}/deliverables/${encodeURIComponent(x.deliverable)}`}>
            {x.project}/{x.deliverable}
          </Link>
        )) : <span className="sub">没有查到受影响的交付件</span>}
      </div>
      <table className="tbl">
        <thead><tr><th>步骤</th><th>内容</th><th>说明</th><th>处置人</th><th>时间</th></tr></thead>
        <tbody>
          {(d.events || []).map((e, i) => (
            <tr key={i}>
              <td>{STEP_LABEL[e.step] || e.step}</td>
              <td>{e.level || ACTIONS[e.action] || ROOT_CAUSES[e.root_cause] || "—"}</td>
              <td className="sub">{e.note_len ? <>{e.note_len} 字 <span className="mono">{(e.note_sha || "").slice(0, 12)}</span></> : "—"}</td>
              <td>{e.actor}</td>
              <td className="sub">{fmt(e.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <EventForm inc={d} onDone={() => { load(); onChanged(); }} />
    </section>
  );
}

export default function QualityIncidents() {
  const [status, setStatus] = useState("open");
  const [level, setLevel] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [sel, setSel] = useState(null);
  const load = useCallback(() => {
    setError(null);
    api.qualityIncidents(status, level).then(setData).catch((e) => setError(e.message));
  }, [status, level]);
  useEffect(() => { setData(null); load(); }, [load]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="项目负责人" title="AI 质量事件" />
      <div className="sub">按八步处置：登记 → 分级 → 遏制 → 影响评估 → 纠正 → 根因 → 预防 → 复盘关闭。P1 4 小时内遏制，P2 1 个工作日。处置记录只追加。</div>
      <Summary s={data?.summary} />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {STATUS_FILTER.map(([v, label]) => (
          <button key={v || "all"} className="btn sm" disabled={v === status} onClick={() => setStatus(v)}>{label}</button>
        ))}
        <select className="inp" aria-label="按分级过滤" value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">全部分级</option>
          {LEVELS.map((lv) => <option key={lv} value={lv}>{lv}</option>)}
        </select>
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {!data && !error && <section className="pnl pad sub">加载中…</section>}
      {data && (
        <section className="pnl pad">
          {data.items.length === 0 ? <div className="sub">没有符合条件的事件</div> : (
            <table className="tbl">
              <thead><tr><th>编号</th><th>类别</th><th>挂在</th><th>项目</th><th>分级</th><th>进度</th><th>遏制时限</th><th>登记</th></tr></thead>
              <tbody>
                {data.items.map((i) => (
                  <tr key={i.id} onClick={() => setSel(i.id)} style={{ cursor: "pointer" }} aria-selected={sel === i.id}>
                    <td className="mono">#{i.id}</td>
                    <td>{i.category_label || CATEGORIES[i.category] || i.category}</td>
                    <td>{REF_KINDS[i.ref_kind] || i.ref_kind} <span className="mono">{i.ref_id}</span></td>
                    <td className="mono">{i.project || "—"}</td>
                    <td>{i.level ? <span className={`chip ${LEVEL_CLS[i.level]}`}>{i.level}</span> : <span className="sub">未分级</span>}</td>
                    <td>{statusLabel(i.status)}</td>
                    <td><span className={i.overdue ? "err" : "sub"}>{dueText(i)}</span></td>
                    <td className="sub">{i.reported_by} · {fmt(i.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}
      {sel && <Detail id={sel} onChanged={load} />}
    </div>
  );
}
