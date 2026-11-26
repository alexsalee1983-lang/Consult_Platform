// 四期 A14②：D1、D2 表单成稿的前端校验（与后端 app/project/forms.py 的规则一致，后端仍最终把关）
export const MAX_LIST_ITEMS = 50;

export function formFields(form) {
  return (form?.sections || []).flatMap((s) => s.fields || []);
}

export function emptyValues(form) {
  return Object.fromEntries(formFields(form).map((f) => [f.key, f.default || ""]));   // #468 产品配置预填
}

export function listCount(v) {
  return String(v || "").split("\n").map((x) => x.trim().replace(/^[-*•]+/, "").trim()).filter(Boolean).length;
}

// A28①：表格字段每行一条、格间用全角竖线“｜”（与后端一致）；编辑器里按行、按格编辑
export function splitRows(v, n) {
  return String(v || "").split("\n").map((line) => line.split("｜").map((c) => c.trim()))
    .filter((cells) => cells.some(Boolean))
    .map((cells) => [...cells, ...Array(Math.max(0, n - cells.length)).fill("")]);
}

export function joinRows(rows) {
  return rows.filter((cells) => cells.some((c) => String(c).trim())).map((cells) => cells.join("｜")).join("\n");
}

function count(f, v) {
  if (f.type === "list") return listCount(v);
  if (f.type === "table") return splitRows(v, f.columns?.length || 1).length;
  return v.trim() ? 1 : 0;
}

/** 返回 {字段 key: 错误文案}；空对象表示可以提交。 */
export function checkForm(form, values) {
  const errors = {};
  for (const f of formFields(form)) {
    const v = String(values?.[f.key] ?? "");
    const multi = f.type === "list" || f.type === "table";
    if (f.required && count(f, v) === 0) errors[f.key] = "必填";
    else if (v.trim().length > f.max) errors[f.key] = `不超过 ${f.max} 字`;
    else if (multi && count(f, v) > MAX_LIST_ITEMS) errors[f.key] = `不超过 ${MAX_LIST_ITEMS} ${f.type === "table" ? "行" : "项"}`;
  }
  return errors;
}
