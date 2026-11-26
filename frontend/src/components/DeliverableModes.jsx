// 四期 A26 交付件页：模式条、一键修订、客户意见录入、D3 批量签。复用交付件页的面板样式和确认卡。
import { useEffect, useState } from "react";
import { api } from "../api.js";
import { MODES, reviseNote } from "../delivModes.js";

export function ModeBar({ mode }) {
  return (
    <nav className="tabs" aria-label="交付件模式">
      {MODES.map((m, i) => (
        <button key={m} type="button" className={i === mode ? "on" : undefined} aria-current={i === mode ? "step" : undefined} disabled={i !== mode}>
          {"①②③④"[i]} {m}
        </button>
      ))}
    </nav>
  );
}

export function RevisePanel({ code, dcode, rv, editable, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!rv) return null;
  const running = ["queued", "running"].includes(rv.status);
  const note = reviseNote(rv);

  async function start() {
    setBusy(true);
    setError("");
    try {
      await api.revise(code, dcode);
      await onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="一键修订">
      <div className="sb"><span className="cap">一键修订</span><span className="sub">最多自动 {rv.max_rounds} 轮</span></div>
      <div className="sub">
        机械问题（用语、口径、格式、来源标注）待修 {rv.mechanical_open} 处
        {rv.pending_feedback > 0 ? `；已采纳的客户意见 ${rv.pending_feedback} 条待修订` : ""}。保密、矛盾、取舍类问题仍由你定。
      </div>
      {note && <div className={rv.handoff || rv.status === "failed" ? "note-amber" : "sub"} role="status">{note}</div>}
      {error && <div className="err" role="alert">{error}</div>}
      {editable && (
        <button className="btn" disabled={busy || running || (rv.mechanical_open === 0 && rv.pending_feedback === 0)} onClick={start}>
          {running ? "修订中…" : "一键修订机械问题"}
        </button>
      )}
    </section>
  );
}

const EMPTY_ITEM = { section: "", text: "" };

export function ClientFeedback({ code, dcode, sections, onChanged }) {
  const [items, setItems] = useState([{ ...EMPTY_ITEM }]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  const set = (i, k, v) => setItems((xs) => xs.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  const filled = items.filter((x) => x.text.trim());

  async function submit() {
    setBusy(true);
    setError("");
    setDone("");
    try {
      const r = await api.clientFeedback(code, dcode, filled.map((x) => ({ section: x.section ? Number(x.section) : null, text: x.text.trim() })));
      setItems([{ ...EMPTY_ITEM }]);
      setDone(`已录入 ${r.confirmations.length} 条客户意见，逐条确认采纳与否后自动生成修订稿`);
      await onChanged();
    } catch (e) {
      setError(e.detail?.summary || e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="客户意见">
      <span className="cap">录入客户意见</span>
      <div className="sub">每条意见生成一张确认卡；全部处理完后，采纳的意见自动进入修订稿，评审修订后重签，已签版本留存。</div>
      {items.map((x, i) => (
        <div key={i} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <select className="sm" aria-label={`第 ${i + 1} 条意见对应章节`} value={x.section} onChange={(e) => set(i, "section", e.target.value)}>
            <option value="">全文</option>
            {sections.map((s) => <option key={s.seq} value={s.seq}>第 {s.seq} 章 {s.title}</option>)}
          </select>
          <textarea className="inp" rows={2} maxLength={4000} aria-label={`第 ${i + 1} 条客户意见`} placeholder="客户意见（不要贴口令、IP、个人信息）"
            value={x.text} onChange={(e) => set(i, "text", e.target.value)} />
        </div>
      ))}
      {error && <div className="err" role="alert">{error}</div>}
      {done && <div className="sub" role="status">{done}</div>}
      <div className="row">
        <button className="btn" disabled={busy || items.length >= 20} onClick={() => setItems((xs) => [...xs, { ...EMPTY_ITEM }])}>再加一条</button>
        <button className="btnp" disabled={busy || filled.length === 0} onClick={submit}>录入客户意见</button>
      </div>
    </section>
  );
}

export function BatchSign({ onChanged }) {
  const [rows, setRows] = useState(null);
  const [picked, setPicked] = useState({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const load = () => api.batchCandidates().then(setRows).catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);
  if (!rows || rows.length === 0) return null;
  const chosen = rows.filter((r) => r.ready && picked[r.project]);

  async function sign() {
    setBusy(true);
    setError("");
    try {
      const res = await api.batchSign(chosen.map((r) => ({ project: r.project, deliverable: r.deliverable })));
      setResult(res);
      setPicked({});
      await load();
      await onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="D3 批量签">
      <span className="cap">D3 批量签（门禁 0 问题）</span>
      <ul className="lst">
        {rows.map((r) => (
          <li key={`${r.project}-${r.deliverable}`}>
            <label className="row">
              <input type="checkbox" disabled={!r.ready} checked={!!picked[r.project]} onChange={(e) => setPicked((p) => ({ ...p, [r.project]: e.target.checked }))} />
              <span>{r.project} · {r.deliverable} {r.title}</span>
            </label>
            {!r.ready && <div className="sub">{r.reasons.join("；")}</div>}
          </li>
        ))}
      </ul>
      {error && <div className="err" role="alert">{error}</div>}
      {result && <div className="sub" role="status">{result.map((x) => `${x.project} ${x.ok ? "已签" : `未签：${x.reason}`}`).join("；")}</div>}
      <button className="btnp" disabled={busy || chosen.length === 0} onClick={sign}>签署选中的 {chosen.length} 份</button>
    </section>
  );
}
