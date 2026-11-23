// 写稿助手 v1（三期 A15②）：交付件编辑页侧栏。问 / 改切换、快 / 深两档、生成中可停止；
// 自动上下文 = 编辑中的正文（未保存的也算）+ 选中的文字，由后端补交付件和章节标题、项目卡、知识库检索。
// “插入”把回复原样放到光标处或替换选区（来源标注保留），之后照常保存为新版本、过签署门禁；助手不直接写交付件。
import { useRef, useState } from "react";
import { assistSection } from "../api.js";
import { sourcesLine } from "../editor/assist.js";

const MODES = [["ask", "问"], ["edit", "改"]];
const SPEEDS = [["fast", "快"], ["deep", "深"]];

function Seg({ items, value, onChange, label, disabled }) {
  return (
    <div className="row assist-seg" role="radiogroup" aria-label={label}>
      {items.map(([k, name]) => (
        <button key={k} type="button" role="radio" aria-checked={value === k} className={`btn sm${value === k ? " on" : ""}`}
          disabled={disabled} onClick={() => onChange(k)}>{name}</button>
      ))}
    </div>
  );
}

export default function AssistPanel({ code, dcode, seq, getContext, getSelection, onInsert }) {
  const [mode, setMode] = useState("ask");
  const [speed, setSpeed] = useState("fast");
  const [prompt, setPrompt] = useState("");
  const [reply, setReply] = useState("");
  const [done, setDone] = useState(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [stopped, setStopped] = useState(false);
  const ctrl = useRef(null);

  async function send() {
    const selection = getSelection() || "";
    const c = new AbortController();
    ctrl.current = c;
    setReply("");
    setDone(null);
    setError("");
    setStopped(false);
    setRunning(true);
    try {
      const d = await assistSection(code, dcode, seq, { mode, speed, prompt, context: getContext(), selection },
        (t) => setReply((r) => r + t), c.signal);
      setDone(d);
      setReply(d.text);
    } catch (e) {
      if (e.name === "AbortError") setStopped(true);
      else setError(e.message);
    } finally {
      setRunning(false);
      ctrl.current = null;
    }
  }

  function stop() {
    ctrl.current?.abort();
  }

  const canInsert = !running && reply.trim() !== "";
  return (
    <aside className="pnl assist" aria-label="写稿助手">
      <div className="sb">
        <b>写稿助手</b>
        <span className="sub">{mode === "ask" ? "问本章、问做法" : "按要求改写选中文字或本章"}</span>
      </div>
      <div className="row" style={{ gap: 12 }}>
        <Seg items={MODES} value={mode} onChange={setMode} label="问或改" disabled={running} />
        <Seg items={SPEEDS} value={speed} onChange={setSpeed} label="快慢档" disabled={running} />
      </div>
      <textarea className="inp" rows={4} maxLength={2000} value={prompt} disabled={running}
        placeholder={mode === "ask" ? "例如：夜间值守的差距怎么写更有依据？" : "例如：改得更简洁，保留来源标注"}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && prompt.trim() && !running) send(); }} />
      <div className="row">
        {running
          ? <button className="btn" onClick={stop}>停止</button>
          : <button className="btnp" disabled={!prompt.trim()} onClick={send}>{mode === "ask" ? "提问" : "改写"}</button>}
        <span className="sub">{speed === "fast" ? "快档：轻量模型，先出结果" : "深档：推理模型，更慢更细"}</span>
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {(reply || running) && (
        <div className="assist-out" aria-live="polite">
          <div className="mono assist-text">{reply || "生成中…"}</div>
          {stopped && <div className="sub">已停止，以上为停止前的部分。</div>}
          {done && <div className={done.sources?.unresolved?.length ? "assist-bad" : "sub"}>{sourcesLine(done.sources)}</div>}
          {done?.demo && <div className="sub">演示模式：未接入模型，以上为示例文字。</div>}
          <div className="row">
            <button className="btn sm" disabled={!canInsert} onClick={() => onInsert(reply)}>
              {mode === "edit" ? "替换选区 / 插入光标处" : "插入光标处"}
            </button>
            <span className="sub">插入后保存为新版本，签署前照样过质量门禁。</span>
          </div>
        </div>
      )}
    </aside>
  );
}
