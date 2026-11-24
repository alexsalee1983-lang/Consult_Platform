// A16② 写稿助手 v2：编辑器里输入 @ 弹出引用候选（项目资料/证据、知识条目、本交付件已有正文的章节），
// 输入 [ 弹出编号补全（证据编号、知识条款/控制点编号）。选中后插入现有格式的来源标注，后端核对规则不变。
// 候选只来自当前用户已有权限的接口（项目证据列表按成员校验、知识检索按用户可见范围），这里不另开接口。
// 纯逻辑与 CodeMirror 扩展放一起，可在 node 里测试；弹层颜色只用 theme.css 令牌（.mention-pop）。
import { Facet, Prec, StateEffect, StateField } from "@codemirror/state";
import { EditorView, keymap, showTooltip } from "@codemirror/view";
import { KB_REF_PATTERN } from "../kbRef.js";

const KB_FULL = new RegExp(`^${KB_REF_PATTERN}$`);
const CODE_CHARS = /^[A-Za-z0-9-]*$/;
const MAX_BACK = 40;
export const MAX_ITEMS = 8;

// 光标前同一行里最近的 @ 或 [：{kind, from, query}；中间遇到空白或 ] 就不算
export function findTrigger(text, pos) {
  for (let i = pos - 1; i >= 0 && i >= pos - MAX_BACK; i--) {
    const ch = text[i];
    if (/\s/.test(ch) || ch === "]") return null;
    if (ch !== "@" && ch !== "[") continue;
    const query = text.slice(i + 1, pos);
    if (ch === "@") return i > 0 && /[A-Za-z0-9]/.test(text[i - 1]) ? null : { kind: "@", from: i, query };
    return CODE_CHARS.test(query) ? { kind: "[", from: i, query } : null;
  }
  return null;
}

// 候选统一形状 {kind: "ev"|"kb"|"sec", code, label, insert}
export function buildCandidates({ evidences = [], kb = [], sections = [], currentSeq = null } = {}) {
  const out = [];
  const seen = new Set();
  for (const e of evidences) {
    if (e.status === "deleted" || seen.has(e.code)) continue;
    seen.add(e.code);
    out.push({ kind: "ev", code: e.code, label: e.title || "", insert: `[${e.code}]` });
  }
  for (const k of kb) {
    if (!KB_FULL.test(k.doc_id || "") || seen.has(k.doc_id)) continue;
    seen.add(k.doc_id);
    out.push({ kind: "kb", code: k.doc_id, label: k.title || "", insert: `[${k.doc_id}]` });
  }
  for (const s of sections) {
    if (!(s.current_version > 0) || s.seq === currentSeq) continue;
    out.push({ kind: "sec", code: `第 ${s.seq} 章`, label: s.title || "", insert: `见第 ${s.seq} 章“${s.title}”` });
  }
  return out;
}

// @ 按编号或标题包含匹配；[ 只按编号匹配、不出章节；编号开头匹配的排前面
export function filterCandidates(cands, kind, query) {
  const q = query.toLowerCase();
  const hit = kind === "["
    ? cands.filter((c) => c.kind !== "sec" && c.code.toLowerCase().includes(q))
    : cands.filter((c) => `${c.code} ${c.label}`.toLowerCase().includes(q));
  const head = hit.filter((c) => c.code.toLowerCase().startsWith(q));
  return [...head, ...hit.filter((c) => !c.code.toLowerCase().startsWith(q))].slice(0, MAX_ITEMS);
}

// 把触发符到光标的文字换成引用，光标停在引用之后
export function applyCandidate(state, trig, cand) {
  const to = state.selection.main.head;
  return {
    changes: { from: trig.from, to, insert: cand.insert },
    selection: { anchor: trig.from + cand.insert.length },
    userEvent: "input.complete",
    scrollIntoView: true,
  };
}

// 候选来源：() => 候选数组（DocEditor 按 props 组装）
export const mentionSource = Facet.define({ combine: (v) => v[0] || (() => []) });
export const refreshMention = StateEffect.define();
const dismiss = StateEffect.define();
const move = StateEffect.define();

const EMPTY = { trig: null, items: [], active: 0, dismissedFrom: -1, tooltip: null };

function currentTrigger(state) {
  const r = state.selection.main;
  if (!r.empty) return null;
  const line = state.doc.lineAt(r.head);
  const t = findTrigger(line.text, r.head - line.from);
  return t && { ...t, from: t.from + line.from };
}

