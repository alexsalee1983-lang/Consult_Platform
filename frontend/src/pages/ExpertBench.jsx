import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";
import { buildAnswer, CHOICE_LABELS, problems } from "../bench/answers.js";

// 专家工作台（四期 B23①）：审核问答化 + 按类型填空卡 + 大白话。只给知识专家；回答换成审核决定，规则以后端为准
function Question({ q, a, onChange }) {
  const choice = a?.choice || "";
  const needValue = q.kind === "field" && (choice === "no" || (q.empty && choice === "yes"));
  return (
    <li>
      <div className="sb" style={{ gap: 8 }}>
        <span>
          {q.text}
          {q.uncertain && !q.empty && <> <span className="chip c-a" title="Agent 拿不准">请核对</span></>}
        </span>
        <span className="row" style={{ gap: 4 }}>
          {Object.entries(CHOICE_LABELS).map(([k, label]) => (
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

function BenchCard({ card, plain, onDone }) {
  const [state, setState] = useState({});
  const [fill, setFill] = useState({});
  const [showFill, setShowFill] = useState(false);
  const [showBody, setShowBody] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [started] = useState(() => Date.now());
  const todo = problems(card, state, fill);
  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      const r = await api.benchAnswer(card.version_id, { ...buildAnswer(card, state, fill), elapsed_ms: Date.now() - started });
      onDone(card.version_id, plain[r.outcome] || r.outcome);
    } catch (e) {
      setErr(e.message);
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
      <ul className="lst" style={{ marginTop: 6 }}>
        {card.questions.map((q) => (
          <Question key={q.id} q={q} a={state[q.id]} onChange={(a) => setState({ ...state, [q.id]: a })} />
        ))}
      </ul>
      <button className="btn sm" onClick={() => setShowFill((v) => !v)}>{showFill ? "不用填空卡" : "用填空卡改正文"}</button>
      {showFill && <FillCard template={card.template} fill={fill} setFill={setFill} />}
      {todo.length > 0 && <ul className="sub" style={{ marginTop: 6 }}>{todo.map((t) => <li key={t}>{t}</li>)}</ul>}
      <div className="row" style={{ gap: 6, marginTop: 6 }}>
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
  const load = useCallback(() => { api.benchCards().then(setD).catch((e) => setError(e.message)); }, []);
  useEffect(load, [load]);
  if (error) return <section className="pnl pad err" role="alert">{error}</section>;
  if (!d) return <section className="pnl pad sub">加载中…</section>;
  const finished = (id, outcome) => { setDone([...done, { id, outcome }]); setD({ ...d, cards: d.cards.filter((c) => c.version_id !== id) }); };
  const today = d.cards.filter((c) => !c.folded);
  const later = d.cards.filter((c) => c.folded);
  const list = (rows) => rows.map((c) => <BenchCard key={c.version_id} card={c} plain={d.plain} onDone={finished} />);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <TopBar cap="一题一答，Agent 已经起好草稿，你只做判断" title="专家工作台" />
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
