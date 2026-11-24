// A16① 写稿助手 v2“选中即改”的纯函数（不依赖浏览器与 CodeMirror，可在 node 里测试）：
// 选中文字 → 助手改写 → 词级前后对比 → 接受（替换原区间，可回退）或放弃（正文不动）。
// 替换前核对原区间文字没变，变了就拒绝，免得把改写放到错位置；改写去掉的来源标注单独列出提醒顾问。
import { findRefs } from "./core.js";

// 来源标注整体一个词；英文数字连写一个词；空白一段；其余（汉字、标点）逐字
const TOKEN = /\[[^[\]\s]{1,64}\]|[A-Za-z0-9_.-]+|\s+|[\s\S]/gu;
const MAX_CELLS = 2_000_000;   // 逐词 DP 上限（约 1400×1400 词），超过整段替换

export function tokenize(s) {
  return s ? s.match(TOKEN) : [];
}

function push(out, op, t) {
  if (!t) return;
  const last = out[out.length - 1];
  if (last && last.op === op) last.t += t;
  else out.push({ op, t });
}

// 返回 [{op: "=" | "-" | "+", t}]，"=" 与 "-" 拼起来是原文，"=" 与 "+" 拼起来是改写
export function wordDiff(a, b) {
  const x = tokenize(a);
  const y = tokenize(b);
  const out = [];
  let p = 0;
  while (p < x.length && p < y.length && x[p] === y[p]) p++;
  let q = 0;
  while (q < x.length - p && q < y.length - p && x[x.length - 1 - q] === y[y.length - 1 - q]) q++;
  push(out, "=", x.slice(0, p).join(""));
  const xs = x.slice(p, x.length - q);
  const ys = y.slice(p, y.length - q);
  const n = xs.length;
  const m = ys.length;
  if (n * m > MAX_CELLS) {
    push(out, "-", xs.join(""));
    push(out, "+", ys.join(""));
  } else {
    // L[i][j] = xs[i:] 与 ys[j:] 的最长公共子序列长度
    const w = m + 1;
    const L = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        L[i * w + j] = xs[i] === ys[j] ? L[(i + 1) * w + j + 1] + 1 : Math.max(L[(i + 1) * w + j], L[i * w + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (xs[i] === ys[j]) { push(out, "=", xs[i]); i++; j++; }
      else if (L[(i + 1) * w + j] >= L[i * w + j + 1]) { push(out, "-", xs[i]); i++; }
      else { push(out, "+", ys[j]); j++; }
    }
    push(out, "-", xs.slice(i).join(""));
    push(out, "+", ys.slice(j).join(""));
  }
  push(out, "=", x.slice(x.length - q).join(""));
  return out;
}

// 删去与新增的字数
export function diffStats(segs) {
  const n = (op) => segs.filter((s) => s.op === op).reduce((k, s) => k + [...s.t].length, 0);
  return { removed: n("-"), added: n("+") };
}

// 原文有、改写里没有的来源标注（按出现顺序去重）
export function lostRefs(original, rewritten) {
  const kept = new Set(findRefs(rewritten).map((r) => r.ref));
  const out = [];
  for (const r of findRefs(original)) if (!kept.has(r.ref) && !out.includes(r.ref)) out.push(r.ref);
  return out;
}

// 文本框兜底：range = {from, to, text}；原区间文字没变才替换
export function applyReplace(text, range, replacement) {
  if (text.slice(range.from, range.to) !== range.text || range.to > text.length) return { ok: false };
  return { ok: true, text: text.slice(0, range.from) + replacement + text.slice(range.to), cursor: range.from + replacement.length };
}