export const mentionField = StateField.define({
  create: () => EMPTY,
  update(v, tr) {
    for (const e of tr.effects) {
      if (e.is(dismiss)) return { ...EMPTY, dismissedFrom: v.trig ? v.trig.from : -1 };
      if (e.is(move) && v.items.length) return withTooltip({ ...v, active: (v.active + e.value + v.items.length) % v.items.length });
    }
    const refreshed = tr.effects.some((e) => e.is(refreshMention));
    if (!tr.docChanged && !tr.selection && !refreshed) return v;
    const trig = currentTrigger(tr.state);
    if (!trig) return EMPTY;
    const dismissedFrom = v.dismissedFrom >= 0 && tr.docChanged ? tr.changes.mapPos(v.dismissedFrom, 1) : v.dismissedFrom;
    if (trig.from === dismissedFrom) return { ...EMPTY, dismissedFrom };
    const items = filterCandidates(tr.state.facet(mentionSource)(), trig.kind, trig.query);
    const same = v.trig && v.trig.from === trig.from && v.trig.query === trig.query;
    const active = same ? Math.min(v.active, Math.max(items.length - 1, 0)) : 0;
    return withTooltip({ trig, items, active, dismissedFrom: -1 });
  },
  provide: (f) => showTooltip.from(f, (v) => v.tooltip),
});

const KIND_NAME = { ev: "证据", kb: "知识", sec: "章节" };

function withTooltip(v) {
  if (!v.items.length) return { ...v, tooltip: null };
  return {
    ...v,
    tooltip: {
      pos: v.trig.from,
      above: false,
      arrow: false,
      create: (view) => {
        const dom = document.createElement("ul");
        dom.className = "mention-pop";
        dom.setAttribute("role", "listbox");
        dom.setAttribute("aria-label", v.trig.kind === "@" ? "引用候选" : "编号补全");
        v.items.forEach((c, i) => {
          const li = document.createElement("li");
          li.setAttribute("role", "option");
          li.setAttribute("aria-selected", String(i === v.active));
          li.className = i === v.active ? "on" : "";
          const tag = document.createElement("span");
          tag.className = `mention-kind k-${c.kind}`;
          tag.textContent = KIND_NAME[c.kind];
          const code = document.createElement("span");
          code.className = "mono";
          code.textContent = c.code;
          const label = document.createElement("span");
          label.className = "sub";
          label.textContent = c.label;
          li.append(tag, code, label);
          li.addEventListener("mousedown", (e) => {
            e.preventDefault();
            view.dispatch(applyCandidate(view.state, v.trig, c));
          });
          dom.append(li);
        });
        return { dom };
      },
    },
  };
}

function open(view) {
  return view.state.field(mentionField, false)?.items.length > 0;
}

// 只在弹层打开时拦截按键，否则交给默认键位（返回 false）
export const mentionKeys = {
  down: (view) => (open(view) ? (view.dispatch({ effects: move.of(1) }), true) : false),
  up: (view) => (open(view) ? (view.dispatch({ effects: move.of(-1) }), true) : false),
  enter: (view) => {
    if (!open(view)) return false;
    const v = view.state.field(mentionField);
    view.dispatch(applyCandidate(view.state, v.trig, v.items[v.active]));
    return true;
  },
  escape: (view) => (open(view) ? (view.dispatch({ effects: dismiss.of(null) }), true) : false),
};

// getCandidates：() => 候选数组；onQuery(kind, query)：触发或查询变了时回调（父组件据此异步检索知识，回来后派发 refreshMention）
export function mentionComplete(getCandidates, onQuery) {
  return [
    mentionSource.of(getCandidates),
    mentionField,
    Prec.highest(keymap.of([
      { key: "ArrowDown", run: mentionKeys.down },
      { key: "ArrowUp", run: mentionKeys.up },
      { key: "Enter", run: mentionKeys.enter },
      { key: "Tab", run: mentionKeys.enter },
      { key: "Escape", run: mentionKeys.escape },
    ])),
    onQuery ? EditorView.updateListener.of((u) => {
      const a = u.startState.field(mentionField, false)?.trig;
      const b = u.state.field(mentionField, false)?.trig;
      if (b && (!a || a.from !== b.from || a.query !== b.query)) onQuery(b.kind, b.query);
    }) : [],
  ];
}
