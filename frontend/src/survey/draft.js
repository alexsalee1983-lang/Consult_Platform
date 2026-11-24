// 四期 E2②a：调研助手现场端的纯逻辑（草稿落盘策略、校验、回传请求体）。
// 普通档草稿存本机浏览器（按项目 + 包哈希），断网可继续填；严格档本机留存待负责人决定（#429），一律不落盘。
// 回传规则与平台侧 app/project/survey.py 一致：角色必填 ≤32 字、封闭题只收包里的选项（X = 不清楚）、开放题 ≤4000 字。

export const MAX_RESPONSES = 50;
export const MAX_ROLE = 32;
export const MAX_TEXT = 4000;
const PREFIX = "cp-survey:";

export const canPersist = (tier) => Boolean(tier) && tier !== "strict";
export const draftKey = (project, packHash) => `${PREFIX}${project}:${packHash}`;

const answered = (q, a) => (q.kind === "closed" ? Boolean(a?.value) : Boolean((a?.text || "").trim()));

export function saveDraft(store, pack, responses, savedAt) {
  if (!canPersist(pack?.tier)) return false;
  store.setItem(draftKey(pack.project, pack.pack_hash), JSON.stringify({ pack, responses, saved_at: savedAt }));
  return true;
}

function parse(raw) {
  try {
    const d = JSON.parse(raw);
    return d && d.pack && Array.isArray(d.responses) && canPersist(d.pack.tier) ? d : null;
  } catch {
    return null;
  }
}

export function loadDraft(store, project, packHash) {
  const raw = store.getItem(draftKey(project, packHash));
  return raw == null ? null : parse(raw);
}

export function clearDraft(store, project, packHash) {
  store.removeItem(draftKey(project, packHash));
}

// 本项目在本机的草稿：[{code, pack_hash, saved_at, responses}]
export function listDrafts(store, project) {
  const out = [];
  for (let i = 0; i < store.length; i += 1) {
    const k = store.key(i);
    if (!k || !k.startsWith(`${PREFIX}${project}:`)) continue;
    const d = parse(store.getItem(k));
    if (d) out.push({ code: d.pack.code, pack_hash: d.pack.pack_hash, saved_at: d.saved_at, responses: d.responses.length });
  }
  return out.sort((a, b) => a.code.localeCompare(b.code));
}

export function progress(pack, response) {
  const done = pack.questions.filter((q) => answered(q, response.answers?.[q.qid])).length;
  return { done, total: pack.questions.length };
}

export function checkResponses(pack, responses) {
  if (!responses.length) return ["至少要有一份答卷"];
  if (responses.length > MAX_RESPONSES) return [`一次最多回传 ${MAX_RESPONSES} 份答卷`];
  const errs = [];
  responses.forEach((r, i) => {
    const n = `第 ${i + 1} 份答卷`;
    const role = (r.respondent_role || "").trim();
    if (!role) errs.push(`${n}：请填答卷人角色`);
    else if (role.length > MAX_ROLE) errs.push(`${n}：答卷人角色不超过 ${MAX_ROLE} 字（写角色不写姓名）`);
    if (progress(pack, r).done === 0) {
      errs.push(`${n}：一题都没答`);
      return;
    }
    for (const q of pack.questions) {
      const a = r.answers?.[q.qid];
      if (!answered(q, a)) continue;
      if (q.kind === "closed" && !q.options.some((o) => o.key === a.value)) errs.push(`${n} ${q.qid}：选项不在题目里`);
      if (q.kind === "open" && a.text.trim().length > MAX_TEXT) errs.push(`${n} ${q.qid}：回答超过 ${MAX_TEXT} 字`);
    }
  });
  return errs;
}

export function resultsBody(pack, responses) {
  return {
    pack_hash: pack.pack_hash,
    responses: responses.map((r) => ({
      respondent_role: (r.respondent_role || "").trim(),
      answers: pack.questions.filter((q) => answered(q, r.answers?.[q.qid])).map((q) => {
        const a = r.answers[q.qid];
        return q.kind === "closed" ? { qid: q.qid, value: a.value, text: "" } : { qid: q.qid, value: "", text: a.text.trim() };
      }),
    })),
  };
}
