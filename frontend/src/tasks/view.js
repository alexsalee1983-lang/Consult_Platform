// 我的任务的分组与筛选（四期试用反馈：工作台并入我的任务）。纯函数，界面和侧栏角标共用一套口径
export const FILTER_KINDS = ["confirmation", "deliverable_review", "kb_content", "kb_compliance", "recheck", "deid", "deletion"];

// 本人可处理数 = 侧栏角标
export function actionableCount(tasks) {
  return (tasks || []).filter((t) => !t.blocked).length;
}

// 筛选条：先列七类固定类别，再列其余接上了且有数的类别；计数用后端给的口径
export function filterChips(kinds) {
  const keys = [...FILTER_KINDS.filter((k) => kinds[k]),
    ...Object.keys(kinds).filter((k) => !FILTER_KINDS.includes(k) && kinds[k].available && kinds[k].count > 0)];
  return keys.map((k) => ({ key: k, name: kinds[k].name, count: kinds[k].count }));
}

// 点选一类只看该类，再点同一类取消
export function toggleFilter(current, key) {
  return current === key ? "" : key;
}

// 分成今天、超出额度两段；filter 为空看全部。四期 A29：别人的卡隐藏，不灰显（后端已不返回，这里再兜一层）
export function splitTasks(tasks, filter) {
  const rows = (tasks || []).filter((t) => !t.blocked && (!filter || t.kind === filter));
  return { today: rows.filter((t) => !t.folded), later: rows.filter((t) => t.folded) };
}

