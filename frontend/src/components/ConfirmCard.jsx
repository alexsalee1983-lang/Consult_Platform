import { useState } from "react";
import { api, KIND_CLS } from "../api.js";

// 一张确认卡：Agent 给出的选项、推荐和理由、影响；顾问选一个选项或写意见（规则见 app/project/confirmations.py）。
// 处理过的卡不能再处理；交付链在等这张卡时，卡都处理完会自动继续。
export default function ConfirmCard({ c, onDone, showProject = false }) {
  const [writing, setWriting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const options = c.options || [];

  async function submit(choice, resolution) {
    setBusy(true);
    setError("");
    try {
      await api.resolve(c.id, { choice, resolution });
      setWriting(false);
      setText("");
      await onDone?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const where = [showProject && c.project_code, c.deliverable_code, c.section_seq && `第 ${c.section_seq} 章`].filter(Boolean).join(" · ");
  return (
    <li className="card" aria-label={`确认卡：${c.title}`}>
      <div className="sb"><span className={`chip ${KIND_CLS[c.kind] || "c-n"}`}>{c.kind}</span><span className="sub">{c.agent}</span></div>
      <div style={{ fontWeight: 600 }}>{c.title}</div>
      {where && <div className="sub mono">{where}</div>}
      {options.length > 0 && (
        <div className="opts">
          {options.map((o) => (
            <div key={o.key} className={`opt${o.key === c.recommendation ? " rec" : ""}`}>
              <span className="mono">{o.key}</span><span>{o.text}</span>
              {o.key === c.recommendation && <span className="chip c-a">推荐</span>}
            </div>
          ))}
        </div>
      )}
      {c.reason && <div className="sub">理由：{c.reason}</div>}
      {c.impact && <div className="sub">影响：{c.impact}</div>}
      {error && <div className="err" role="alert">{error}</div>}
      {writing ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <textarea className="inp" rows={3} maxLength={2000} value={text} placeholder="写下你的意见或改写后的做法" onChange={(e) => setText(e.target.value)} />
          <div className="row">
            <button className="btnp" disabled={busy || !text.trim()} onClick={() => submit("", text.trim())}>提交意见</button>
            <button className="btn" disabled={busy} onClick={() => setWriting(false)}>取消</button>
          </div>
        </div>
      ) : (
        <div className="row" style={{ flexWrap: "wrap" }}>
          {options.map((o) => (
            <button key={o.key} className={o.key === c.recommendation ? "btnp" : "btn"} disabled={busy} onClick={() => submit(o.key, "")}>
              {o.key === c.recommendation ? `采纳 ${o.key}` : `选 ${o.key}`}
            </button>
          ))}
          <button className="btn" disabled={busy} onClick={() => setWriting(true)}>{options.length ? "我来改写" : "写意见"}</button>
          {!options.length && <button className="btn" disabled={busy} onClick={() => submit("", "已确认")}>确认</button>}
        </div>
      )}
    </li>
  );
}
