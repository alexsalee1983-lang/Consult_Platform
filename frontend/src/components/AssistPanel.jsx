// 写稿助手 v1（三期 A15②）：交付件编辑页侧栏。问 / 改切换、快 / 深两档、生成中可停止；
// 自动上下文 = 编辑中的正文（未保存的也算）+ 选中的文字，由后端补交付件和章节标题、项目卡、知识库检索。
// “插入”把回复原样放到光标处或替换选区（来源标注保留），之后照常保存为新版本、过签署门禁；助手不直接写交付件。
// 四期 A16① 选中即改：“改”且有选区时锁定该区间，回复后显示词级前后对比，接受 = 替换原区间（可回退）、放弃 = 正文不动；
// 原区间文字被改动过就拒绝替换；改写去掉的来源标注单独提醒。
import { useEffect, useMemo, useRef, useState } from "react";
import { assistSection } from "../api.js";
import { sourcesLine } from "../editor/assist.js";
import { diffStats, lostRefs, wordDiff } from "../editor/selectEdit.js";

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

function DiffView({ segs }) {
  return (
    <div className="mono assist-diff" aria-label="前后对比">
      {segs.map((g, i) => (g.op === "=" ? <span key={i}>{g.t}</span>
        : g.op === "-" ? <del key={i}>{g.t}</del> : <ins key={i}>{g.t}</ins>))}
    </div>
  );
}

export default function AssistPanel({ code, dcode, seq, getContext, getRange, onInsert, onReplace, request }) {
  const [mode, setMode] = useState("ask");
  const [speed, setSpeed] = useState("fast");
  const [prompt, setPrompt] = useState("");
  const [reply, setReply] = useState("");
  const [done, setDone] = useState(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [target, setTarget] = useState(null);   // 改写锁定的区间 {from, to, text}
  const [applied, setApplied] = useState(null); // 已接受：{from, to, original, text}，用于回退
  const ctrl = useRef(null);
  const promptRef = useRef(null);

  // 编辑器选区浮层“让助手改”：切到“改”、锁定区间、光标进要求框
  useEffect(() => {
    if (!request || running) return;
    setMode("edit");
    setTarget({ from: request.from, to: request.to, text: request.text });
    setReply("");
    setDone(null);
    setApplied(null);
    setError("");
    promptRef.current?.focus();
  }, [request]); // eslint-disable-line react-hooks/exhaustive-deps

  async function send() {
    const r = target || getRange() || { from: 0, to: 0, text: "" };
    const selection = r.text || "";
    setTarget(mode === "edit" && selection ? r : null);
    setApplied(null);
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
  const compare = Boolean(mode === "edit" && target && done && !stopped);
  const segs = useMemo(() => (compare ? wordDiff(target.text, reply) : null), [compare, target, reply]);
  const stats = segs && diffStats(segs);
  const lost = compare ? lostRefs(target.text, reply) : [];

  function accept() {
    if (onReplace(target, reply)) {
      setApplied({ from: target.from, to: target.from + reply.length, original: target.text, text: reply });
      setTarget(null);
      setReply("");
      setDone(null);
    } else setError("原文这段已经改动过，没有替换。请重新选中再改。");
  }
  function revert() {
    if (onReplace({ from: applied.from, to: applied.to, text: applied.text }, applied.original)) setApplied(null);
    else setError("替换后的文字又改动过，无法自动回退，请用 Ctrl+Z 撤销。");
  }
  function discard() {
    setTarget(null);
    setReply("");
    setDone(null);
  }
  return (
    <aside className="pnl assist" aria-label="写稿助手">
      <div className="sb">
        <b>写稿助手</b>
        <span className="sub">{mode === "ask" ? "问本章、问做法" : "按要求改写选中文字或本章"}</span>
      </div>
      {target && !done && <div className="sub">将改写选中的 {[...target.text].length} 字（在正文里选中文字可点“让助手改”）</div>}
      <div className="row" style={{ gap: 12 }}>
        <Seg items={MODES} value={mode} onChange={setMode} label="问或改" disabled={running} />
        <Seg items={SPEEDS} value={speed} onChange={setSpeed} label="快慢档" disabled={running} />
      </div>
      <textarea ref={promptRef} className="inp" rows={4} maxLength={2000} value={prompt} disabled={running}
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
          {segs ? <DiffView segs={segs} /> : <div className="mono assist-text">{reply || "生成中…"}</div>}
          {stats && <div className="sub">删 {stats.removed} 字 · 增 {stats.added} 字（红删绿增）</div>}
          {lost.length > 0 && <div className="assist-bad">改写去掉了来源标注：{lost.join("、")}，接受前请确认。</div>}
          {stopped && <div className="sub">已停止，以上为停止前的部分。</div>}
          {done && <div className={done.sources?.unresolved?.length ? "assist-bad" : "sub"}>{sourcesLine(done.sources)}</div>}
          {done?.demo && <div className="sub">演示模式：未接入模型，以上为示例文字。</div>}
          {segs ? (
            <div className="row">
              <button className="btnp" disabled={!canInsert} onClick={accept}>接受改写</button>
              <button className="btn sm" onClick={discard}>放弃</button>
              <span className="sub">接受后保存为新版本，签署前照样过质量门禁。</span>
            </div>
          ) : (
            <div className="row">
              <button className="btn sm" disabled={!canInsert} onClick={() => onInsert(reply)}>
                {mode === "edit" ? "替换选区 / 插入光标处" : "插入光标处"}
              </button>
              <span className="sub">插入后保存为新版本，签署前照样过质量门禁。</span>
            </div>
          )}
        </div>
      )}
      {applied && (
        <div className="row">
          <span className="sub">已替换选中的文字。</span>
          <button className="btn sm" onClick={revert}>回退这次改写</button>
        </div>
      )}
    </aside>
  );
}
