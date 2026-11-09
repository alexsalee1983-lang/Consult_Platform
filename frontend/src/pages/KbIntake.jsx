import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api.js";
import { hasRole, isKnowledge, useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";
import {
  BlockedNotice, Chip, DEID_CATEGORIES, EMPTY_META, fmtDate, fmtSize, MetaForm, UPLOAD_STATUS, useKbMeta,
} from "../components/kb.jsx";

const ACCEPT = ".docx,.pdf,.md,.markdown,.txt";
const BUSY = new Set(["received", "parsed"]);   // 后台还在处理，定时刷新

function Uploader({ onDone }) {
  const input = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function upload(file) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      onDone(await api.kbUpload(file));
    } catch (e) {
      setError(e.status === 422 && e.detail?.findings ? { blocked: e.detail } : { message: e.message });
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <section className="pnl pad" aria-label="上传资料">
      <h2 className="h2">投料</h2>
      <label className={`drop${busy ? " busy" : ""}`}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); upload(e.dataTransfer.files?.[0]); }}>
        <input ref={input} type="file" accept={ACCEPT} disabled={busy} onChange={(e) => upload(e.target.files?.[0])} />
        <span style={{ fontWeight: 700 }}>{busy ? "正在上传和检查……" : "拖入文件，或点击选择"}</span>
        <span className="sub">Word（.docx）、文字版 PDF、Markdown、纯文本，单个 20MB 以内。扫描件请提供文字版。</span>
      </label>
      <div className="sub">上传时先做机密级检查，命中的文件不保存。通过后自动解析、去标识，再由知识管理员确认。</div>
      {error?.blocked && <BlockedNotice detail={error.blocked} />}
      {error?.message && <div className="err" role="alert">{error.message}</div>}
    </section>
  );
}

function TermsEditor({ onRun, busy }) {
  const [rows, setRows] = useState([{ term: "", category: "客户名称" }]);
  const set = (i, k) => (e) => setRows((r) => r.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)));
  const ready = rows.some((r) => r.term.trim());
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div className="sub">规则漏掉的客户简称、系统名、人名，在这里补充后重跑。补充的词只用于这一次替换，不保存、不进审计。</div>
      {rows.map((r, i) => (
        <div key={i} className="row">
          <input className="inp" style={{ flex: 1 }} value={r.term} maxLength={100} placeholder="要替换的词" onChange={set(i, "term")} />
          <select className="inp" value={r.category} onChange={set(i, "category")}>
            {DEID_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      ))}
      <div className="row">
        <button type="button" className="btn sm" onClick={() => setRows((r) => [...r, { term: "", category: "客户名称" }])}>＋ 再加一个</button>
        <button type="button" className="btn sm" disabled={!ready || busy}
          onClick={() => onRun(rows.filter((r) => r.term.trim()).map((r) => ({ term: r.term.trim(), category: r.category })))}>
          {busy ? "重跑中……" : "补充后重跑去标识"}
        </button>
      </div>
    </div>
  );
}

