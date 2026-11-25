import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api, CONCLUSION_CLS, DELIV_STATUS, SEVERITY_CLS, TIER } from "../api.js";
import { useAuth } from "../auth.jsx";
import { Loading, TopBar } from "../components/common.jsx";
import AssistPanel from "../components/AssistPanel.jsx";
import ConfirmCard from "../components/ConfirmCard.jsx";
import DocBody from "../components/DocBody.jsx";
import DeliverableForm from "../components/DeliverableForm.jsx";
import { BatchSign, ClientFeedback, ModeBar, RevisePanel } from "../components/DeliverableModes.jsx";
import { modeOf, separationNote } from "../delivModes.js";
import { noteAfterInsert, spliceText } from "../editor/assist.js";
import { applyReplace } from "../editor/selectEdit.js";

// A15：新编辑器按需加载；新旧并存一个关口，新编辑器默认开启（决策记录 2026-11-21 ⑦），可一键切回文本框
const DocEditor = lazy(() => import("../components/DocEditor.jsx"));
const EDITOR_PREF = "cp.editor";   // localStorage："textarea" = 用旧文本框；其他 = 新编辑器
function useNewEditor() {
  const [on, setOn] = useState(() => {
    try { return localStorage.getItem(EDITOR_PREF) !== "textarea"; } catch { return true; }
  });
  function toggle() {
    const next = !on;
    try { localStorage.setItem(EDITOR_PREF, next ? "new" : "textarea"); } catch { /* 无痕模式等：只本次生效 */ }
    setOn(next);
  }
  return [on, toggle];
}
import { FeedbackKeys, useKbFeedback } from "../components/KbFeedback.jsx";
import { knowledgeLine } from "../kbUsage.js";

// 交付件审阅（设计稿 04）：左栏目录与来源构成、质量门禁；中间章节正文（来源上标、版本、修改）；
// 右栏与本交付件有关的确认卡、签署流程。规则都在后端，界面只按接口返回显示和操作。

const SOURCE_ROWS = [["evidence", "客户证据", "var(--ok)"], ["knowledge", "知识库", "var(--cyan)"], ["inferred", "模型推断", "var(--vio)"]];
const EVIDENCE_KIND = { D: "客户资料", I: "访谈纪要", O: "现场观察" };
const ACTION_NAMES = { submit: "提交签署", sign: "签署", return: "退回修改", reopen: "客户意见重开修订" };

function fmt(t) {
  return t ? new Date(t).toLocaleString("zh-CN", { hour12: false }).replace(/:\d{2}$/, "") : "";
}

function SourceBar({ counts }) {
  const total = SOURCE_ROWS.reduce((n, [k]) => n + (counts[k] || 0), 0);
  return (
    <>
      <div className="srcbar" aria-hidden="true">
        {total > 0 && SOURCE_ROWS.map(([k, , color]) => counts[k] ? <i key={k} style={{ flex: counts[k], background: color }} /> : null)}
      </div>
      {SOURCE_ROWS.map(([k, name, color]) => (
        <div key={k} className="sb sub"><span style={{ color }}>{name}</span><span className="mono">{counts[k] || 0}</span></div>
      ))}
      {counts.unresolved > 0 && <div className="sb sub"><span style={{ color: "var(--crim)" }}>核对不上</span><span className="mono">{counts.unresolved}</span></div>}
    </>
  );
}

