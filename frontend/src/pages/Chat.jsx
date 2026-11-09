import { useCallback, useEffect, useRef, useState } from "react";
import { api, sendMessage, TIER } from "../api.js";
import { TopBar } from "../components/common.jsx";

const SUGGESTIONS = ["一期人力模型怎么选？给出推荐和理由", "列出事件响应能力的主要差距，并给整改建议", "把整改建议写成四要素表格"];
const DEFAULT_AGENT = "consultant-lead"; // 咨询师 Agent 是主入口
const LEVEL = { reasoning: "推理", standard: "标准", light: "轻量" };

function Blocked({ detail }) {
  return (
    <div className="blocked">
      <div style={{ fontWeight: 700, color: "var(--crim)" }}>已拦截：{detail.summary}</div>
      <div className="sub" style={{ marginTop: 4 }}>内容没有发给大模型，也没有保存。请把下列内容改写成问题描述，例如“核心区有一台交换机口令长期未改”。</div>
      <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
        {detail.findings.map((f, i) => <li key={i}><span className="mono">{f.excerpt}</span>　{f.label}</li>)}
      </ul>
    </div>
  );
}

// 本条回复前平台检索到、附给 Agent 的知识库条目
function References({ refs }) {
  if (!refs || refs.length === 0) return null;
  return (
    <div className="refs" aria-label="参考知识">
      <span className="cap">参考知识</span>
      {refs.map((r) => (
        <span key={r.doc_id} className="ref" title={r.citation || r.heading || r.title}>
          <span className="mono">{r.doc_id}</span> {r.title}{r.heading ? ` · ${r.heading}` : ""}
        </span>
      ))}
    </div>
  );
}

function fromServer(m, agentName) {
  if (m.role === "user") return { role: "user", content: m.content };
  return { role: "ai", content: m.content, model: m.model, agentName, interrupted: m.status !== "ok", references: m.references || [] };
}