function DeidReport({ upload, report, reload }) {
  const { user } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [comment, setComment] = useState("");
  const pending = report.status === "pending" && upload.status === "deid_pending";
  const canDecide = pending && hasRole(user, "knowledge_admin") && upload.uploaded_by !== user?.username;

  async function act(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const agentCls = { ok: "c-g", skipped: "c-a", failed: "c-r" }[report.agent_status] || "c-n";
  const agentLabel = { ok: "已复查", skipped: "未复查", failed: "复查失败" }[report.agent_status] || "—";
  return (
    <section className="pnl pad" aria-label="去标识报告">
      <div className="sb">
        <h2 className="h2">去标识报告</h2>
        <span className="row" style={{ gap: 4 }}>
          <span className={`chip ${agentCls}`}>脱敏审查 Agent：{agentLabel}</span>
          {report.conclusion && <span className={`chip ${report.conclusion === "退回" ? "c-r" : report.conclusion === "通过" ? "c-g" : "c-a"}`}>结论：{report.conclusion}</span>}
          {report.risk_level && <span className="chip c-n">残余风险：{report.risk_level}</span>}
        </span>
      </div>
      <div className="sub">共替换 {report.total} 处 · 去标识后文件名：<span className="mono">{report.safe_filename}</span>{report.agent_note ? ` · ${report.agent_note}` : ""}</div>
      <table className="tbl">
        <thead><tr><th>类别</th><th>替换为</th><th>次数</th><th>章节</th></tr></thead>
        <tbody>
          {report.replacements.map((r, i) => (
            <tr key={i}><td>{r.category}</td><td className="mono">{r.placeholder}</td><td>{r.count}</td>
              <td className="sub">{r.sections.map((s) => (s < 0 ? "文件名" : `第 ${s + 1} 节`)).join("、")}</td></tr>
          ))}
          {report.replacements.length === 0 && <tr><td colSpan={4} className="sub">规则没有发现需要替换的内容</td></tr>}
        </tbody>
      </table>
      {report.combination_risks.length > 0 && (
        <div>
          <div className="cap">组合推断风险</div>
          <ul className="lst">{report.combination_risks.map((r, i) => <li key={i}>{r.description}<div className="sub">处理：{r.handling}</div></li>)}</ul>
        </div>
      )}
      {report.manual_items.length > 0 && (
        <div className="note-amber">
          <b>需人工确认</b>
          <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{report.manual_items.map((m, i) => <li key={i}>{m}</li>)}</ul>
        </div>
      )}
      <div>
        <div className="cap">去标识后预览（前 {report.preview.length} 段）</div>
        <div className="preview">
          {report.preview.map((c, i) => (
            <div key={i}>{c.heading && <div className="sub mono">{c.heading}</div>}<div>{c.text}</div></div>
          ))}
        </div>
      </div>
      {pending && isKnowledge(user) && <TermsEditor busy={busy} onRun={(terms) => act(() => api.kbDeidRerun(upload.id, terms))} />}
      {canDecide && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label className="field"><span>确认意见</span>
            <input value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} placeholder="例如：已逐段核对，无残留客户信息" />
          </label>
          <div className="row">
            <button type="button" className="btnp" disabled={busy || report.conclusion === "退回"}
              onClick={() => window.confirm("确认去标识通过？通过后原件和未去标识的解析结果会被删除，不能恢复。")
                && act(() => api.kbDeidDecide(upload.id, "pass", comment))}>去标识通过</button>
            <button type="button" className="btn" disabled={busy}
              onClick={() => window.confirm("退回这份资料？原件和全部解析结果都会被删除。")
                && act(() => api.kbDeidDecide(upload.id, "reject", comment))}>退回</button>
          </div>
          {report.conclusion === "退回" && <div className="sub">Agent 结论为退回：请补充替换词重跑，或直接退回。</div>}
        </div>
      )}
      {pending && hasRole(user, "knowledge_admin") && upload.uploaded_by === user?.username && (
        <div className="sub">这是你上传的资料，需由另一位知识管理员确认。</div>
      )}
      {pending && !hasRole(user, "knowledge_admin") && <div className="sub">等待知识管理员确认去标识结果。</div>}
      {report.status !== "pending" && (
        <div className="sub">{report.status === "passed" ? "已通过" : "已退回"} · {report.reviewed_by} · {fmtDate(report.reviewed_at)}{report.comment ? ` · ${report.comment}` : ""}</div>
      )}
      {error && <div className="err" role="alert">{error}</div>}
    </section>
  );
}

