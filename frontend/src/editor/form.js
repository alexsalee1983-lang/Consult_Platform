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

/** 返回 {字段 key: 错误文案}；空对象表示可以提交。 */
export function checkForm(form, values) {
  const errors = {};
  for (const f of formFields(form)) {
    const v = String(values?.[f.key] ?? "");
    const empty = f.type === "list" ? listCount(v) === 0 : !v.trim();
    if (f.required && empty) errors[f.key] = "必填";
    else if (v.trim().length > f.max) errors[f.key] = `不超过 ${f.max} 字`;
    else if (f.type === "list" && listCount(v) > MAX_LIST_ITEMS) errors[f.key] = `不超过 ${MAX_LIST_ITEMS} 项`;
  }
  return errors;
}
