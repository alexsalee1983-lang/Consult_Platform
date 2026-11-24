// 四期 A14②：D1、D2“按表单成稿”。顾问填表，平台按模板程序成稿（不调模型）；
// 资质边界段落由平台固定写入，Agent 起草的章节只进大纲。成稿后表单只读，之后在编辑器里改。
import { useMemo, useState } from "react";
import { api } from "../api.js";
import { checkForm, emptyValues, listCount, MAX_LIST_ITEMS } from "../editor/form.js";

export default function DeliverableForm({ code, dcode, form, onDone, onCancel }) {
  const [values, setValues] = useState(() => emptyValues(form));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const errors = useMemo(() => checkForm(form, values), [form, values]);
  const bad = Object.keys(errors).length > 0;

  async function submit() {
    setTouched(true);
    if (bad) return;
    setBusy(true);
    setError("");
    try {
      await api.postDeliverableForm(code, dcode, values);
      await onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="按表单成稿">
      <div className="sb">
        <span className="cap">按表单成稿 · {form.code} {form.title}</span>
        <button className="btn sm" onClick={onCancel} disabled={busy}>收起</button>
      </div>
      <p className="sub">填好后平台按模板直接成稿（不调模型），每章生成第 1 版；之后在编辑器里逐章修改。内容先过机密级检测，不要填口令、IP、个人信息和客户原文。</p>
      {form.sections.map((s) => (
        <fieldset key={s.title} className="pnl pad" style={{ marginTop: 10 }}>
          <legend className="cap">{s.title}</legend>
          {s.drafted_by_agent && <div className="sub">本章只进大纲，由 Agent 或顾问在编辑器里起草。</div>}
          {s.fields.map((f) => {
            const v = values[f.key];
            const err = touched && errors[f.key];
            const set = (e) => setValues((x) => ({ ...x, [f.key]: e.target.value }));
            const count = f.type === "list" ? `${listCount(v)}/${MAX_LIST_ITEMS} 项` : `${v.trim().length}/${f.max} 字`;
            return (
              <label key={f.key} className="field" style={{ display: "block", marginTop: 8 }}>
                <span>{f.label}{f.required ? " *" : ""}</span>
                {f.type === "text"
                  ? <input value={v} onChange={set} placeholder={f.hint || ""} aria-invalid={!!err} />
                  : <textarea rows={f.type === "list" ? 4 : 3} value={v} onChange={set} placeholder={f.hint || (f.type === "list" ? "每行一项" : "")} aria-invalid={!!err} />}
                <span className="sub">{count}{err ? <span className="err"> · {err}</span> : null}</span>
              </label>
            );
          })}
          {s.fixed && <div className="note-amber" style={{ marginTop: 8 }}>平台固定写入：{s.fixed}</div>}
        </fieldset>
      ))}
      {error && <div className="err" role="alert">{error}</div>}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button className="btnp" onClick={submit} disabled={busy || (touched && bad)}>{busy ? "成稿中…" : "按表单成稿"}</button>
        {touched && bad && <span className="err">还有 {Object.keys(errors).length} 项未通过检查</span>}
      </div>
    </section>
  );
}
