// 四期 A14②：D1、D2“按表单成稿”。顾问填表，平台按模板程序成稿（不调模型）；
// 资质边界段落由平台固定写入，Agent 起草的章节只进大纲。成稿后表单只读，之后在编辑器里改。
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { checkForm, emptyValues, joinRows, listCount, MAX_LIST_ITEMS, splitRows } from "../editor/form.js";

// 负责人 2026-11-26：每个输入框下方常驻填写示例（虚拟客户），预填后输入框不空也看得到
function Example({ text }) {
  return text ? <div className="sub form-ex">示例：{text}</div> : null;
}

// A28①：表格字段按行按格编辑，存回“每行一条、格间全角竖线”的文本（与后端一致）
function TableField({ f, value, onChange }) {
  const n = f.columns.length;
  const [rows, setRows] = useState(() => {
    const r = splitRows(value, n);
    return r.length ? r : [Array(n).fill("")];
  });
  const update = (next) => {
    setRows(next);
    onChange(joinRows(next));
  };
  return (
    <table className="tbl" aria-label={f.label}>
      <thead><tr>{f.columns.map((c) => <th key={c}>{c}</th>)}<th aria-label="操作" /></tr></thead>
      <tbody>
        {rows.map((cells, i) => (
          <tr key={i}>
            {cells.slice(0, n).map((c, j) => (
              <td key={j}>
                <input value={c} aria-label={`${f.columns[j]} 第 ${i + 1} 行`}
                  onChange={(e) => update(rows.map((r, k) => (k === i ? r.map((x, m) => (m === j ? e.target.value : x)) : r)))} />
              </td>
            ))}
            <td><button type="button" className="btn sm" onClick={() => update(rows.filter((_, k) => k !== i))}>删除</button></td>
          </tr>
        ))}
      </tbody>
      <tfoot><tr><td colSpan={n + 1}>
        <button type="button" className="btn sm" onClick={() => update([...rows, Array(n).fill("")])}>加一行</button>
      </td></tr></tfoot>
    </table>
  );
}

export default function DeliverableForm({ code, dcode, form, onDone, onCancel }) {
  const [values, setValues] = useState(() => emptyValues(form));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const errors = useMemo(() => checkForm(form, values), [form, values]);
  const bad = Object.keys(errors).length > 0;
  // A28②：从合同导入——后台由 Agent 抽取要点，回填后顾问逐项核对，再按表单成稿（成稿仍由程序生成）
  const [imp, setImp] = useState({ busy: false, msg: "", err: "" });
  const [gen, setGen] = useState(0);       // 回填后重建表格字段
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  async function importContract(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setImp({ busy: true, msg: "合同已上传，正在抽取要点…", err: "" });
    try {
      await api.importContract(code, dcode, file);
      for (let i = 0; i < 60 && alive.current; i++) {
        if (i) await new Promise((r) => setTimeout(r, 3000));
        const st = await api.contractImport(code, dcode);
        if (st.status === "failed") throw new Error(st.error || "抽取失败，可直接填表");
        if (st.status === "done") {
          const got = Object.entries(st.values || {}).filter(([k, v]) => v && k in values);
          setValues((x) => ({ ...x, ...Object.fromEntries(got) }));
          setGen((g) => g + 1);
          setImp({ busy: false, err: "", msg: got.length ? `已从合同回填 ${got.length} 项，请逐项核对后再成稿` : "合同里没有抽到可回填的内容，请直接填表" });
          return;
        }
      }
      if (alive.current) setImp({ busy: false, msg: "", err: "抽取还没完成，稍后可重新导入或直接填表" });
    } catch (x) {
      if (alive.current) setImp({ busy: false, msg: "", err: x.message });
    }
  }

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
      {form.code === "D1" && (
        <div className="sb" style={{ marginBottom: 8 }}>
          <label className="btn sm" aria-disabled={imp.busy}>
            {imp.busy ? "抽取中…" : "从合同导入"}
            <input type="file" hidden disabled={imp.busy || busy} onChange={importContract} accept=".docx,.pdf,.md,.txt" aria-label="合同文件" />
          </label>
          <span className="sub">{imp.err ? <span className="err">{imp.err}</span> : imp.msg || "上传合同，Agent 抽取目标、周期、范围、角色等回填表单；严格档项目不收合同原文"}</span>
        </div>
      )}
      <p className="sub">填好后平台按模板直接成稿（不调模型），每章生成第 1 版；之后在编辑器里逐章修改。内容先过机密级检测，不要填口令、IP、个人信息和客户原文。</p>
      {form.sections.map((s) => {
        const body = (
          <>
            {s.drafted_by_agent && <div className="sub">本章只进大纲，由 Agent 或顾问在编辑器里起草。</div>}
            {s.fields.map((f) => {
              const v = values[f.key];
              const err = touched && errors[f.key];
              const put = (nv) => setValues((x) => ({ ...x, [f.key]: nv }));
              if (f.type === "table") {
                return (
                  <div key={f.key} className="field" style={{ marginTop: 8 }}>
                    <span>{f.label}{f.required ? " *" : ""}</span>
                    <TableField key={`${f.key}-${gen}`} f={f} value={v} onChange={put} />
                    <span className="sub">{f.hint}{err ? <span className="err"> · {err}</span> : null}</span>
                    <Example text={f.example} />
                  </div>
                );
              }
              const set = (e) => put(e.target.value);
              const count = f.type === "list" ? `${listCount(v)}/${MAX_LIST_ITEMS} 项` : `${v.trim().length}/${f.max} 字`;
              return (
                <label key={f.key} className="field" style={{ display: "block", marginTop: 8 }}>
                  <span>{f.label}{f.required ? " *" : ""}</span>
                  {f.type === "text"
                    ? <input value={v} onChange={set} placeholder={f.example || f.hint || ""} aria-invalid={!!err} />
                    : <textarea rows={f.type === "list" ? 4 : 3} value={v} onChange={set} placeholder={f.example || f.hint || (f.type === "list" ? "每行一项" : "")} aria-invalid={!!err} />}
                  <span className="sub">{f.hint ? `${f.hint} · ` : ""}{count}{err ? <span className="err"> · {err}</span> : null}</span>
                  <Example text={f.example} />
                </label>
              );
            })}
            {s.fixed && <div className="note-amber" style={{ marginTop: 8 }}>平台固定写入：{s.fixed}</div>}
          </>
        );
        // A28①：没有必填项的章节已按产品配置和公司标准写法预填，默认收起；有未通过检查的字段时展开
        const needs = s.fields.some((f) => f.required || (touched && errors[f.key]));
        return needs || !s.fields.length
          ? <fieldset key={s.title} className="pnl pad" style={{ marginTop: 10 }}><legend className="cap">{s.title}</legend>{body}</fieldset>
          : (
            <details key={s.title} className="pnl pad" style={{ marginTop: 10 }}>
              <summary><span className="cap">{s.title}</span> <span className="sub">已预填 · 可展开修改</span></summary>
              {body}
            </details>
          );
      })}
      {error && <div className="err" role="alert">{error}</div>}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button className="btnp" onClick={submit} disabled={busy || (touched && bad)}>{busy ? "成稿中…" : "按表单成稿"}</button>
        {touched && bad && <span className="err">还有 {Object.keys(errors).length} 项未通过检查</span>}
      </div>
    </section>
  );
}
