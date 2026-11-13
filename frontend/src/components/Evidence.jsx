import { useEffect, useRef, useState } from "react";
import { api, EVIDENCE_STATUS } from "../api.js";
import { useAuth } from "../auth.jsx";

const TABS = [["all", "全部"], ["document", "资料"], ["interview", "访谈"], ["observation", "观察"]];

// 证据详情：纪要 / 观察显示正文，资料显示解析后的前若干节
function Detail({ code, ev, onChange }) {
  const { user } = useAuth();
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [isManager, setIsManager] = useState(false);
  const [marking, setMarking] = useState(false);
  const [ref, setRef] = useState("");
  const [busy, setBusy] = useState(false);
  const [markErr, setMarkErr] = useState("");
  useEffect(() => { api.evidence(code, ev.code).then(setD).catch(setError); }, [code, ev.code, ev.status]);
  useEffect(() => {
    if (ev.kind !== "document") return;
    api.members(code).then((ms) => setIsManager(ms.some((m) => m.username === user?.username && m.project_role === "manager")))
      .catch(() => setIsManager(false));
  }, [code, ev.kind, user]);
  const mark = () => {
    setBusy(true);
    setMarkErr("");
    api.markLocalOnly(code, ev.code, ref.trim()).then((x) => { setD(null); setMarking(false); onChange(x); })
      .catch((e) => setMarkErr(e.message)).finally(() => setBusy(false));
  };
  if (error) return <div className="err sub">{error.message}</div>;
  if (!d) return <div className="sub">加载中…</div>;
  const meta = [d.kind_name, d.interviewee_role && `受访者：${d.interviewee_role}`, d.occurred_on, d.filename,
    `${d.chars} 字`, `录入：${d.created_by}`].filter(Boolean).join(" · ");
  return (
    <div className="ev-detail">
      <div className="sub">{meta}</div>
      {d.error && <div className="err sub">解析失败：{d.error}</div>}
      {d.content && <div className="ev-text">{d.content}</div>}
      {d.preview.map((s, i) => (
        <div key={i}>{s.heading && <b>{s.heading}</b>}<div className="ev-text">{s.text}</div></div>
      ))}
      {d.kind === "document" && d.status === "received" && <div className="sub">资料正在后台解析，稍后刷新。</div>}
      {d.local_only && (
        <div className="sub" style={{ color: "var(--vio)" }}>
          仅现场使用（客户通知 {d.local_only_ref} · {d.local_only_by} · {d.local_only_at?.slice(0, 10)}）：平台已删除原件和解析结果，
          只能在客户现场查阅；Agent 读不到，交付件不能引用。
        </div>
      )}
      {d.kind === "document" && !d.local_only && isManager && !marking && (
        <div className="row"><button className="btn sm" onClick={() => setMarking(true)}>标为仅现场使用</button></div>
      )}
      {marking && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div className="sub">客户书面要求这份资料只在现场查阅时使用。标注后平台立即删除原件和解析结果，不可撤销；
            同一文件不能再上传，交付件里已有的引用要删去后重新评审。</div>
          <label className="sub">客户书面通知的文号（不写客户名称）
            <input className="inp" maxLength={64} value={ref} onChange={(e) => setRef(e.target.value)} />
          </label>
          {markErr && <div className="err sub" role="alert">{markErr}</div>}
          <div className="row">
            <button className="btnp" disabled={busy || !ref.trim()} onClick={mark}>确认标注并删除文件</button>
            <button className="btn" disabled={busy} onClick={() => setMarking(false)}>取消</button>
          </div>
        </div>
      )}
    </div>
  );
}

