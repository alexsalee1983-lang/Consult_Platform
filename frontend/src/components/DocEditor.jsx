// A15 交付件章节编辑器：CodeMirror 6 Markdown 源码模式（决策记录 2026-11-21 ⑥），来源标注显示为标签、整体删除。
// 接口与原 textarea 同形：value、onChange(text)、placeholder；读写同一字符串、同一保存接口，后端不改。
// 颜色只用 theme.css 令牌。按需加载（Deliverable.jsx 用 React.lazy），不进首屏包。
import { useEffect, useRef } from "react";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorView, keymap, placeholder as placeholderExt, drawSelection } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { External, createState, docText, refDecorations } from "../editor/core.js";

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

export default function DocEditor({ value, onChange, placeholder = "", unresolved, ariaLabel = "章节正文编辑" }) {
  const host = useRef(null);
  const view = useRef(null);
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
        markdown({ base: markdownLanguage }),
        syntaxHighlighting(highlight),
        theme,
        placeholderExt(placeholder),
        refDecorations(unresolved || new Set()),
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
}
