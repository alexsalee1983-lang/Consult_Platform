import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import SelfEval from "./SelfEval.jsx";
import { clampInterval, registerShell, REMIND_CHOICES, REMIND_DEFAULT_MIN, reminderState } from "../survey/offline.js";
import { clashes, itemsByQid, PHASES, phases, REASONS, sourceLabel } from "../survey/agenda.js";
import { canPersist, checkResponses, clearDraft, listDrafts, loadDraft, progress, resultsBody, saveDraft } from "../survey/draft.js";

const STATUS = { pending: ["待确认", "c-a"], confirmed: ["已确认", "c-g"], superseded: ["已被取代", ""] };
const store = () => globalThis.localStorage;
const blank = () => ({ respondent_role: "", answers: {} });

// 四期 E2②a：项目驾驶舱“调研助手”。生成调研包 → 按包填答（可多份答卷）→ 回传 → 事实确认单。
// 四期 E3③：现场议程按受访角色分三段（快速确认 → 议程 → 补问），填答按这个顺序走；选项与答案池已有答案不一致时实时提示。
// 证据不足由平台自动分流（补资料请求 / 进议程 / 转人工确认卡），这里只展示。
// 普通档草稿存本机浏览器，断网可继续填，回传成功即清；严格档本机留存待负责人决定（#429），草稿只在内存、刷新即丢。
export default function Survey({ code, tier }) {
  const persist = canPersist(tier);
  const [packs, setPacks] = useState(null);
  const [facts, setFacts] = useState([]);
  const [drafts, setDrafts] = useState([]);
  const [open, setOpen] = useState(null);         // {pack, responses}
  const [idx, setIdx] = useState(0);
  const [errs, setErrs] = useState([]);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [roles, setRoles] = useState({});         // 事实 id → 确认人角色
  const [agenda, setAgenda] = useState(null);
  const [phase, setPhase] = useState("quick_confirm");
  // 严格档定时导出摘要卡提醒：计时与间隔只在内存，不写 storage（E2②b）
  const [interval, setIntervalMin] = useState(REMIND_DEFAULT_MIN);
  const [since, setSince] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());

  // 普通档注册离线壳（只缓存页面壳与静态资源，不缓存接口和答卷）；严格档不注册。开发模式没有 /sw.js，跳过
  useEffect(() => {
    if (import.meta.env?.PROD) registerShell(globalThis.navigator, tier).catch(() => {});
  }, [tier]);
  useEffect(() => {
    if (persist || !open) return undefined;
    const t = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(t);
  }, [persist, open]);
  const remind = reminderState(since, now, interval);
  const exported = () => { const t = Date.now(); setSince(t); setNow(t); };

  const load = useCallback(() => {
    api.surveyPacks(code).then(setPacks).catch(() => setPacks([]));
    api.surveyFacts(code).then(setFacts).catch(() => setFacts([]));
    api.surveyAgenda(code).then(setAgenda).catch(() => setAgenda(null));
    if (persist) setDrafts(listDrafts(store(), code));
  }, [code, persist]);
  useEffect(load, [load]);

  function edit(next) {
    setOpen(next);
    if (persist) {
      saveDraft(store(), next.pack, next.responses, new Date().toISOString());
      setDrafts(listDrafts(store(), code));
    }
  }

  async function run(fn) {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const create = () => run(async () => {
    const pack = await api.createSurveyPack(code);
    setIdx(0);
    setErrs([]);
    edit({ pack, responses: [blank()] });
    exported();
    load();
  });

  function resume(hash) {
    const d = loadDraft(store(), code, hash);
    if (!d) return setError("本机没有这份草稿");
    setIdx(0);
    setErrs([]);
    setOpen({ pack: d.pack, responses: d.responses.length ? d.responses : [blank()] });
  }

  function discard(hash) {
    clearDraft(store(), code, hash);
    if (open?.pack.pack_hash === hash) setOpen(null);
    setDrafts(listDrafts(store(), code));
  }

  const submit = () => {
    const found = checkResponses(open.pack, open.responses);
    setErrs(found);
    if (found.length) return;
    return run(async () => {
      const r = await api.surveyResults(code, open.pack.code, resultsBody(open.pack, open.responses));
      if (persist) clearDraft(store(), code, open.pack.pack_hash);
      setOpen(null);
      setOk(`已回传：待确认事实 ${r.facts} 条，开放题 ${r.open} 条，不清楚 ${r.unclear} 处，跨角色矛盾 ${r.conflicts} 处；`
        + `自动确认 ${r.auto_confirmed} 题，补资料请求 ${r.requests ?? 0} 题，转人工确认卡 ${r.to_manual ?? 0} 题`);
      load();
    });
  };

  const confirm = (f) => run(async () => {
    await api.confirmSurveyFact(code, f.id, (roles[f.id] || "").trim());
    load();
  });

  const cur = open?.responses[idx];
  const groups = open && cur ? phases(open.pack, agenda, cur.respondent_role) : null;
  const items = cur ? itemsByQid(agenda, cur.respondent_role) : {};
  const shown = groups ? groups[phase] : [];
  const setAnswer = (qid, patch) => {
    const responses = open.responses.map((r, i) => (i === idx ? { ...r, answers: { ...r.answers, [qid]: { value: "", text: "", ...r.answers[qid], ...patch } } } : r));
    edit({ ...open, responses });
  };
  const setRole = (v) => edit({ ...open, responses: open.responses.map((r, i) => (i === idx ? { ...r, respondent_role: v } : r)) });
  const addResponse = () => {
    edit({ ...open, responses: [...open.responses, blank()] });
    setIdx(open.responses.length);
  };
  const dropResponse = () => {
    const responses = open.responses.filter((_, i) => i !== idx);
    edit({ ...open, responses: responses.length ? responses : [blank()] });
    setIdx(Math.max(0, idx - 1));
  };

  return (
    <section className="pnl pad" aria-label="调研助手">
      <div className="sb">
        <h2 className="h2">调研助手</h2>
        <span className={`chip ${persist ? "c-g" : "c-r"}`}>{persist ? "草稿存本机浏览器 · 断网可填" : "严格档 · 草稿不落盘"}</span>
      </div>
      <div className="sub">
        {persist
          ? "生成调研包后在现场按受访角色逐份填答，草稿自动存在本机浏览器，联网后回传；回传成功即清本机草稿。封闭题回传后成为待确认事实，“不清楚”只计数。"
          : "严格档本机留存方式待负责人决定，现阶段草稿只在本页内存，刷新或关闭即丢失，请当场填完回传；回传须有有效的客户书面同意，开放题按摘要卡规则检查。"}
      </div>
      {error && <div className="err sub">{error}</div>}
      {ok && <div className="sub" style={{ color: "var(--ok)" }}>{ok}</div>}

      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className="btn sm" disabled={busy} onClick={create}>生成调研包</button>
        {(packs || []).map((k) => (
          <span key={k.code} className="sub mono">{k.code} · {k.questions} 题 · 已回传 {k.returned} 次</span>
        ))}
      </div>
      {drafts.length > 0 && (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span className="cap">本机草稿</span>
          {drafts.map((d) => (
            <span key={d.pack_hash} className="row">
              <button className="btn sm" onClick={() => resume(d.pack_hash)}>继续填 {d.code}（{d.responses} 份）</button>
              <button className="btn sm" onClick={() => discard(d.pack_hash)}>丢弃</button>
            </span>
          ))}
        </div>
      )}

      {open && cur && (
        <div className="pnl pad" style={{ gap: 8 }}>
          <div className="sb">
            <span className="h2">调研包 {open.pack.code}</span>
            <span className="sub">第 {idx + 1} / {open.responses.length} 份 · 已答 {progress(open.pack, cur).done} / {open.pack.questions.length}</span>
          </div>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {open.responses.map((r, i) => (
              <button key={i} className={i === idx ? "btnp sm" : "btn sm"} onClick={() => setIdx(i)}>{r.respondent_role.trim() || `答卷 ${i + 1}`}</button>
            ))}
            <button className="btn sm" onClick={addResponse}>加一份答卷</button>
            <button className="btn sm" onClick={dropResponse}>删除本份</button>
          </div>
          <label className="sub">受访角色（写角色不写姓名，例如“SOC 主管”）
            <input className="inp" maxLength={32} value={cur.respondent_role} onChange={(e) => setRole(e.target.value)} />
          </label>
          {!persist && (
            <div className="row" style={{ flexWrap: "wrap" }} aria-label="定时导出摘要卡提醒">
              {remind.due
                ? <span className="chip c-r">已填 {remind.elapsedMin} 分钟未导出：请用现场工具导出去标识摘要卡，经客户书面确认后带回</span>
                : <span className="sub">距下次导出摘要卡提醒约 {remind.remainMin} 分钟</span>}
              <label className="sub">每
                <select className="inp" value={interval} onChange={(e) => setIntervalMin(clampInterval(e.target.value))}>
                  {REMIND_CHOICES.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
                分钟提醒
              </label>
              <button className="btn sm" onClick={exported}>已导出，重新计时</button>
            </div>
          )}
          <div className="row" style={{ flexWrap: "wrap" }} aria-label="现场三段">
            {PHASES.map(([key, name], i) => (
              <button key={key} className={phase === key ? "btnp sm" : "btn sm"} onClick={() => setPhase(key)}>
                {i < 3 ? `${i + 1}. ` : ""}{name}（{groups[key].length}）
              </button>
            ))}
          </div>
          {!cur.respondent_role.trim() && <div className="sub">先填受访角色，平台按角色给出该角色的快速确认、议程和补问。</div>}
          {shown.length === 0 && <div className="sub">这一段没有题。</div>}
          {shown.map((q) => {
            const a = cur.answers[q.qid] || {};
            const item = items[q.qid];
            const prior = (item?.answers || []).find((x) => x.value);
            const clash = q.kind === "closed" ? clashes(item, a.value) : [];
            return (
              <div key={q.qid} style={{ display: "grid", gap: 4 }}>
                <div className="sub">
                  <span className="mono">{q.qid}</span> {q.text}
                  {item?.reason && <span className="chip">{REASONS[item.reason] || item.reason}</span>}
                </div>
                {phase === "quick_confirm" && prior && (
                  <div className="row" style={{ flexWrap: "wrap" }}>
                    <span className="sub">已有答案：{prior.label}（{prior.role} · {sourceLabel(prior.source)}）</span>
                    <button className="btn sm" onClick={() => setAnswer(q.qid, { value: prior.value })}>对方确认</button>
                  </div>
                )}
                {clash.length > 0 && <span className="chip c-r">与 {clash.join("、")} 的答案不一致，请现场定案</span>}
                {q.kind === "closed" ? (
                  <div className="row" style={{ flexWrap: "wrap" }}>
                    {q.options.map((o) => (
                      <label key={o.key} className="sub">
                        <input type="radio" name={`${idx}-${q.qid}`} checked={a.value === o.key} onChange={() => setAnswer(q.qid, { value: o.key })} /> {o.label}
                      </label>
                    ))}
                    {a.value && <button className="btn sm" onClick={() => setAnswer(q.qid, { value: "" })}>清除</button>}
                  </div>
                ) : (
                  <textarea className="inp" rows={3} maxLength={4000} value={a.text || ""} onChange={(e) => setAnswer(q.qid, { text: e.target.value })}
                    placeholder="不写口令、IP、拓扑、个人信息和业务数据样本" />
                )}
              </div>
            );
          })}
          {errs.length > 0 && <ul className="err sub">{errs.map((e) => <li key={e}>{e}</li>)}</ul>}
          <div className="row">
            <button className="btnp" disabled={busy} onClick={submit}>回传 {open.responses.length} 份答卷</button>
            <button className="btn" disabled={busy} onClick={() => setOpen(null)}>收起{persist ? "（草稿保留在本机）" : "（严格档草稿将丢失）"}</button>
          </div>
        </div>
      )}

      {persist && packs && <SelfEval code={code} packs={packs} />}
      {agenda && agenda.counts.questions > 0 && (
        <div style={{ display: "grid", gap: 6 }} aria-label="现场议程">
          <span className="cap">现场议程（按受访角色，平台自动生成）</span>
          {agenda.roles.length === 0 && <span className="sub">调研包里的题都已确认，没有需要现场谈的。</span>}
          {agenda.roles.map((r) => (
            <div key={r.role} className="row" style={{ flexWrap: "wrap" }}>
              <span className="sub">{r.role}</span>
              {r.interviewed && <span className="chip c-g">已访谈</span>}
              <span className="sub">快速确认 {r.quick_confirm.length} · 议程 {r.agenda.length} · 补问 {r.followup.length}</span>
            </div>
          ))}
          {agenda.requests.length > 0 && (
            <span className="sub">补资料请求 {agenda.requests.length} 题：
              {[...new Set(agenda.requests.flatMap((i) => i.materials))].join("、")}</span>
          )}
          {agenda.manual.length > 0 && (
            <span className="sub">转人工 {agenda.manual.length} 题（已发确认卡，在“待我确认”处理）：
              {agenda.manual.map((i) => `${i.qid} ${i.card_kind}`).join("；")}</span>
          )}
        </div>
      )}

      {facts.length > 0 && (
        <div style={{ display: "grid", gap: 6 }}>
          <span className="cap">事实确认单</span>
          {facts.map((f) => {
            const [label, cls] = STATUS[f.fact_status] || [f.fact_status || "开放题", ""];
            return (
              <div key={f.id} className="row" style={{ flexWrap: "wrap" }}>
                <span className="mono sub">{f.qid}</span>
                <span className="sub">{f.respondent_role}：{f.kind === "closed" ? f.label : f.text}</span>
                {f.kind === "closed" && <span className={`chip ${cls}`}>{label}</span>}
                {f.conflict && <span className="chip c-r">跨角色矛盾</span>}
                {f.fact_status === "confirmed" && <span className="sub">由 {f.confirmed_role} 确认</span>}
                {f.kind === "closed" && f.fact_status === "pending" && (
                  <>
                    <input className="inp" style={{ width: 160 }} maxLength={64} placeholder="确认人角色" value={roles[f.id] || ""}
                      onChange={(e) => setRoles({ ...roles, [f.id]: e.target.value })} />
                    <button className="btn sm" disabled={busy || !(roles[f.id] || "").trim()} onClick={() => confirm(f)}>确认</button>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
