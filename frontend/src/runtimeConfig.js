// 运行层配置编辑（四期 G3③）：页面规则，与后端 app/services/runtime_config.py 保持一致。
// 理由只在后端存表，审计只记长度；页面不显示密钥（运行层本来就不含密钥）。
export const STATUS = { pending: "待二次确认", applied: "已生效", rejected: "已驳回" };
export const STATUS_CLS = { pending: "c-a", applied: "c-g", rejected: "c-n" };
export const REASON_MAX = 500;
export const MAX_KEYS = 40;

// 按当前值的类型解析输入框文字；布尔用下拉框传 "true" / "false"。
export function parseValue(current, text) {
  const t = String(text ?? "").trim();
  if (typeof current === "boolean") return { value: t === "true" };
  if (typeof current === "number") {
    const n = Number(t);
    if (t === "" || !Number.isFinite(n)) return { error: "请填数字" };
    return { value: n };
  }
  if (!t) return { error: "不能为空" };
  return { value: t };
}

// drafts：键 → 输入框文字；只收集与当前值不同的键。
export function buildChanges(items, drafts) {
  const values = {};
  const errors = {};
  for (const it of items) {
    if (!(it.key in drafts)) continue;
    const r = parseValue(it.value, drafts[it.key]);
    if (r.error) errors[it.key] = r.error;
    else if (r.value !== it.value) values[it.key] = r.value;
  }
  return { values, errors };
}

export function validateChange(reason, values) {
  const r = (reason || "").trim();
  if (!r) return "必须填写改动理由";
  if (r.length > REASON_MAX) return `理由不超过 ${REASON_MAX} 字`;
  const n = Object.keys(values || {}).length;
  if (n === 0) return "至少改一项";
  if (n > MAX_KEYS) return `一次最多改 ${MAX_KEYS} 项`;
  return null;
}

// 发起人本人不能确认或驳回（后端同样拒绝，这里先把按钮置灰）
export function canDecide(version, me) {
  return version.status === "pending" && version.requested_by !== me;
}

// 可回滚到的版本：已生效的版本号（新的在前）+ 0（全部恢复 .env）
export function rollbackTargets(versions) {
  return [...versions.filter((v) => v.status === "applied").map((v) => v.id), 0];
}

export function versionTitle(v) {
  if (v.rollback_to === null || v.rollback_to === undefined) return `版本 ${v.id}：改动`;
  if (v.rollback_to === 0) return `版本 ${v.id}：全部恢复 .env`;
  return `版本 ${v.id}：回滚到版本 ${v.rollback_to}`;
}

export function valueText(v) {
  if (v === null || v === undefined) return "（恢复 .env）";
  if (typeof v === "boolean") return v ? "是" : "否";
  return String(v);
}