function Toc({ d, seq, onPick, editable, onOutline }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function save() {
    const titles = text.split("\n").map((t) => t.trim()).filter(Boolean);
    setBusy(true);
    setError("");
    try {
      await onOutline(titles);
      setEditing(false);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="目录">
      <div className="sb"><span className="cap">目录</span>
        {editable && !editing && <button className="btn sm" onClick={() => { setText(d.sections.map((s) => s.title).join("\n")); setEditing(true); }}>{d.sections.length ? "改大纲" : "写大纲"}</button>}
      </div>
      {editing ? (
        <>
          <textarea className="inp" rows={8} value={text} onChange={(e) => setText(e.target.value)} placeholder="每行一个章节标题" />
          <div className="sub">按位置对应：改了标题的章节，正文留在原位置。</div>
          {error && <div className="err" role="alert">{error}</div>}
          <div className="row">
            <button className="btnp" disabled={busy || !text.trim()} onClick={save}>保存大纲</button>
            <button className="btn" disabled={busy} onClick={() => setEditing(false)}>取消</button>
          </div>
        </>
      ) : (
        <ul className="toc">
          {d.sections.map((s) => (
            <li key={s.seq}>
              <button className={s.seq === seq ? "on" : undefined} onClick={() => onPick(s.seq)}>
                <span className="mono sub">{s.seq}</span>
                <span className="t">{s.title}</span>
                {s.current_version === 0 ? <span className="chip c-n">无正文</span>
                  : s.unresolved.length > 0 ? <span className="chip c-r">{s.unresolved.length}</span> : null}
              </button>
            </li>
          ))}
          {d.sections.length === 0 && <li className="sub">还没有大纲</li>}
        </ul>
      )}
    </section>
  );
}

function Diff({ lines }) {
  const body = lines.filter((l) => !l.startsWith("---") && !l.startsWith("+++"));
  return (
    <div className="mono diff-b" aria-label="版本差异">
      {body.map((l, i) => (
        <div key={i} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "ctx"}>
          {l.startsWith("@@") ? "· · ·" : l}
        </div>
      ))}
      {body.length === 0 && <div className="ctx">两个版本没有差异</div>}
    </div>
  );
}

