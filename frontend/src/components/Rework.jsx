// 四期 A35：改稿——审阅清单（先告诉人看哪里）、不满意刷新（先问原因）、改动意图提示、一键回退
// 规则在后端 app/project/rework.py；这里只放人要做的事。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

const KIND_CLS = { 门禁提醒: "c-r", 评审问题: "c-a", 来源核对不到: "c-a", 缺正文: "c-a", 无来源句子: "c-n", 低置信: "c-n" };

export function useReviewList(code, dcode) {
  const [rl, setRl] = useState(null);
  const load = useCallback(() => api.reviewList(code, dcode).then(setRl).catch(() => setRl(null)), [code, dcode]);
  useEffect(() => { setRl(null); load(); }, [load]);
  return [rl, load];
}

export function ReviewList({ rl, onPick }) {
  if (!rl) return null;
  const changed = rl.sections.filter((s) => s.changed).map((s) => s.seq);
  const finals = rl.sections.filter((s) => s.human_final).map((s) => s.seq);
  return (
    <section className="pnl pad" aria-label="审阅清单">
      <div className="sb"><span className="cap">审阅清单</span><span className="mono">{rl.items.length}</span></div>
      {rl.round > 1 && <div className="sub">第二轮起只看本轮变化：{changed.length ? `第 ${changed.join("、")} 章有改动` : "评审后没有改动"}</div>}
      {finals.length > 0 && <div className="sub">人工定稿（刷新时跳过）：第 {finals.join("、")} 章</div>}
      <ul className="lst">
        {rl.items.map((x, i) => (
          <li key={`${x.seq}-${x.kind}-${i}`}>
            <button className="btn sm" style={{ textAlign: "left", opacity: rl.round > 1 && !x.changed && x.rank > 0 ? 0.6 : 1 }}
              onClick={() => x.seq && onPick(x.seq)}>
              <span className={`chip ${KIND_CLS[x.kind] || ""}`}>{x.kind}</span> {x.seq ? `第 ${x.seq} 章` : "全件"} · {x.hint}
            </button>
          </li>
        ))}
        {rl.items.length === 0 && <li className="sub">没有需要特别留意的地方。</li>}
      </ul>
    </section>
  );
}

export function RefreshPanel({ code, dcode, seq, rl, editable, intentTick, onChanged }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [intent, setIntent] = useState(null);

  useEffect(() => {
    if (!intentTick || !seq) return;
    api.editIntent(code, dcode, seq).then((r) => setIntent(r.intent ? r : null)).catch(() => setIntent(null));
  }, [code, dcode, seq, intentTick]);

  if (!rl || !editable) return null;
  const cur = rl.sections.find((s) => s.seq === seq);
  const running = ["queued", "running"].includes(rl.refresh.status);

  async function act(fn, done) {
    setBusy(true);
    setError("");
    try {
      const r = await fn();
      if (done) setMsg(done(r));
      await onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const refresh = (reason, whole) => act(() => api.refreshDeliverable(code, dcode, reason, whole ? null : seq), (r) => {
    setOpen(false);
    const extra = [r.skipped.length ? `跳过人工定稿第 ${r.skipped.join("、")} 章` : "", r.handoff.length ? `第 ${r.handoff.join("、")} 章已连续刷新 ${rl.refresh_max} 轮，请人工改写或对话整章` : ""].filter(Boolean).join("；");
    return r.action === "redraft" ? `已开始重新起草第 ${r.seqs.join("、")} 章，完成后可在版本里看差异或一键回退${extra ? `；${extra}` : ""}` : `${r.hint}${extra && r.action !== "handoff" ? `；${extra}` : ""}`;
  });
  const projectWide = () => act(() => api.createCustomization(code, {
    source: "chat", text: `全项目口径：${intent.changes.map(([a, b]) => `“${a}”改为“${b}”`).join("；")}`.slice(0, 200),
  }), () => { setIntent(null); return "已转成定制项，请在“待你确认”里确认后全项目生效"; });

  return (
    <section className="pnl pad" aria-label="不满意刷新">
      <div className="sb"><span className="cap">改稿</span>{running && <span className="sub">重新起草中…</span>}</div>
      {intent?.intent === "事实" && <div className="note-amber" role="status">这处改动涉及事实{intent.marked ? `，已把引用同一证据的 ${intent.marked} 章标为需更新` : ""}。</div>}
      {intent?.ask_project_wide && (
        <div className="note-amber" role="status">
          这处改的是口径，全项目都这样改？
          <div className="row" style={{ gap: 6, marginTop: 6 }}>
            <button className="btn sm" disabled={busy} onClick={projectWide}>全项目都改</button>
            <button className="btn sm" onClick={() => setIntent(null)}>只改这一处</button>
          </div>
        </div>
      )}
      {rl.refresh.status === "failed" && <div className="err">上次刷新没有完成：{rl.refresh.error}</div>}
      {!open ? (
        <div className="row" style={{ gap: 6 }}>
          <button className="btn" disabled={busy || running} onClick={() => { setOpen(true); setMsg(""); }}>不满意，刷新</button>
          {cur?.refresh_rounds > 0 && cur.version > 1 && (
            <button className="btn sm" disabled={busy || running} onClick={() => act(() => api.rollbackSection(code, dcode, seq, cur.version - 1), (r) => `已回退，第 ${seq} 章现为 v${r.version}（人工定稿）`)}>回退到上一版</button>
          )}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div className="sub">先说哪里不满意：</div>
          {rl.reasons.map((r) => (
            <button key={r.reason} className="btn sm" disabled={busy} title={r.hint} onClick={() => refresh(r.reason, false)}>{r.reason}{r.action === "redraft" ? `（第 ${seq} 章）` : ""}</button>
          ))}
          <button className="btn sm" disabled={busy} onClick={() => refresh("写得不好", true)}>写得不好（整件，跳过人工定稿）</button>
          <button className="btn sm" onClick={() => setOpen(false)}>取消</button>
        </div>
      )}
      {msg && <div className="sub" role="status">{msg}</div>}
      {error && <div className="err" role="alert">{error}</div>}
    </section>
  );
}
