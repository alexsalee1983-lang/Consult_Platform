import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// 四期 A30 项目定制项：交付件页右栏。列出对本交付件生效的定制项（含被高优先级覆盖、不可接受的理由）、
// 受定制项变更影响“需更新”的章，并提供编辑器入口：只对当前这一章的一次性要求（形式类、口径类，提交即生效）。
const STATUS_CLS = { active: "c-g", pending_pm: "c-a", proposed: "c-a", unacceptable: "c-r" };
const SHOWN = new Set(["proposed", "pending_pm", "active", "unacceptable"]);

export default function Customizations({ code, dcode, seq, editable, onChanged }) {
  const [data, setData] = useState(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => api.customizations(code).then(setData).catch(() => setData(null)), [code]);
  useEffect(() => { load(); }, [load, dcode, seq]);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      await api.createCustomization(code, { source: "editor", text: text.trim(), deliverable: dcode, section: seq });
      setText("");
      await load();
      await onChanged?.();
    } catch (e) {
      setError(e.detail?.summary || e.message);
    } finally {
      setBusy(false);
    }
  }

  async function withdraw(id) {
    setError("");
    try {
      await api.withdrawCustomization(code, id);
      await load();
      await onChanged?.();
    } catch (e) {
      setError(e.message);
    }
  }

  if (!data) return null;
  const items = data.items.filter((i) => SHOWN.has(i.status) && (!i.deliverable || i.deliverable === dcode));
  const stale = data.needs_update[dcode] || [];
  return (
    <section className="pnl pad" aria-label="项目定制项">
      <div className="sb"><span className="cap">项目定制项</span><span className="mono">{items.length}</span></div>
      {stale.length > 0 && (
        <div className="note-amber" role="status">定制项有变更，第 {stale.join("、")} 章需更新；改写后提醒自动消失，签署前须重新评审。</div>
      )}
      <ul className="lst">
        {items.map((i) => (
          <li key={i.id} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
              <span className="mono">{i.code}</span>
              <span className={`chip ${STATUS_CLS[i.status] || ""}`}>{i.superseded ? "已被覆盖" : i.status_name}</span>
              <span className="sub">{i.layer} · {i.kind_name}{i.section ? ` · 第 ${i.section} 章一次性` : ""}</span>
            </div>
            <div>{i.text}</div>
            {i.reason && <div className="sub">{i.reason}</div>}
            {i.superseded && <div className="sub">与 C-{i.overridden_by} 冲突，按优先级以 C-{i.overridden_by} 为准</div>}
            {editable && i.status !== "unacceptable" && (
              <button className="btn sm" style={{ alignSelf: "flex-start" }} onClick={() => withdraw(i.id)}>撤回</button>
            )}
          </li>
        ))}
        {items.length === 0 && <li className="sub">本交付件没有定制项。客户要求可在 D1 客户要求栏或顾问对话里提。</li>}
      </ul>
      {editable && seq && (
        <>
          <textarea className="inp" rows={2} maxLength={500} aria-label="本章一次性要求" placeholder={`只对第 ${seq} 章的一次性要求（格式、口径），下次改写本章后用掉`}
            value={text} onChange={(e) => setText(e.target.value)} />
          {error && <div className="err" role="alert">{error}</div>}
          <button className="btn" disabled={busy || !text.trim()} onClick={submit}>加到本章</button>
        </>
      )}
    </section>
  );
}