function Section({ code, dcode, seq, editable, onSaved, signed, sections = [] }) {
  const [s, setS] = useState(null);
  const [error, setError] = useState(null);
  const [version, setVersion] = useState(null);       // null = 当前版本
  const [diff, setDiff] = useState(null);
  const [edit, setEdit] = useState(null);             // {body, note}
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [newEditor, toggleEditor] = useNewEditor();
  const [askReq, setAskReq] = useState(null);   // A16① 选区浮层“让助手改”
  const editorRef = useRef(null);     // 新编辑器：selection() / insert()
  const areaRef = useRef(null);       // 文本框兜底
  const bodyRef = useRef("");
  bodyRef.current = edit?.body ?? "";

  // 写稿助手（A15②）：取选中文字、插入回复（来源标注原样保留），修改说明记一笔“含写稿助手插入内容”
  function selectionRange() {
    if (newEditor) return editorRef.current?.range() || null;
    const a = areaRef.current;
    return a ? { from: a.selectionStart, to: a.selectionEnd, text: a.value.slice(a.selectionStart, a.selectionEnd) } : null;
  }
  // A16① 接受 / 回退改写：原区间文字没变才替换
  function replaceReply(range, snippet) {
    let ok;
    if (newEditor && editorRef.current) ok = editorRef.current.replaceRange(range.from, range.to, range.text, snippet);
    else {
      const r = applyReplace(bodyRef.current, range, snippet);
      ok = r.ok;
      if (ok) setEdit((e) => ({ ...e, body: r.text }));
    }
    if (ok) setEdit((e) => ({ ...e, note: noteAfterInsert(e.note) }));
    return ok;
  }
  function insertReply(snippet) {
    if (newEditor && editorRef.current) editorRef.current.insert(snippet);
    else {
      const a = areaRef.current;
      const r = spliceText(bodyRef.current, a?.selectionStart, a?.selectionEnd, snippet);
      setEdit((e) => ({ ...e, body: r.text }));
    }
    setEdit((e) => ({ ...e, note: noteAfterInsert(e.note) }));
  }

  const load = useCallback(() => {
    setError(null);
    return api.section(code, dcode, seq, version).then(setS).catch(setError);
  }, [code, dcode, seq, version]);
  useEffect(() => { setDiff(null); load(); }, [load]);
  useEffect(() => { setVersion(null); setEdit(null); }, [seq]);

  // A16② @ 引用与 [ 补全：证据列表进编辑态时取一次（项目成员接口），知识按查询词走已有检索接口（用户可见范围），防抖 300ms
  const [evs, setEvs] = useState(null);
  const [kbHits, setKbHits] = useState([]);
  const kbTimer = useRef(null);
  const editing = !!edit;
  useEffect(() => {
    if (editing && evs === null) api.evidences(code).then(setEvs).catch(() => setEvs([]));
  }, [editing, evs, code]);
  useEffect(() => () => clearTimeout(kbTimer.current), []);
  const onMentionQuery = useCallback((kind, q) => {
    clearTimeout(kbTimer.current);
    if (q.length < 2) return;
    kbTimer.current = setTimeout(() => {
      api.kbSearch({ q, limit: 8 }).then((r) => setKbHits(r.items || [])).catch(() => {});
    }, 300);
  }, []);
  const mentionRefs = useMemo(() => ({ evidences: evs || [], kb: kbHits, sections, currentSeq: seq }), [evs, kbHits, sections, seq]);

  async function compare() {
    try {
      const r = await api.sectionDiff(code, dcode, seq, s.version, s.current_version);
      setDiff(r.diff);
    } catch (e) {
      setError(e);
    }
  }

  async function save() {
    setBusy(true);
    setSaveError("");
    try {
      await api.putSection(code, dcode, seq, edit.body, edit.note.trim(), s.current_version || null);
      setEdit(null);
      setVersion(null);
      await load();
      await onSaved();
    } catch (e) {
      setSaveError(e.message);
    } finally {
      setBusy(false);
    }
  }

  // 知识使用反馈三键（三期 B29）：本版本里核对得上的知识引用
  const fb = useKbFeedback(code, s?.version && (s.sources?.knowledge || []).some((r) => r.found)
    ? { deliverable: dcode, seq, version: s.version } : null);
  if (!s) return <Loading error={error} />;
  const isCurrent = s.version === s.current_version;
  const unresolved = s.sources?.unresolved || [];
  const refs = [...(s.sources?.evidence || []), ...(s.sources?.knowledge || [])];
  const kbCited = (s.sources?.knowledge || []).some((r) => r.found);

  return (
    <section className="pnl doc-pnl" aria-label="章节正文">
      <div className="vhead">
        <div className="sb" style={{ flexWrap: "wrap", gap: 8 }}>
          <span className="mono sub">{dcode} / 第 {s.seq} 章 / {s.version ? `v${s.version}` : "无正文"}{!isCurrent && "（历史版本）"}</span>
          {!signed && <span className="mono" style={{ color: "var(--amber)", fontWeight: 700 }}>【草稿 · 待顾问审核】</span>}
          <div className="row">
            {s.versions.length > 1 && (
              <select className="sm" aria-label="版本" value={version ?? ""} onChange={(e) => setVersion(e.target.value ? Number(e.target.value) : null)}>
                <option value="">当前版本 v{s.current_version}</option>
                {s.versions.filter((v) => v.version !== s.current_version).map((v) => (
                  <option key={v.version} value={v.version}>v{v.version} · {v.author}{v.agent_code ? `（${v.agent_code}）` : ""}</option>
                ))}
              </select>
            )}
            {!isCurrent && <button className="btn sm" onClick={diff ? () => setDiff(null) : compare}>{diff ? "收起对比" : "与当前版本对比"}</button>}
            {editable && isCurrent && !edit && <button className="btn sm" onClick={() => setEdit({ body: s.body, note: "" })}>{s.version ? "修改本章" : "写正文"}</button>}
          </div>
        </div>
        <h2 className="h1" style={{ fontSize: 24 }}>{s.title}</h2>
        {s.version > 0 && <div className="sub">{s.author}{s.agent_code ? `（${s.agent_code}）` : ""} · {fmt(s.created_at)}{s.note ? ` · ${s.note}` : ""}</div>}
      </div>
      <div className="doc-wrap">
        {error && <div className="err">{error.message}</div>}
        {diff && <Diff lines={diff} />}
        {edit ? (
          <div className="assist-grid">
          <div style={{ display: "flex", flexDirection: "column", gap: 8, minWidth: 0 }}>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn sm" onClick={toggleEditor}>{newEditor ? "切回文本框" : "用新编辑器"}</button>
            </div>
            {newEditor ? (
              <Suspense fallback={<div className="sub">编辑器加载中…</div>}>
                <DocEditor ref={editorRef} onAsk={(r) => setAskReq({ ...r, at: Date.now() })} value={edit.body} onChange={(body) => setEdit((e) => ({ ...e, body }))} unresolved={new Set(unresolved)}
                  refs={mentionRefs} onMentionQuery={onMentionQuery}
                  placeholder="Markdown；来源标注写成 [I-01] [D-12] [O-05] [S-02] [L4-…] [推断]，显示为标签，退格整体删除；输入 @ 引用证据、知识、章节，输入 [ 补全编号" />
              </Suspense>
            ) : (
              <textarea ref={areaRef} className="inp mono" rows={22} value={edit.body} onChange={(e) => setEdit({ ...edit, body: e.target.value })}
                placeholder="Markdown；来源标注写成 [I-01] [D-12] [O-05] [S-02] [L4-…] [推断]" />
            )}
            <input className="inp" maxLength={500} value={edit.note} placeholder="修改说明（可选）" onChange={(e) => setEdit({ ...edit, note: e.target.value })} />
            <div className="sub">保存会新建一个版本，旧版本保留。正文先过机密级检测，命中不保存。{signed ? "" : "待签署时修改会退回起草中。"}</div>
            {saveError && <div className="err" role="alert">{saveError}</div>}
            <div className="row">
              <button className="btnp" disabled={busy || !edit.body.trim()} onClick={save}>{busy ? "保存中…" : "保存为新版本"}</button>
              <button className="btn" disabled={busy} onClick={() => setEdit(null)}>取消</button>
            </div>
          </div>
          <AssistPanel code={code} dcode={dcode} seq={seq} getContext={() => bodyRef.current}
            getRange={selectionRange} onInsert={insertReply} onReplace={replaceReply} request={askReq} />
          </div>
        ) : s.version ? (
          <DocBody body={s.body} unresolved={unresolved} />
        ) : (
          <div className="sub">本章还没有正文。</div>
        )}
        {(refs.length > 0 || unresolved.length > 0) && (
          <div className="doc-refs" aria-label="本章来源">
            {edit && <div className="cap">当前保存版本用到的来源与知识（保存后更新）</div>}
            {refs.map((r) => {
              const kb = r.found && r.ref.startsWith("L") ? fb.items[r.ref] || fb.items[r.doc_id] : null;
              return (
              <div key={r.ref} className="row" style={{ flexWrap: "wrap" }}>
                <span className="mono" style={{ color: !r.found ? "var(--crim)" : r.ref.startsWith("L") ? "var(--cyan)" : "var(--ok)", minWidth: 120 }}>{r.ref}</span>
                <span className="sub">{!r.found ? "核对不上：项目里没有这条证据或知识"
                  : r.layer_name || r.layer || EVIDENCE_KIND[r.ref[0]] || ""}</span>
                {kb && kb.visible && <span>{kb.title}</span>}
                {kb && <span className="sub" style={kb.recheck_state ? { color: "var(--amber)" } : undefined}>{knowledgeLine(kb)}</span>}
                {!edit && kbCited && r.found && r.ref.startsWith("L") && <FeedbackKeys docId={r.ref} fb={fb} />}
              </div>
              );
            })}
            {fb.error && <div className="err" role="alert">{fb.error}</div>}
          </div>
        )}
      </div>
    </section>
  );
}

