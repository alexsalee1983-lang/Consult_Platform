// 四期 A41（docs/交付链输入输出方案.md 8.1“顾问入口收敛”）：驾驶舱右侧抽屉，挂在项目路由外层、各页共用。
// ① 对话：在哪页打开就带哪页上下文，同一项目同一会话（沿用最近一次咨询师对话）；独立的顾问对话屏保留给完整功能。
// ② 待我处理：复用“我的任务”，只列本项目，一行一件事，点了跳过去办。
// 用户消息可“转为定制项”，进同一个定制项队列（A30），回显确认后生效。
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router";
import { api, sendMessage } from "../api.js";
import { DRAWER_AGENT, pageContext, pickConversation, projectTodos } from "../cockpitDrawer.js";
import Drawer from "./Drawer.jsx";

const fromServer = (m) => ({ role: m.role === "user" ? "user" : "ai", id: m.id, content: m.content, interrupted: m.status && m.status !== "ok" && m.role !== "user" });

function Todos({ items, onGo }) {
  if (items === null) return <div className="sub">加载中……</div>;
  if (items.length === 0) return <div className="sub">本项目暂时没有要你处理的事。</div>;
  return (
    <ul className="conv-list" aria-label="待我处理">
      {items.map((t) => (
        <li key={`${t.kind}:${t.ref_id}`}>
          <Link to={t.link} onClick={onGo} className="row" style={{ gap: 6, flexWrap: "wrap", textDecoration: "none", color: "var(--tx)" }}>
            <span className="chip c-a">{t.kindName}</span>
            <span>{t.title}</span>
            {t.blocked && <span className="sub">卡在：{t.blocked}</span>}
          </Link>
        </li>
      ))}
    </ul>
  );
}

export default function AdvisorDrawer({ code }) {
  const { pathname } = useLocation();
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("chat");
  const [todos, setTodos] = useState(null);
  const [conv, setConv] = useState(null);
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [custNote, setCustNote] = useState({});
  const scrollerRef = useRef(null);
  const page = pageContext(pathname);

  useEffect(() => {
    setTodos(null);
    api.myTasks().then((d) => setTodos(projectTodos(d, code))).catch(() => setTodos([]));
  }, [code, pathname]);

  // 切项目时重新取会话；同一项目里切页不换会话
  useEffect(() => {
    setConv(null); setMessages([]); setCustNote({}); setError("");
    if (!open) return;
    let alive = true;
    api.conversations(code).then(async (list) => {
      const c = pickConversation(list);
      if (!c || !alive) return;
      const d = await api.conversation(c.id);
      if (alive) { setConv(d); setMessages(d.messages.map(fromServer)); }
    }).catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [code, open]);

  useEffect(() => {
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const close = useCallback(() => setOpen(false), []);

  async function toCustomization(i, content) {
    try {
      const r = await api.createCustomization(code, { source: "chat", text: content });
      const bad = r.items.filter((x) => x.status === "unacceptable").length;
      setCustNote((n) => ({ ...n, [i]: `已拆成 ${r.items.length} 条定制项，${r.items.length - bad} 张确认卡待处理${bad ? `，${bad} 条不可接受` : ""}` }));
    } catch (e) {
      setCustNote((n) => ({ ...n, [i]: `没有转成定制项：${e.detail?.summary || e.message}` }));
    }
  }

  async function send() {
    const content = text.trim();
    if (!content || sending) return;
    setText(""); setError(""); setSending(true);
    let current = conv, started = false;
    const patchLast = (fn) => setMessages((m) => [...m.slice(0, -1), fn(m[m.length - 1])]);
    try {
      if (!current) { current = await api.createConversation(code, DRAWER_AGENT); setConv(current); }
      setMessages((m) => [...m, { role: "user", content }]);
      const done = await sendMessage(current.id, content, (delta) => {
        if (!started) { started = true; setMessages((m) => [...m, { role: "ai", content: delta, streaming: true }]); }
        else patchLast((last) => ({ ...last, content: last.content + delta }));
      }, undefined, page);
      if (started) patchLast((last) => ({ ...last, id: done.message_id, streaming: false }));
    } catch (e) {
      if (started) patchLast((last) => ({ ...last, streaming: false, interrupted: true }));
      if (e.status === 422 && e.detail?.findings) {
        // 被机密级拦截的提问不保存：撤下，换成拦截说明
        setMessages((m) => [...m.slice(0, -1), { role: "blocked", content: e.detail.summary }]);
      } else {
        setMessages((m) => [...m, { role: "error", content: e.message }]);
      }
    } finally {
      setSending(false);
    }
  }

  const n = todos?.length || 0;
  return (
    <>
      <button type="button" className="btnp" aria-label="打开顾问对话与待我处理" onClick={() => setOpen(true)}
        style={{ position: "fixed", right: 20, bottom: 20, zIndex: 15 }}>
        顾问对话{n > 0 && <span className="chip c-a" style={{ marginLeft: 6 }}>待我处理 {n}</span>}
      </button>
      {open && (
        <Drawer title="顾问对话" onClose={close} more={{ to: `/chat/${encodeURIComponent(code)}`, label: "在完整对话页打开" }}>
          <nav className="tabs" aria-label="抽屉分类">
            <button type="button" className={tab === "chat" ? "on" : undefined} onClick={() => setTab("chat")}>对话</button>
            <button type="button" className={tab === "todo" ? "on" : undefined} onClick={() => setTab("todo")}>待我处理（{n}）</button>
          </nav>
          {tab === "todo" ? <Todos items={todos} onGo={close} /> : (
            <>
              <div className="sub" role="status">当前页：{page} · 提问会带上这一页，回复均为草稿</div>
              <div ref={scrollerRef} style={{ display: "flex", flexDirection: "column", gap: 10, maxHeight: "55vh", overflowY: "auto" }}>
                {messages.length === 0 && <div className="sub">问这一页的事：缺什么、依据是什么、这一段怎么改。发送前先过机密级检测，命中即拦截、不发给模型、不保存。</div>}
                {messages.map((m, i) => {
                  if (m.role === "user") return (
                    <div key={i} style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
                      <div className="msg-user">{m.content}</div>
                      {custNote[i] ? <div className="sub" role="status">{custNote[i]}</div>
                        : <button type="button" className="btn sm" onClick={() => toCustomization(i, m.content)}>转为定制项</button>}
                    </div>
                  );
                  if (m.role === "blocked") return <div key={i} className="blocked">已拦截：{m.content}（没有发给大模型，也没有保存）</div>;
                  if (m.role === "error") return <div key={i} className="err">请求失败：{m.content}</div>;
                  return (
                    <div key={i} className="msg-ai">
                      <div className="body">{m.content}{m.streaming ? "……" : ""}</div>
                      {m.interrupted && <div className="sub">输出中断，内容不完整</div>}
                    </div>
                  );
                })}
                {sending && messages[messages.length - 1]?.role === "user" && <div className="sub">Agent 正在思考……</div>}
                {error && <div className="err" role="alert">{error}</div>}
              </div>
              <form className="composer" onSubmit={(e) => { e.preventDefault(); send(); }}>
                <label style={{ flex: 1, display: "flex", minWidth: 0 }}>
                  <span style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>给咨询师 Agent 的问题</span>
                  <textarea rows={2} value={text} placeholder="问这一页的事（Enter 发送，Shift+Enter 换行）" onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
                </label>
                <button className="btnp" type="submit" disabled={sending || !text.trim()}>发送</button>
              </form>
            </>
          )}
        </Drawer>
      )}
    </>
  );
}
