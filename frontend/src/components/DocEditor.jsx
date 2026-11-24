// A15 交付件章节编辑器：CodeMirror 6 Markdown 源码模式（决策记录 2026-11-21 ⑥），来源标注显示为标签、整体删除。
// 接口与原 textarea 同形：value、onChange(text)、placeholder；读写同一字符串、同一保存接口，后端不改。
// 颜色只用 theme.css 令牌。按需加载（Deliverable.jsx 用 React.lazy），不进首屏包。
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { HighlightStyle, Language, defineLanguageFacet, syntaxHighlighting } from "@codemirror/language";
import { GFM, parser as mdParser } from "@lezer/markdown";
import { EditorView, keymap, placeholder as placeholderExt, drawSelection } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { External, createState, docText, insertWithSources, refDecorations, replaceRangeIfUnchanged, selectionAsk } from "../editor/core.js";

// 只取 @lezer/markdown 语法树做高亮：@codemirror/lang-markdown 会静态拉入 lang-html/css/javascript（嵌套代码高亮用），
// 交付件正文用不到，去掉后编辑器块 gzip 179.5 KB 降到 105.0 KB。
const mdLanguage = new Language(defineLanguageFacet({}), mdParser.configure(GFM), [], "markdown");

const theme = EditorView.theme({
  "&": { background: "var(--s2)", color: "var(--tx)", border: "1px solid var(--ln2)", borderRadius: "6px", fontSize: "14px", minHeight: "420px" },
  "&.cm-focused": { outline: "none", borderColor: "var(--cyan)" },
  ".cm-scroller": { fontFamily: '"JetBrains Mono", ui-monospace, monospace', lineHeight: "1.7", maxHeight: "70vh" },
  ".cm-content": { padding: "8px 10px", caretColor: "var(--tx)" },
  ".cm-cursor": { borderLeftColor: "var(--tx)" },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": { background: "var(--ln2)" },
  ".cm-placeholder": { color: "var(--tx3)" },
  ".src-tag": { borderRadius: "4px", padding: "0 2px" },
  ".src-ev": { background: "var(--ok-d)" },
  ".src-kb": { background: "var(--cyan-d)" },
  ".src-inf": { background: "var(--vio-d)" },
  ".src-bad": { background: "var(--crim-d)" },
}, { dark: true });

const highlight = HighlightStyle.define([
  { tag: tags.heading, color: "var(--amber)", fontWeight: "700" },
  { tag: tags.strong, fontWeight: "700" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: [tags.link, tags.url], color: "var(--cyan)" },
  { tag: tags.monospace, color: "var(--tx2)" },
  { tag: [tags.processingInstruction, tags.meta, tags.contentSeparator], color: "var(--tx3)" },
  { tag: tags.quote, color: "var(--tx2)" },
]);

// ref 暴露给写稿侧栏（A15②）：selection() 取选中文字，insert(snippet) 在光标处插入或替换选区（来源标注原样保留）
const DocEditor = forwardRef(function DocEditor({ value, onChange, placeholder = "", unresolved, ariaLabel = "章节正文编辑", onAsk }, ref) {
  const host = useRef(null);
  const view = useRef(null);
  useImperativeHandle(ref, () => ({
    selection() {
      const v = view.current;
      if (!v) return "";
      const r = v.state.selection.main;
      return v.state.sliceDoc(r.from, r.to);
    },
    insert(snippet) {
      const v = view.current;
      if (!v) return;
      v.dispatch(insertWithSources(v.state, snippet));
      v.focus();
    },
    // A16①：当前选区 {from, to, text}；按区间替换（原文没变才换，返回是否成功，可 Ctrl+Z 撤销）
    range() {
      const v = view.current;
      if (!v) return null;
      const r = v.state.selection.main;
      return { from: r.from, to: r.to, text: v.state.sliceDoc(r.from, r.to) };
    },
    replaceRange(from, to, original, snippet) {
      const v = view.current;
      const spec = v && replaceRangeIfUnchanged(v.state, from, to, original, snippet);
      if (!spec) return false;
      v.dispatch(spec);
      v.focus();
      return true;
    },
  }), []);
  const onAskRef = useRef(onAsk);
  onAskRef.current = onAsk;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    const v = new EditorView({
      parent: host.current,
      state: createState(value, [
        history(),
        drawSelection(),
        EditorView.lineWrapping,
        keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
        mdLanguage,
        syntaxHighlighting(highlight),
        theme,
        placeholderExt(placeholder),
        refDecorations(unresolved || new Set()),
        selectionAsk((r) => onAskRef.current?.(r)),
        EditorView.contentAttributes.of({ "aria-label": ariaLabel, spellcheck: "false" }),
        EditorView.updateListener.of((u) => {
          if (u.docChanged && !u.transactions.some((t) => t.annotation(External))) onChangeRef.current(docText(u.state));
        }),
      ]),
    });
    view.current = v;
    return () => v.destroy();
    // 只在挂载时建一次；外部值变化走下面的同步
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 外部值与文档不同才替换，避免光标跳动
  useEffect(() => {
    const v = view.current;
    if (v && docText(v.state) !== value) {
      v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value }, annotations: External.of(true) });
    }
  }, [value]);

  return <div ref={host} className="doc-editor" />;
});

export default DocEditor;
