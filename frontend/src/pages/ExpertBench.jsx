import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";
import { applyPreview, buildAnswer, CHOICE_LABELS, previewTexts, problems } from "../bench/answers.js";

// 专家工作台（四期 B23①②；试用反馈 11-25 起侧栏不再单列，从“我的任务”的知识内容审核项一键进入，?v= 定位到对应卡）：审核问答化 + 按类型填空卡 + 大白话 + 冲突处理卡 + 去标识预览。只给知识专家；回答换成审核决定，规则以后端为准
function Question({ q, a, onChange }) {
  const choice = a?.choice || "";
  const needValue = q.kind === "field" && (choice === "no" || (q.empty && choice === "yes"));
  const labels = q.kind === "conflict" ? q.options : CHOICE_LABELS;
  return (
    <li>
      <div className="sb" style={{ gap: 8 }}>
        <span>
          {q.text}
          {q.uncertain && !q.empty && <> <span className="chip c-a" title="Agent 拿不准">请核对</span></>}
        </span>
        <span className="row" style={{ gap: 4 }}>
          {Object.entries(labels).map(([k, label]) => (
            <button key={k} className={choice === k ? "btnp sm" : "btn sm"} aria-pressed={choice === k}
              onClick={() => onChange({ ...a, choice: k })}>{label}</button>
          ))}
        </span>
      </div>
      {(needValue || (q.kind === "keep" && choice === "no")) && (
        <input className="inp" style={{ width: "100%", marginTop: 4 }} maxLength={500}
          placeholder={q.kind === "keep" ? "为什么不收（选填，勿写客户原文）" : "写上对的（可直接贴）"}
          value={a?.value || ""} onChange={(e) => onChange({ ...a, value: e.target.value })} />
      )}
    </li>
  );
}

function FillCard({ template, fill, setFill }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
      <span className="sub">按“{template.name}”填空改正文：不用写成文章，零散文字直接贴</span>
      {template.slots.map((s) => (
        <label key={s.key} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span>{s.label}{s.required ? "" : <span className="sub">（选填）</span>}</span>
          <textarea className="inp" rows={2} maxLength={2000} placeholder={s.hint}
            value={fill[s.key] || ""} onChange={(e) => setFill({ ...fill, [s.key]: e.target.value })} />
        </label>
      ))}
    </div>
  );
}

function ConflictCard({ conflict }) {
  return (
    <div className="pnl pad" style={{ marginTop: 6 }} aria-label="冲突处理卡">
      <div className="sub">Agent 的理由：{conflict.reason || "没写"}</div>
      {conflict.items.length ? (
        <ul className="lst">
          {conflict.items.map((x) => (
            <li key={x.doc_id}>
              <span className="mono">{x.doc_id} v{x.version}</span> {x.title}
              <div className="sub">{x.summary}</div>
            </li>
          ))}
        </ul>
      ) : <div className="sub">没找到对应的已有条目，按理由判断即可。</div>}
    </div>
  );
}

function DeidPreview({ preview, ack, setAck, onApply }) {
  if (preview.blocked) {
    return <div className="err" role="alert">含不能入库的内容（{preview.guard_labels.join("、")}），请删掉后重写。</div>;
  }
  if (!preview.total) return <div className="sub">没发现能认出客户的词，可以直接交。</div>;
  return (
    <div className="pnl pad" style={{ marginTop: 6 }} aria-label="去标识预览">
      <div className="sub">你写的文字里这些可能认出客户，交之前会换成占位符：</div>
      <ul className="lst">
        {preview.hits.map((h) => <li key={h.placeholder}>{h.category} → <span className="mono">{h.placeholder}</span> ×{h.count}</li>)}
      </ul>
      <div className="row" style={{ gap: 6 }}>
        <button className="btnp sm" onClick={onApply}>换成占位符</button>
        <label className="row" style={{ gap: 4 }}>
          <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
          <span className="sub">这些不是客户信息，照原样交</span>
        </label>
      </div>
    </div>
  );
}