function shortTime(at) {
  return new Date(at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

export default function Chat({ code }) {
  const [project, setProject] = useState(null);
  const [agents, setAgents] = useState([]);
  const [agentCode, setAgentCode] = useState(DEFAULT_AGENT);
  const [convs, setConvs] = useState([]);
  const [conv, setConv] = useState(null);       // 当前对话（null 表示新对话，发第一条消息时创建）
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [external, setExternal] = useState(false);   // 当前接的是外部测试模型
  // 只滚动对话区内部，不滚动整页；用户往上翻看时不把他拽回底部
  const scrollerRef = useRef(null);
  const stickRef = useRef(true);

  const loadConvs = useCallback(() => api.conversations(code).then(setConvs).catch(() => setConvs([])), [code]);

  useEffect(() => {
    api.project(code).then(setProject).catch(() => setProject(null));
    api.agents().then(setAgents).catch(() => setAgents([]));
    api.health().then((h) => setExternal(Boolean(h.llm_external))).catch(() => setExternal(false));
    setConv(null);
    setMessages([]);
    loadConvs();
  }, [code, loadConvs]);
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !stickRef.current) return undefined;
    // 流式输出时每段字都会触发；合并到下一帧，直接定位到底部（不用平滑滚动，避免连续动画互相打断而抖动）
    const id = requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    return () => cancelAnimationFrame(id);
  }, [messages]);

  function onScroll(e) {
    const el = e.currentTarget;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }

  const agent = agents.find((a) => a.code === (conv?.agent_code ?? agentCode));
  const agentName = conv?.agent_name ?? agent?.name ?? "";

  async function open(c) {
    if (sending) return;
    setError("");
    try {
      const d = await api.conversation(c.id);
      setConv(d);
      setMessages(d.messages.map((m) => fromServer(m, d.agent_name)));
    } catch (e) {
      setError(e.message);
    }
  }

  function startNew(code_) {
    if (sending) return;
    setAgentCode(code_ ?? agentCode);
    setConv(null);
    setMessages([]);
    setError("");
  }

  async function send(msg) {
    const content = (msg ?? text).trim();
    if (!content || sending) return;
    setText("");
    stickRef.current = true;          // 发新消息时回到底部跟随输出
    setError("");
    setSending(true);
    let current = conv;
    let started = false;
    const patchLast = (fn) => setMessages((m) => [...m.slice(0, -1), fn(m[m.length - 1])]);
    try {
      if (!current) {
        current = await api.createConversation(code, agentCode);
        setConv(current);
      }
      setMessages((m) => [...m, { role: "user", content }]);
      const done = await sendMessage(current.id, content, (delta) => {
        if (!started) {
          started = true;
          setMessages((m) => [...m, { role: "ai", content: delta, streaming: true, agentName: current.agent_name }]);
        } else {
          patchLast((last) => ({ ...last, content: last.content + delta }));
        }
      });
      if (started) patchLast((last) => ({ ...last, streaming: false, model: done.model, demo: done.demo, references: done.references || [] }));
      // 后端用第一条提问作标题，这里同步显示
      setConv((c) => (c && !c.title ? { ...c, title: content.split("\n")[0].slice(0, 40) } : c));
    } catch (e) {
      if (started) patchLast((last) => ({ ...last, streaming: false, interrupted: true }));
      if (e.status === 422 && e.detail?.findings) {
        // 被拦截的提问不保存：从界面上撤下，换成拦截说明
        setMessages((m) => [...m.slice(0, -1), { role: "blocked", detail: e.detail }]);
      } else {
        setMessages((m) => [...m, { role: "error", content: e.message }]);
      }
    } finally {
      setSending(false);
      loadConvs();
    }
  }

  const tier = TIER[project?.tier] || TIER.standard;

  return (
    <>
      <TopBar cap={`${code} · ${project?.service || ""} · 阶段 ${project?.phase_name || ""}`} title="顾问对话">
        <span className={`chip ${tier.cls}`}>{tier.label}{project?.tier === "strict" ? " · 输出使用占位符" : external ? " · 外部测试模型" : " · 资料不出公司"}</span>
        <span className="chip c-a">输出均为草稿 · 待顾问审核</span>
      </TopBar>
      <div className="chat-layout">
        <aside className="pnl pad chat-side" aria-label="Agent 与对话">
          <label className="field">
            <span>新对话使用的 Agent</span>
            <select value={agentCode} onChange={(e) => startNew(e.target.value)} disabled={sending}>
              {agents.map((a) => (
                <option key={a.code} value={a.code} disabled={!a.ready}>
                  {a.name}{a.ready ? `（${LEVEL[a.level] || a.level}）` : `（不可用：${a.reason}）`}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="btn" onClick={() => startNew()} disabled={sending}>＋ 新对话</button>
          <div className="cap" style={{ marginTop: 4 }}>我在本项目的对话</div>
          <ul className="conv-list">
            {convs.map((c) => (
              <li key={c.id}>
                <button type="button" className={conv?.id === c.id ? "on" : undefined} onClick={() => open(c)} disabled={sending}>
                  <span className="t">{c.title || "（未命名）"}</span>
                  <span className="sub">{c.agent_name} · {shortTime(c.updated_at)}</span>
                </button>
              </li>
            ))}
            {convs.length === 0 && <li className="sub">还没有对话</li>}
          </ul>
        </aside>

        <section className="pnl chat-main" style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <div className="sb" style={{ padding: "10px 20px", borderBottom: "1px solid var(--ln)" }}>
            <span style={{ fontWeight: 700 }}>{conv ? conv.title || "（未命名）" : "新对话"}</span>
            <span className="sub">{agentName} Agent{agent?.prompt_version ? ` · 提示词 ${conv?.prompt_version ?? agent.prompt_version}` : ""}</span>
          </div>
          {external && (
            <div className="blocked" role="note" style={{ margin: "12px 20px 0" }}>
              <div style={{ fontWeight: 700, color: "var(--crim)" }}>当前接的是公司外部的测试模型，勿输入任何真实客户信息</div>
              <div className="sub" style={{ marginTop: 2 }}>
                只用虚拟客户数据测试；提问会经平台假名化后发出；严格档项目不能使用（决策记录 2026-11-08）。
              </div>
            </div>
          )}
          <div ref={scrollerRef} onScroll={onScroll}
            style={{ flex: 1, minHeight: 0, padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14, overflowY: "auto", overscrollBehavior: "contain" }}>
            {messages.length === 0 && (
              <div className="sub">
                发送前平台会先检查机密级内容（口令、密钥、IP、身份证号、银行卡号、手机号、邮箱），命中即拦截，不会发给大模型，也不保存。
                Agent 会读取本项目的项目卡，并在每次回答前检索知识库（只用已发布的知识，引用的条目显示在回复下方）；对话保存在项目区，只有你能看到。所有调用写入审计日志，日志不记录原文。
              </div>
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
                      {m.agentName || agentName} Agent{m.model ? ` · ${m.model}` : ""}{m.demo ? " · 演示模式" : ""}
                      {m.streaming ? " · 输出中……" : ""}{m.interrupted ? " · 输出中断，内容不完整" : ""}
                    </div>
                    <div className="body">{m.content}</div>
                    <References refs={m.references} />
                  </div>
                </div>
              );
            })}
            {sending && messages[messages.length - 1]?.role === "user" && <div className="sub">Agent 正在思考……</div>}
            {error && <div className="err" role="alert">{error}</div>}
          </div>
          <form style={{ padding: "12px 20px 14px", borderTop: "1px solid var(--ln)", display: "flex", flexDirection: "column", gap: 8 }}
            onSubmit={(e) => { e.preventDefault(); send(); }}>
            {messages.length === 0 && (
              <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                {SUGGESTIONS.map((s) => <button type="button" key={s} className="chip c-n" style={{ border: "1px solid var(--ln2)" }} onClick={() => send(s)}>{s}</button>)}
              </div>
            )}
            <div className="composer">
              <label style={{ flex: 1, display: "flex", minWidth: 0 }}>
                <span style={{ position: "absolute", width: 1, height: 1, overflow: "hidden", clip: "rect(0 0 0 0)" }}>给 Agent 的指令</span>
                <textarea rows={2} value={text} placeholder="继续追问，或让 Agent 把结论写进交付件……（Enter 发送，Shift+Enter 换行）"
                  onChange={(e) => setText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }} />
              </label>
              <button className="btnp" type="submit" disabled={sending || !text.trim() || (!conv && !agent?.ready)} aria-label="发送">发送</button>
            </div>
          </form>
        </section>
      </div>
    </>
  );
}
