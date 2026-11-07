import { useEffect, useRef, useState } from "react";
import { api, chatStream, TIER } from "../api.js";
import { TopBar } from "../components/common.jsx";

const SUGGESTIONS = ["一期人力模型怎么选？给出推荐和理由", "列出事件响应能力的主要差距，并给整改建议", "把整改建议写成四要素表格"];

function Blocked({ detail }) {
  return (
    <div className="blocked">
      <div style={{ fontWeight: 700, color: "var(--crim)" }}>已拦截：{detail.summary}</div>
      <div className="sub" style={{ marginTop: 4 }}>内容没有发给大模型。请把下列内容改写成问题描述，例如“核心区有一台交换机口令长期未改”。</div>
      <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
        {detail.findings.map((f, i) => <li key={i}><span className="mono">{f.excerpt}</span>　{f.label}</li>)}
      </ul>
    </div>
  );
}

export default function Chat({ code }) {
  const [project, setProject] = useState(null);
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const endRef = useRef(null);

  useEffect(() => { api.project(code).then(setProject).catch(() => setProject(null)); }, [code]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  async function send(msg) {
    const content = (msg ?? text).trim();
    if (!content || sending) return;
    setText("");
    setMessages((m) => [...m, { role: "user", content }]);
    setSending(true);
    let started = false;
    // 更新最后一条 AI 消息（流式输出时逐段追加）
    const patchLast = (fn) => setMessages((m) => [...m.slice(0, -1), fn(m[m.length - 1])]);
    try {
      const done = await chatStream(code, content, (delta) => {
        if (!started) {
          started = true;
          setMessages((m) => [...m, { role: "ai", content: delta, streaming: true }]);
        } else {
          patchLast((last) => ({ ...last, content: last.content + delta }));
        }
      });
      if (started) patchLast((last) => ({ ...last, streaming: false, model: done.model, demo: done.demo }));
    } catch (e) {
      if (started) patchLast((last) => ({ ...last, streaming: false, interrupted: true }));
      if (e.status === 422 && e.detail?.findings) setMessages((m) => [...m, { role: "blocked", detail: e.detail }]);
      else setMessages((m) => [...m, { role: "error", content: e.message }]);
    } finally {
      setSending(false);
    }
  }

  const tier = TIER[project?.tier] || TIER.standard;

  return (
    <>
      <TopBar cap={`${code} · ${project?.service || ""} · 阶段 ${project?.phase_name || ""}`} title="顾问对话">
        <span className={`chip ${tier.cls}`}>{tier.label}{project?.tier === "strict" ? " · 输出使用占位符" : " · 资料不出公司"}</span>
        <span className="chip c-a">输出均为草稿 · 待顾问审核</span>
      </TopBar>
      <section className="pnl" style={{ display: "flex", flexDirection: "column", minHeight: "calc(100vh - 110px)" }}>
        <div style={{ flex: 1, padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14, overflowY: "auto" }}>
          {messages.length === 0 && (
            <div className="sub">发送前平台会先检查机密级内容（口令、密钥、IP、身份证号、银行卡号、手机号、邮箱），命中即拦截，不会发给大模型。所有调用写入审计日志，日志不记录原文。</div>
          )}
          {messages.map((m, i) => {
            if (m.role === "user") return <div key={i} className="msg-user">{m.content}</div>;
            if (m.role === "blocked") return <Blocked key={i} detail={m.detail} />;
            if (m.role === "error") return <div key={i} className="err">请求失败：{m.content}</div>;
            return (
              <div key={i} className="msg-ai">
                <div className="av" aria-hidden="true">AI</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
                  <div className="sub">
                    咨询师 Agent{m.model ? ` · ${m.model}` : ""}{m.demo ? " · 演示模式" : ""}
                    {m.streaming ? " · 输出中……" : ""}{m.interrupted ? " · 输出中断，内容不完整" : ""}
                  </div>
                  <div className="body">{m.content}</div>
                </div>
              </div>
            );
          })}
          {sending && messages[messages.length - 1]?.role === "user" && <div className="sub">Agent 正在思考……</div>}
          <div ref={endRef} />
        </div>
        <form style={{ padding: "12px 20px 14px", borderTop: "1px solid var(--ln)", display: "flex", flexDirection: "column", gap: 8 }}
          onSubmit={(e) => { e.preventDefault(); send(); }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {SUGGESTIONS.map((s) => <button type="button" key={s} className="chip c-n" style={{ border: "1px solid var(--ln2)" }} onClick={() => send(s)}>{s}</button>)}
          </div>
          <div className="composer">
            <label style={{ flex: 1, display: "flex", minWidth: 0 }}>
              <span style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>给 Agent 的指令</span>
              <textarea rows={2} value={text} placeholder="继续追问，或让 Agent 把结论写进交付件……（Enter 发送，Shift+Enter 换行）"
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
            </label>
            <button className="btnp" type="submit" disabled={sending || !text.trim()} aria-label="发送">发送</button>
          </div>
        </form>
      </section>
    </>
  );
}