function CreateItem({ upload }) {
  const navigate = useNavigate();
  const meta = useKbMeta();
  const [m, setM] = useState(EMPTY_META);
  const [c, setC] = useState({ title: "", summary: "", applicability: "", source_type: "project", source_detail: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setC((x) => ({ ...x, [k]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const v = await api.kbCreateItem({ meta: m, content: { ...c, body: "" }, upload_id: upload.id });
      navigate(`/kb/review?version=${v.id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="建知识条目">
      <h2 className="h2">建知识条目</h2>
      <div className="sub">正文取去标识后的内容，建好后是草稿，在“审批队列”里修改、提交，经两人审核后发布。</div>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <label className="field"><span>标题 *（简明，不含客户信息）</span><input value={c.title} maxLength={200} onChange={set("title")} required /></label>
        <MetaForm value={m} onChange={setM} />
        <div className="form-grid">
          <label className="field"><span>来源 *</span>
            <select value={c.source_type} onChange={set("source_type")}>
              {meta && Object.entries(meta.source_types).filter(([k]) => k !== "public").map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <label className="field"><span>{c.source_type === "internal" ? "授权编号 *" : "来源说明"}</span>
            <input value={c.source_detail} maxLength={300} onChange={set("source_detail")} />
          </label>
        </div>
        <label className="field"><span>摘要</span><input value={c.summary} maxLength={1000} onChange={set("summary")} /></label>
        <label className="field"><span>适用条件</span><input value={c.applicability} maxLength={500} onChange={set("applicability")} placeholder="例如：有 7×24 值守的客户" /></label>
        {error && <div className="err" role="alert">{error}</div>}
        <div className="row"><button className="btnp" type="submit" disabled={busy || !c.title.trim()}>{busy ? "创建中……" : "建草稿"}</button></div>
      </form>
    </section>
  );
}

export default function KbIntake() {
  const { user } = useAuth();
  const [list, setList] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");

  const loadList = useCallback(() => api.kbUploads().then(setList).catch((e) => setError(e.message)), []);
  const loadDetail = useCallback(async (id) => {
    if (!id) return;
    const d = await api.kbUpload1(id);
    setDetail(d);
    setReport(d.status.startsWith("deid_") ? await api.kbDeid(id).catch(() => null) : null);
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { loadDetail(selected).catch((e) => setError(e.message)); }, [selected, loadDetail]);
  // 后台还在解析、去标识时，每 3 秒刷新一次
  useEffect(() => {
    if (!detail || !BUSY.has(detail.status)) return undefined;
    const t = setInterval(() => { loadDetail(detail.id); loadList(); }, 3000);
    return () => clearInterval(t);
  }, [detail, loadDetail, loadList]);

  const reload = async () => { await loadDetail(selected); await loadList(); };

  return (
    <>
      <TopBar cap={isKnowledge(user) ? "知识 · 全部投料" : "知识 · 我的投料"} title="投料箱" />
      <div className="kb2">
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          <Uploader onDone={(up) => { loadList(); setSelected(up.id); }} />
          <section className="pnl pad" aria-label="投料列表">
            <div className="sb"><h2 className="h2">投料记录</h2><span className="mono sub">{list.length}</span></div>
            {error && <div className="err">{error}</div>}
            <ul className="pick">
              {list.map((u) => (
                <li key={u.id}>
                  <button type="button" className={selected === u.id ? "on" : undefined} onClick={() => setSelected(u.id)}>
                    <span className="sb"><span className="t">{u.filename}</span><Chip map={UPLOAD_STATUS} value={u.status} /></span>
                    <span className="sub">#{u.id} · {u.uploaded_by} · {fmtDate(u.created_at)} · {fmtSize(u.size)}</span>
                  </button>
                </li>
              ))}
              {list.length === 0 && <li className="sub">还没有投料</li>}
            </ul>
          </section>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          {!detail && <section className="pnl pad sub">选择左边的一条投料，查看解析和去标识结果。</section>}
          {detail && (
            <section className="pnl pad" aria-label="投料详情">
              <div className="sb"><h2 className="h2">{detail.filename}</h2><Chip map={UPLOAD_STATUS} value={detail.status} /></div>
              <div className="sub">
                {detail.kind} · {fmtSize(detail.size)} · {detail.section_count} 节 · {detail.chunk_count} 段 · {detail.char_count} 字
                {detail.page_count ? ` · ${detail.page_count} 页` : ""} · 上传人 {detail.uploaded_by}
              </div>
              {BUSY.has(detail.status) && <div className="sub">后台正在解析和去标识，页面会自动刷新……</div>}
              {detail.status === "failed" && <div className="err">{detail.error}</div>}
              {detail.status === "deid_rejected" && <div className="sub">已退回，原件和解析结果已删除。</div>}
            </section>
          )}
          {detail && report && <DeidReport upload={detail} report={report} reload={reload} />}
          {detail?.status === "deid_passed" && isKnowledge(user) && <CreateItem upload={detail} />}
        </div>
      </div>
    </>
  );
}