function BenchCard({ card, plain, onDone }) {
  const [state, setState] = useState({});
  const [fill, setFill] = useState({});
  const [showFill, setShowFill] = useState(false);
  const [showBody, setShowBody] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [started] = useState(() => Date.now());
  const [preview, setPreview] = useState(null);
  const [ack, setAck] = useState(false);
  const todo = problems(card, state, fill);
  const texts = previewTexts(state, fill);
  const runPreview = async () => {
    setErr("");
    try {
      setPreview(await api.benchDeidPreview(texts));
      setAck(false);
    } catch (e) {
      setErr(e.message);
    }
  };
  const applyIt = () => {
    const r = applyPreview(state, fill, preview.texts);
    setState(r.state);
    setFill(r.fill);
    setPreview(null);
  };
  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.benchAnswer(card.version_id, { ...buildAnswer(card, state, fill, "", ack), elapsed_ms: Date.now() - started });
      onDone(card.version_id, plain[r.outcome] || r.outcome);
    } catch (e) {
      setErr(e.message);
      if (e.status === 409) runPreview();
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="pnl pad" aria-label={card.title}>
      <div className="sb">
        <h2 className="h2">{card.title} <span className="sub mono">{card.doc_id} v{card.version}</span></h2>
        <span className="row" style={{ gap: 6 }}>
          <span className="chip c-n">{card.kind_plain}</span>
          <span className="chip c-n">{card.status_plain}</span>
        </span>
      </div>
      <div className="sub">{plain.source_detail}：{card.source_detail || "没写"} · {card.confidence_plain}</div>
      {card.basis && <div className="sub">Agent 的看法：{card.basis}</div>}
      <button className="btn sm" style={{ marginTop: 6 }} onClick={() => setShowBody((v) => !v)}>{showBody ? "收起正文" : "看正文"}</button>
      {showBody && <pre className="pnl pad" style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>{card.body}</pre>}
      {card.conflict && <ConflictCard conflict={card.conflict} />}
      <ul className="lst" style={{ marginTop: 6 }}>
        {card.questions.map((q) => (
          <Question key={q.id} q={q} a={state[q.id]} onChange={(a) => setState({ ...state, [q.id]: a })} />
        ))}
      </ul>
      <button className="btn sm" onClick={() => setShowFill((v) => !v)}>{showFill ? "不用填空卡" : "用填空卡改正文"}</button>
      {showFill && <FillCard template={card.template} fill={fill} setFill={setFill} />}
      {todo.length > 0 && <ul className="sub" style={{ marginTop: 6 }}>{todo.map((t) => <li key={t}>{t}</li>)}</ul>}
      {preview && <DeidPreview preview={preview} ack={ack} setAck={setAck} onApply={applyIt} />}
      <div className="row" style={{ gap: 6, marginTop: 6 }}>
        <button className="btn sm" disabled={!Object.keys(texts).length} onClick={runPreview}>去标识预览</button>
        <button className="btnp sm" disabled={busy || todo.length > 0} onClick={submit}>交上去</button>
      </div>
      {err && <div className="err" role="alert">{err}</div>}
    </section>
  );
}

export default function ExpertBench() {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);
  const [done, setDone] = useState([]);
  const [showFolded, setShowFolded] = useState(false);
  const [params] = useSearchParams();
  const focus = Number(params.get("v")) || 0;
  const load = useCallback(() => { api.benchCards().then(setD).catch((e) => setError(e.message)); }, []);
  useEffect(load, [load]);
  if (error) return <section className="pnl pad err" role="alert">{error}</section>;
  if (!d) return <section className="pnl pad sub">加载中…</section>;
  const finished = (id, outcome) => { setDone([...done, { id, outcome }]); setD({ ...d, cards: d.cards.filter((c) => c.version_id !== id) }); };
  const picked = d.cards.filter((c) => c.version_id === focus);      // 从我的任务点进来的那张卡排最前，不受额度折叠
  const today = [...picked, ...d.cards.filter((c) => !c.folded && c.version_id !== focus)];
  const later = d.cards.filter((c) => c.folded && c.version_id !== focus);
  const list = (rows) => rows.map((c) => <BenchCard key={c.version_id} card={c} plain={d.plain} onDone={finished} />);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="一题一答，Agent 已经起好草稿，你只做判断" title="专家工作台" />
      <div className="row"><Link className="btn sm" to="/my-tasks">回我的任务</Link>
        {focus > 0 && !picked.length && <span className="sub">这条已处理或已不在你的待审里。</span>}</div>
      <div className="sub">今天先看这 {today.length} 条（每日 {d.quota} 条）。不确定的就选“不确定”，这条会留着不定。</div>
      {done.length > 0 && <div className="sub">刚交的：{done.map((x) => `#${x.id} ${x.outcome}`).join("，")}</div>}
      {today.length ? list(today) : <section className="pnl pad sub">今天没有要你看的。</section>}
      {later.length > 0 && (
        <section className="pnl pad" aria-label="超出额度">
          <div className="sb"><h2 className="h2">明天再看 <span className="sub mono">{later.length}</span></h2>
            <button className="btn sm" onClick={() => setShowFolded((v) => !v)}>{showFolded ? "收起" : "展开"}</button></div>
        </section>
      )}
      {showFolded && list(later)}
    </div>
  );
}