const MIN_WAIVE_REASON = 10; // 与后端 quality.MIN_WAIVE_REASON 一致

// 项目经理驳回评审 Agent 提出的一条阻断（必须写理由；平台规则的阻断不能驳回）
function Waive({ code, dcode, rid, issue, onDone }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit() {
    setBusy(true);
    setError("");
    try {
      onDone(await api.waive(code, dcode, rid, issue, reason.trim()));
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }
  const short = reason.replace(/\s/g, "").length < MIN_WAIVE_REASON;
  if (!open) return <button className="btn sm" onClick={() => setOpen(true)}>驳回</button>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <textarea className="inp" rows={2} maxLength={2000} value={reason} placeholder={`驳回理由（必填，至少 ${MIN_WAIVE_REASON} 个字，写明为什么可以不改；记入签署记录）`}
        onChange={(e) => setReason(e.target.value)} />
      {error && <div className="err" role="alert">{error}</div>}
      <div className="row">
        <button className="btnp" disabled={busy || short} onClick={submit}>确认驳回</button>
        <button className="btn" disabled={busy} onClick={() => setOpen(false)}>取消</button>
      </div>
    </div>
  );
}

function Quality({ code, dcode, so, canReview, isManager, onChanged }) {
  const r = so.latest_review;
  const [detail, setDetail] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = r && (r.status === "queued" || r.status === "running");

  useEffect(() => { setDetail(null); }, [r?.id, r?.status]);
  useEffect(() => {
    if (open && r && r.status === "done" && !detail) api.review(code, dcode, r.id).then(setDetail).catch((e) => setError(e.message));
  }, [open, r, detail, code, dcode]);

  async function start() {
    setBusy(true);
    setError("");
    try {
      await api.startReview(code, dcode);
      await onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="质量门禁">
      <div className="sb"><span className="cap">质量门禁</span>
        {r?.status === "done" && <span className={`chip ${CONCLUSION_CLS[r.conclusion] || "c-n"}`}>{r.conclusion_name}</span>}
      </div>
      {!r && <div className="sub">还没有评审</div>}
      {r && r.status === "done" && (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span className="chip c-r">阻断 {r.blocking}</span><span className="chip c-a">重要 {r.major}</span><span className="chip c-n">建议 {r.minor}</span>
          {r.waived > 0 && <span className="chip c-n">已驳回阻断 {r.waived}</span>}
        </div>
      )}
      {active && <div className="sub" style={{ color: "var(--cyan)" }}>{r.status === "queued" ? "评审排队中…" : "质量评审 Agent 评审中…"}</div>}
      {r?.status === "failed" && <div className="err">评审失败：{r.error || "请重试"}</div>}
      {r && <div className="sub">{fmt(r.created_at)} · {r.created_by} · 修订 r{r.revision}{r.stale && <span style={{ color: "var(--amber)" }}> · 评审后有修改</span>}</div>}
      {r?.status === "done" && <button className="btn sm" onClick={() => setOpen(!open)}>{open ? "收起问题清单" : "查看问题清单"}</button>}
      {open && detail && (
        <ul className="lst issues">
          {detail.issues.map((x, i) => {
            const w = detail.waivers.find((v) => v.issue === i);
            return (
              <li key={i}>
                <div className="row" style={{ flexWrap: "wrap" }}>
                  {x.code && <span className="sub mono">{x.code}</span>}
                  <span className={`chip ${w || x.status === "已驳回" ? "c-n" : SEVERITY_CLS[x.severity] || "c-n"}`}>{x.severity}</span>
                  <span className="sub">{x.dimension}</span>
                  {x.downgraded && <span className="chip c-n">不在五类阻断内，已降为重要</span>}
                  {!w && x.status === "已驳回" && <span className="chip c-n">之前已驳回，不再计入</span>}
                  {x.by === "rule" && <span className="chip c-n">平台规则</span>}
                  {w && <span className="chip c-n">已驳回</span>}
                </div>
                {x.location && <div className="sub">{x.location}</div>}
                <div>{x.problem}</div>
                {x.suggestion && <div className="sub">建议：{x.suggestion}</div>}
                {w && <div className="sub">驳回：{w.actor} · {fmt(w.created_at)} · {w.reason}</div>}
                {!w && x.waivable && isManager && canReview && !r.stale && (
                  <Waive code={code} dcode={dcode} rid={r.id} issue={i} onDone={(res) => { setDetail(res); onChanged(); }} />
                )}
              </li>
            );
          })}
          {detail.issues.length === 0 && <li className="sub">没有问题</li>}
        </ul>
      )}
      {error && <div className="err" role="alert">{error}</div>}
      {canReview && <button className="btn" disabled={busy || active} onClick={start}>{r ? "重新评审" : "发起质量评审"}</button>}
    </section>
  );
}

function SignFlow({ code, dcode, d, so, isManager, onChanged }) {
  const [comment, setComment] = useState("");
  const [returning, setReturning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const r = so.latest_review;
  const hist = so.history;
  const lastSubmit = [...hist].reverse().find((h) => h.action === "submit");
  const lastSign = [...hist].reverse().find((h) => h.action === "sign");
  const reviewed = r?.status === "done" && !r.stale && r.open_blocking === 0;
  const steps = [
    { name: "Agent / 顾问起草", done: d.sections.length > 0 && d.sections.every((s) => s.current_version > 0), info: d.updated_by },
    { name: "质量评审 Agent", done: reviewed || so.status === "signed", info: r?.status === "done" ? `阻断 ${r.open_blocking}${r.waived ? `（已驳回 ${r.waived}）` : ""}` : "" },
    { name: "顾问审核，提交签署", done: so.status === "review" || so.status === "signed", info: actorName(lastSubmit?.actor) },
    { name: "项目经理签署", done: so.status === "signed", info: lastSign?.actor || "" },
  ];
  const current = steps.findIndex((s) => !s.done);

  async function act(action, text = "") {
    setBusy(true);
    setError("");
    try {
      await (action === "merged" ? api.signMerged(code, dcode) : api.signoffAction(code, dcode, action, text));
      setComment("");
      setReturning(false);
      await onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="签署流程">
      <span className="cap">签署流程</span>
      <ol className="flow">
        {steps.map((s, i) => (
          <li key={s.name} className={s.done ? "done" : i === current ? "now" : undefined}>
            <i aria-hidden="true" /><span>{s.name}</span><span className="sub">{s.done ? s.info : i === current ? "进行中" : "待"}</span>
          </li>
        ))}
      </ol>
      {!so.gate && so.status !== "signed" && so.reasons.length > 0 && (
        <ul className="sub gate-reasons">{so.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
      )}
      {separationNote(so) && <div className="sub">{separationNote(so)}</div>}
      {so.sign_block && so.status === "review" && isManager && <div className="note-amber" role="status">{so.sign_block}</div>}
      {error && <div className="err" role="alert">{error}</div>}
      {so.merged && so.status === "drafting" && isManager && (
        <button className="btnp" disabled={busy || !so.merged_ready} title={so.merged_ready ? "" : "门禁还有问题，先逐条核对"} onClick={() => act("merged")}>核对并签署（一步完成）</button>
      )}
      {so.status === "signed" ? <div className="sub" style={{ color: "var(--ok)" }}>已签署，正文只读。</div>
        : so.status === "review" ? (
          isManager ? (returning ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <textarea className="inp" rows={3} maxLength={2000} value={comment} placeholder="退回理由（必填）" onChange={(e) => setComment(e.target.value)} />
              <div className="row">
                <button className="btnp" disabled={busy || !comment.trim()} onClick={() => act("return", comment.trim())}>确认退回</button>
                <button className="btn" disabled={busy} onClick={() => setReturning(false)}>取消</button>
              </div>
            </div>
          ) : (
            <div className="row">
              <button className="btnp" style={{ flex: 1 }} disabled={busy || !so.gate || !!so.sign_block} onClick={() => act("sign")}>签署</button>
              <button className="btn" disabled={busy} onClick={() => setReturning(true)}>退回修改</button>
            </div>
          )) : <div className="sub">已提交，等待项目经理签署。</div>
        ) : (
          <button className="btnp" disabled={busy || !so.gate} onClick={() => act("submit")}>完成审核，提交签署</button>
        )}
      {hist.length > 0 && (
        <ul className="lst">
          {[...hist].reverse().map((h) => (
            <li key={h.id} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <div className="sb"><span>{ACTION_NAMES[h.action] || h.action} · {actorName(h.actor)}</span><span className="sub mono">r{h.revision}</span></div>
              <div className="sub">{fmt(h.created_at)}{h.waived ? ` · 项目经理驳回阻断 ${h.waived} 项` : ""}{h.comment ? ` · ${h.comment}` : ""}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}


const actorName = (a) => (a === "platform" ? "平台自动（门禁通过）" : a || "");
export default function Deliverable({ code, dcode }) {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [p, setP] = useState(null);
  const [d, setD] = useState(null);
  const [so, setSo] = useState(null);
  const [rv, setRv] = useState(null);           // A26 一键修订状态
  const [cards, setCards] = useState([]);
  const [members, setMembers] = useState([]);
  const [seq, setSeq] = useState(1);
  const [error, setError] = useState(null);
  const [exporting, setExporting] = useState(false);
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState(null);        // A14②：D1、D2 表单定义，没有表单为 null
  const [formOpen, setFormOpen] = useState(false);

  const loadForm = useCallback(() => {
    api.deliverableForm(code, dcode).then(setForm).catch(() => setForm(null));
  }, [code, dcode]);

  const load = useCallback(async () => {
    try {
      const [dd, ss, cc] = await Promise.all([api.deliverable(code, dcode), api.signoff(code, dcode),
        api.projectConfirmations(code, "open")]);
      setD(dd);
      setSo(ss);
      setCards(cc.filter((c) => c.deliverable_code === dcode));
      setError(null);
      api.reviseState(code, dcode).then(setRv).catch(() => setRv(null));
    } catch (e) {
      setError(e);
    }
  }, [code, dcode]);

  useEffect(() => {
    setD(null); setSo(null); setRv(null); setSeq(1); setNotice(""); setForm(null); setFormOpen(false);
    loadForm();
    api.project(code).then(setP).catch(setError);
    api.members(code).then(setMembers).catch(() => setMembers([]));
    load();
  }, [code, dcode, load, loadForm]);

  // 评审在后台跑：排队或进行中时每 3 秒刷新一次
  const active = (so?.latest_review && ["queued", "running"].includes(so.latest_review.status)) || ["queued", "running"].includes(rv?.status);
  useEffect(() => {
    if (!active) return undefined;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [active, load]);

  async function exportFile(fn) {
    setExporting(true);
    setNotice("");
    try {
      const r = await fn(code, dcode);
      setNotice(`已导出 ${r.name}${r.exportId ? `（导出编号 ${r.exportId}）` : ""}`);
    } catch (e) {
      setNotice(`导出失败：${e.message}`);
    } finally {
      setExporting(false);
    }
  }

  if (!p || !d || !so) return <Loading error={error} />;
  const tier = TIER[p.tier] || TIER.standard;
  const strict = p.tier === "strict";
  const signed = d.status === "signed";
  const editable = !signed;
  const isManager = members.some((m) => m.username === user?.username && m.project_role === "manager");
  const st = DELIV_STATUS[d.status] || DELIV_STATUS.not_started;
  const cur = d.sections.find((s) => s.seq === seq) ? seq : d.sections[0]?.seq;
  const mode = modeOf(d, so, rv);

  return (
    <>
      <TopBar cap={`${p.code} · ${d.code} · 修订 r${d.revision}${d.updated_by ? ` · ${d.updated_by} 更新于 ${fmt(d.updated_at)}` : ""}`} title={d.title}>
        <span className={`chip ${tier.cls}`}>{tier.label}</span>
        <span className={`chip ${signed ? "c-g" : "c-a"}`}>{signed ? "已签署" : `草稿 · ${st.label}`}</span>
        <select className="sm" aria-label="切换交付件" value={d.code} onChange={(e) => navigate(`/project/${encodeURIComponent(code)}/deliverables/${e.target.value}`)}>
          {p.deliverables.map((x) => <option key={x.code} value={x.code}>{x.code} {x.title}</option>)}
        </select>
        {editable && form?.writable && !formOpen && <button className="btnp" onClick={() => setFormOpen(true)}>按表单成稿</button>}
        <button className="btn" disabled={exporting || !d.sections.length} onClick={() => exportFile(api.exportWord)}>{exporting ? "导出中…" : "导出 Word"}</button>
        <button className="btn" disabled={exporting || !d.sections.length} onClick={() => exportFile(api.exportPpt)}>导出 PPT</button>
      </TopBar>
      <ModeBar mode={mode} />
      {notice && <div className={notice.startsWith("导出失败") ? "err" : "sub"} role="status">{notice}</div>}
      {error && <div className="err">{error.message}</div>}
      {d.totals.knowledge_updates > 0 && (
        <div className="note-amber" role="status">
          本交付件引用的知识条目有变化，请复核相关章节：
          {d.sections.filter((x) => x.knowledge_updates?.length).map((x) => (
            <span key={x.seq}> 第 {x.seq} 章（{x.knowledge_updates.map((u) => `${u.doc_id} ${u.change === "retired" ? "已下线" : `已更新到 v${u.current_version}`}`).join("，")}）</span>
          ))}
          。改写该章后提醒自动消失。
        </div>
      )}
      {strict && <div className="note-amber">严格档项目：这里只写占位符版本（【客户】【系统A】【人员1】），来源标 [S-xx]，不写真名、IP、产品名和原话，平台写入时检查。导出的 Word 带回现场，用现场工具按对照表回填成正式稿。</div>}
      <div className="kb3">
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <Toc d={d} seq={cur} onPick={setSeq} editable={editable}
            onOutline={async (titles) => { await api.putOutline(code, dcode, titles); await load(); }} />
          <section className="pnl pad" aria-label="来源构成">
            <span className="cap">全文来源构成</span>
            <SourceBar counts={d.totals} />
          </section>
          <Quality code={code} dcode={dcode} so={so} canReview={editable && d.sections.length > 0} isManager={isManager} onChanged={load} />
        </div>
        <div style={{ minWidth: 0 }}>
          {formOpen && form ? <DeliverableForm code={code} dcode={dcode} form={form} onCancel={() => setFormOpen(false)}
            onDone={async () => { setFormOpen(false); setNotice("已按表单成稿，各章第 1 版已生成"); await load(); loadForm(); }} />
            : cur ? <Section key={`${dcode}-${cur}`} code={code} dcode={dcode} seq={cur} editable={editable} signed={signed} onSaved={load} sections={d.sections} />
            : <div className="pnl pad sub">这份交付件还没有大纲。{editable ? (form?.writable ? "可以点右上角“按表单成稿”，或先在左侧写大纲，或在驾驶舱发起交付链让 Agent 起草。" : "先在左侧写大纲，或在驾驶舱发起交付链让 Agent 起草。") : ""}</div>}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <section className="pnl pad" aria-label="待你确认">
            <div className="sb"><span className="cap">待你确认</span><span className="mono" style={{ color: "var(--amber)" }}>{cards.length}</span></div>
            <ul className="lst">
              {cards.map((c) => <ConfirmCard key={c.id} c={c} onDone={load} />)}
              {cards.length === 0 && <li className="sub">本交付件没有待确认事项。<Link to={`/project/${encodeURIComponent(code)}`}>回驾驶舱</Link></li>}
            </ul>
          </section>
          {mode === 1 && <RevisePanel code={code} dcode={dcode} rv={rv} editable={editable} onChanged={load} />}
          <SignFlow code={code} dcode={dcode} d={d} so={so} isManager={isManager} onChanged={load} />
          {mode === 3 && !so.merged && <ClientFeedback code={code} dcode={dcode} sections={d.sections} onChanged={load} />}
          {so.merged && isManager && <BatchSign onChanged={load} />}
        </div>
      </div>
    </>
  );
}