function NoteForm({ code, onSaved, onCancel }) {
  const [f, setF] = useState({ kind: "interview", title: "", interviewee_role: "", occurred_on: "", content: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const ok = f.title.trim() && f.content.trim() && (f.kind !== "interview" || f.interviewee_role.trim());
  const save = () => {
    setBusy(true);
    setError(null);
    api.addNote(code, { ...f, occurred_on: f.occurred_on || null,
      interviewee_role: f.kind === "interview" ? f.interviewee_role : "" })
      .then(onSaved).catch(setError).finally(() => setBusy(false));
  };
  return (
    <div className="ev-form">
      <div className="row">
        <select className="inp" value={f.kind} onChange={set("kind")} aria-label="类别">
          <option value="interview">访谈纪要</option><option value="observation">现场观察</option>
        </select>
        <input className="inp" style={{ flex: 1 }} placeholder="标题" value={f.title} onChange={set("title")} maxLength={200} />
        <input className="inp" type="date" value={f.occurred_on} onChange={set("occurred_on")} aria-label="日期" />
      </div>
      {f.kind === "interview" && (
        <input className="inp" placeholder="受访者角色（只写角色，如“SOC 负责人”，不写姓名）" value={f.interviewee_role}
          onChange={set("interviewee_role")} maxLength={100} />
      )}
      <textarea className="inp" rows={6} placeholder="正文。不要写口令、密钥、可利用的漏洞细节、完整 IP 规划和个人信息样本。"
        value={f.content} onChange={set("content")} />
      {error && <div className="err sub">{error.message}</div>}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn sm" onClick={onCancel}>取消</button>
        <button className="btnp" disabled={!ok || busy} onClick={save}>{busy ? "保存中…" : "保存"}</button>
      </div>
    </div>
  );
}

// 项目证据：列表、详情、上传客户资料、录入访谈纪要和现场观察
export default function Evidence({ code, strict, items, onChange }) {
  const [tab, setTab] = useState("all");
  const [open, setOpen] = useState(null);
  const [mode, setMode] = useState(null);       // null / note
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);

  const upload = (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setBusy(true);
    setMsg(null);
    api.uploadDocument(code, file).then((ev) => { setMsg({ ok: `已收到，编号 ${ev.code}，正在后台解析` }); onChange(); })
      .catch((err) => setMsg({ err: err.message })).finally(() => setBusy(false));
  };

  const shown = (items || []).filter((e) => tab === "all" || e.kind === tab);
  return (
    <section className="pnl pad" aria-label="项目证据">
      <div className="sb">
        <h2 className="h2">项目证据</h2>
        {!strict && (
          <div className="row">
            <button className="btn sm" disabled={busy} onClick={() => fileRef.current?.click()}>{busy ? "上传中…" : "上传客户资料"}</button>
            <button className="btn sm" onClick={() => setMode(mode === "note" ? null : "note")}>录入纪要 / 观察</button>
            <input ref={fileRef} type="file" hidden onChange={upload} accept=".docx,.pdf,.xlsx,.pptx,.txt,.md,.csv" />
          </div>
        )}
      </div>
      {strict && <div className="sub">严格档：资料不出客户现场，平台不收客户资料和纪要，只收去标识摘要卡。</div>}
      {msg && <div className={msg.err ? "err sub" : "sub"} style={msg.ok ? { color: "var(--ok)" } : undefined}>{msg.err || msg.ok}</div>}
      {mode === "note" && (
        <NoteForm code={code} onCancel={() => setMode(null)}
          onSaved={(ev) => { setMode(null); setMsg({ ok: `已保存，编号 ${ev.code}` }); onChange(); }} />
      )}
      <div className="tabs">
        {TABS.map(([k, n]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {n} {(items || []).filter((e) => k === "all" || e.kind === k).length}
          </button>
        ))}
      </div>
      <ul className="lst ev-lst">
        {shown.map((e) => {
          const st = EVIDENCE_STATUS[e.status] || EVIDENCE_STATUS.ready;
          return (
            <li key={e.code}>
              <button className="ev-row" onClick={() => setOpen(open === e.code ? null : e.code)} aria-expanded={open === e.code}>
                <span className="mono sub ev-code">{e.code}</span>
                <span className="ev-title">{e.title || e.filename}{e.interviewee_role && <span className="sub"> · {e.interviewee_role}</span>}</span>
                <span className="sub mono">{e.occurred_on || e.created_at.slice(0, 10)}</span>
                <span className={`chip ${st.cls}`}>{st.label}</span>
              </button>
              {open === e.code && <Detail code={code} ev={e} onChange={onChange} />}
            </li>
          );
        })}
        {items && shown.length === 0 && <li className="sub">还没有证据</li>}
        {!items && <li className="sub">加载中…</li>}
      </ul>
    </section>
  );
}
