// A15 编辑器内核（不依赖浏览器，可在 node 里测试）：
// 正文就是带方括号来源标注的 Markdown 文本，编辑器读入与取出必须逐字节一致；
// 来源标注识别规则与后端 app/project/deliverables.py 的 _EVIDENCE_REF、_KB_REF、_INFERRED 一致（共用 refSamples.json 测试）；
// 用户删除碰到半个标注时整体删除（决策记录 2026-11-21 ⑧），不加确认。
import { Annotation, EditorSelection, EditorState, StateField } from "@codemirror/state";
import { Decoration, EditorView } from "@codemirror/view";

const EVIDENCE_REF = /\[((?:I|D|O|S)-\d{2,4})(-\d{1,3})?\]/g;
const KB_REF = /[[｜|]\s*(L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5})\s*[\]】]/g;
const INFERRED = /\[(?:推断|模型推断)\]/g;

// 外部同步（父组件换了正文）带这个标记，不做整体删除扩展
export const External = Annotation.define();

// 返回按位置排序的标注：{from, to, kind: "ev"|"kb"|"inf", ref}
export function findRefs(text) {
  const out = [];
  for (const [re, kind] of [[EVIDENCE_REF, "ev"], [KB_REF, "kb"], [INFERRED, "inf"]]) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(text)) !== null) out.push({ from: m.index, to: m.index + m[0].length, kind, ref: m[1] || "推断" });
  }
  out.sort((a, b) => a.from - b.from || a.to - b.to);
  return out.filter((r, i) => i === 0 || r.from >= out[i - 1].to);   // 防重叠（RangeSet 要求有序）
}

// 当前正文里的标注；编辑器装饰与原子范围都从这里取
export const refsField = StateField.define({
  create: (state) => findRefs(state.sliceDoc()),
  update: (refs, tr) => (tr.docChanged ? findRefs(tr.state.sliceDoc()) : refs),
});

// 删除或替换只盖住标注一部分时，扩展到整个标注
const wholeRefDelete = EditorState.transactionFilter.of((tr) => {
  if (!tr.docChanged || tr.annotation(External)) return tr;
  const refs = tr.startState.field(refsField, false);
  if (!refs || refs.length === 0) return tr;
  let widened = false;
  const specs = [];
  tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    let from = fromA;
    let to = toA;
    if (to > from) {
      for (const r of refs) {
        if (r.from < to && r.to > from && (r.from < from || r.to > to)) {
          from = Math.min(from, r.from);
          to = Math.max(to, r.to);
          widened = true;
        }
      }
    }
    const last = specs[specs.length - 1];
    if (last && from <= last.to) {
      last.to = Math.max(last.to, to);
      last.insert += inserted.sliceString(0, inserted.length, tr.startState.lineBreak);
    } else specs.push({ from, to, insert: inserted.sliceString(0, inserted.length, tr.startState.lineBreak) });
  });
  if (!widened) return tr;
  const changes = tr.startState.changes(specs);
  const event = ["delete.backward", "delete.forward", "delete.selection", "delete.cut", "delete", "input.paste", "input.type", "input"]
    .find((e) => tr.isUserEvent(e));
  return { changes, selection: tr.startState.selection.map(changes, 1), userEvent: event, scrollIntoView: tr.scrollIntoView };
});

// 换行符按原文保留：含 \r\n 的正文用 \r\n 分行，其余用 \n；其他换行字符留在行内，取出时原样
export function lineSeparatorFor(text) {
  return text.includes("\r\n") ? "\r\n" : "\n";
}

export function coreExtensions(text) {
  return [EditorState.lineSeparator.of(lineSeparatorFor(text)), refsField, wholeRefDelete];
}

// 取出正文：必须用 sliceDoc（按 lineSeparator 拼行）；doc.toString() 一律用 LF 拼行，会丢 CRLF
export function docText(state) {
  return state.sliceDoc();
}

export function createState(text, extensions = []) {
  return EditorState.create({ doc: text, extensions: [coreExtensions(text), extensions] });
}

// 写稿侧栏“插入”：在当前选区插入（来源标注原样保留），光标停在插入内容之后
export function insertWithSources(state, snippet) {
  return {
    ...state.changeByRange((range) => ({
      changes: { from: range.from, to: range.to, insert: snippet },
      range: EditorSelection.cursor(range.from + snippet.length),
    })),
    userEvent: "input.insert",
    scrollIntoView: true,
  };
}

const MARK = { ev: "src-tag src-ev", kb: "src-tag src-kb", inf: "src-tag src-inf" };

// 来源标签装饰（颜色语义同只读渲染 DocBody：绿 = 客户证据，青 = 知识库，紫 = 推断，核对不上猩红）与原子范围
export function refDecorations(unresolved = new Set()) {
  const deco = StateField.define({
    create: (state) => build(state.field(refsField)),
    update: (value, tr) => (tr.docChanged ? build(tr.state.field(refsField)) : value),
    provide: (f) => [EditorView.decorations.from(f), EditorView.atomicRanges.of((view) => view.state.field(f))],
  });
  function build(refs) {
    return Decoration.set(refs.map((r) => Decoration.mark({
      class: unresolved.has(r.ref) ? "src-tag src-bad" : MARK[r.kind],
      attributes: unresolved.has(r.ref) ? { title: "核对不上：项目里没有这条证据或知识" } : {},
    }).range(r.from, r.to)));
  }
  return deco;
}
