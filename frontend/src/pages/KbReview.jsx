import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router";
import { api } from "../api.js";
import { hasRole, useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";
import { Chip, EMPTY_META, fmtDate, MetaForm, useKbMeta, VERSION_STATUS } from "../components/kb.jsx";

const TABS = [
  ["queue", "待审"], ["draft", "我的草稿"], ["rejected", "已退回"], ["published", "已发布"],
];

function Diff({ lines }) {
  const body = lines.filter((l) => !l.startsWith("---") && !l.startsWith("+++"));
  const plus = body.filter((l) => l.startsWith("+")).length;
  const minus = body.filter((l) => l.startsWith("-")).length;
  return (
    <section className="diff" aria-label="版本差异">
      <div className="sb diff-h"><span className="cap">与现行版本的差异</span><span className="sub">+{plus} −{minus}</span></div>
      <div className="mono diff-b">
        {body.map((l, i) => (
          <div key={i} className={l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : "ctx"}>
            {l.startsWith("@@") ? "· · ·" : l}
          </div>
        ))}
      </div>
    </section>
  );
}

function EditDraft({ v, onSaved }) {
  const [f, setF] = useState({ title: v.title, summary: v.summary, applicability: v.applicability,
    change_note: v.change_note, body: v.body });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));

  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="修改草稿">
      <h2 className="h2">修改草稿</h2>
      <label className="field"><span>标题</span><input value={f.title} maxLength={200} onChange={set("title")} /></label>
      <label className="field"><span>摘要</span><input value={f.summary} maxLength={1000} onChange={set("summary")} /></label>
      <label className="field"><span>适用条件</span><input value={f.applicability} maxLength={500} onChange={set("applicability")} /></label>
      <label className="field"><span>与上一版的差异说明</span><input value={f.change_note} maxLength={1000} onChange={set("change_note")} /></label>
      <label className="field"><span>正文（Markdown，用 ## 分章节）</span>
        <textarea className="inp" rows={14} value={f.body} onChange={set("body")} />
      </label>
      {error && <div className="err" role="alert">{error}</div>}
      <div className="row">
        <button type="button" className="btn" disabled={busy} onClick={() => run(() => api.kbEditVersion(v.id, f))}>保存</button>
        <button type="button" className="btnp" disabled={busy}
          onClick={() => run(async () => { await api.kbEditVersion(v.id, f); await api.kbSubmit(v.id); })}>保存并提交审核</button>
      </div>
    </section>
  );
}

function ReviewActions({ v, onDone }) {
  const { user } = useAuth();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const roles = [["content", "knowledge_expert", "内容审核"], ["compliance", "knowledge_admin", "合规审核"]]
    .filter(([, r]) => hasRole(user, r));
  const mine = [v.created_by, v.submitted_by].includes(user?.username);
  const done = new Set(v.reviews.filter((r) => r.decision === "approve").map((r) => r.review_role));

  async function act(role, decision) {
    if (decision === "reject" && !window.confirm("退回后这个版本不能再改，需要建新版本重新提交。确认退回？")) return;
    setBusy(true);
    setError("");
    try {
      await api.kbReview(v.id, role, decision, comment);
      await onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (mine) return <div className="sub">这是你创建或提交的版本，需由其他人审核。</div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label className="field"><span>审核意见</span>
        <input value={comment} maxLength={1000} onChange={(e) => setComment(e.target.value)} />
      </label>
      {roles.map(([role, , label]) => (
        <div key={role} className="row">
          <span className="sub" style={{ width: 64 }}>{label}</span>
          {done.has(role) ? <span className="chip c-g">已有人通过</span> : (
            <>
              <button type="button" className="btnp" disabled={busy} onClick={() => act(role, "approve")}>采纳</button>
              <button type="button" className="btn" disabled={busy} onClick={() => act(role, "reject")}>退回</button>
            </>
          )}
        </div>
      ))}
      {roles.length === 0 && <div className="sub">你没有审核角色。</div>}
      {error && <div className="err" role="alert">{error}</div>}
    </div>
  );
}

function PublishedActions({ v, onDone }) {
  const { user } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function act(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      await onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="row" style={{ flexWrap: "wrap" }}>
      <button type="button" className="btn" disabled={busy}
        onClick={() => act(() => api.kbRevise(v.doc_id, { content: { title: v.title, source_type: v.source_type, source_detail: v.source_detail } }))}>修订（建新版本）</button>
      {hasRole(user, "knowledge_expert") && (
        <button type="button" className="btn" disabled={busy} onClick={() => act(() => api.kbReconfirm(v.doc_id))}>复审确认仍有效</button>
      )}
      {hasRole(user, "knowledge_admin") && (
        <button type="button" className="btn" disabled={busy}
          onClick={() => { const r = window.prompt("下线原因"); if (r !== null) act(() => api.kbRetire(v.doc_id, r)); }}>下线</button>
      )}
      {error && <div className="err" role="alert">{error}</div>}
    </div>
  );
}

function NewPublicItem({ onCreated, onCancel }) {
  const meta = useKbMeta();
  const [m, setM] = useState(EMPTY_META);
  const [c, setC] = useState({ title: "", body: "", source_detail: "", ref_document: "", ref_version: "", ref_status: "current" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setC((x) => ({ ...x, [k]: e.target.value }));

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const content = { title: c.title, body: c.body, source_type: "public", source_detail: c.source_detail };
      if (m.layer === "L4") Object.assign(content, { ref_document: c.ref_document, ref_version: c.ref_version, ref_status: c.ref_status });
      onCreated(await api.kbCreateItem({ meta: m, content }));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="新建公开资料条目">
      <h2 className="h2">新建公开资料条目</h2>
      <div className="sub">只适用于公开标准、法规、行业资料的整理。项目沉淀、专家萃取、内部制度改造请先在投料箱投料、通过去标识。</div>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <label className="field"><span>标题 *</span><input value={c.title} maxLength={200} onChange={set("title")} required /></label>
        <MetaForm value={m} onChange={setM} />
        <label className="field"><span>来源说明（标准编号、文件名）</span><input value={c.source_detail} maxLength={300} onChange={set("source_detail")} /></label>
        {m.layer === "L4" && meta && (
          <div className="form-grid">
            <label className="field"><span>依据文件名 *</span><input value={c.ref_document} onChange={set("ref_document")} placeholder="GB/T 22239" /></label>
            <label className="field"><span>版本号 *</span><input value={c.ref_version} onChange={set("ref_version")} placeholder="2019" /></label>
            <label className="field"><span>生效状态 *</span>
              <select value={c.ref_status} onChange={set("ref_status")}>
                {Object.entries(meta.ref_statuses).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
            </label>
          </div>
        )}
        <label className="field"><span>正文 *（Markdown，用 ## 分章节；依据类用条款号作章节标题，如“## 8.1.5 集中管控”）</span>
          <textarea className="inp" rows={12} value={c.body} onChange={set("body")} required />
        </label>
        {error && <div className="err" role="alert">{error}</div>}
        <div className="row">
          <button className="btnp" type="submit" disabled={busy || !c.title.trim() || !c.body.trim()}>{busy ? "创建中……" : "建草稿"}</button>
          <button className="btn" type="button" onClick={onCancel}>取消</button>
        </div>
      </form>
    </section>
  );
}

export default function KbReview() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState(params.get("version") ? "draft" : "queue");
  const [list, setList] = useState([]);
  const [selected, setSelected] = useState(params.get("version") ? Number(params.get("version")) : null);
  const [v, setV] = useState(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");

  const loadList = useCallback(async () => {
    setError("");
    try {
      if (tab === "queue") setList(await api.kbQueue());
      else if (tab === "published") setList(await api.kbVersions({ status: "published" }));
      else setList(await api.kbVersions({ status: tab, mine: true }));
    } catch (e) {
      setError(e.message);
    }
  }, [tab]);
  const loadV = useCallback(async (id) => {
    if (!id) { setV(null); return; }
    try {
      setV(await api.kbVersion(id));
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { loadV(selected); }, [selected, loadV]);

  const select = (id) => { setSelected(id); setCreating(false); setParams(id ? { version: String(id) } : {}); };
  const refresh = async () => { await loadList(); await loadV(selected); };

  return (
    <>
      <TopBar cap="知识 · 双人审核（知识专家审内容，知识管理员审合规）" title="审批队列">
        <button type="button" className="btnp" onClick={() => { setCreating(true); setSelected(null); }}>＋ 新建公开资料条目</button>
      </TopBar>
      <div className="kb3">
        <section className="pnl pad" aria-label="版本列表">
          <div className="tabs" role="tablist">
            {TABS.map(([k, l]) => (
              <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? "on" : undefined}
                onClick={() => setTab(k)}>{l}</button>
            ))}
          </div>
          {error && <div className="err">{error}</div>}
          <ul className="pick">
            {list.map((x) => (
              <li key={x.id}>
                <button type="button" className={selected === x.id ? "on" : undefined} onClick={() => select(x.id)}>
                  <span className="sb"><span className="t">{x.title}</span><Chip map={VERSION_STATUS} value={x.status} /></span>
                  <span className="sub mono">{x.doc_id} · v{x.version}</span>
                  <span className="sub">{x.submitted_by || x.created_by} · {fmtDate(x.created_at)}</span>
                </button>
              </li>
            ))}
            {list.length === 0 && <li className="sub">没有{TABS.find(([k]) => k === tab)[1]}的版本</li>}
          </ul>
        </section>

        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          {creating && <NewPublicItem onCancel={() => setCreating(false)} onCreated={(nv) => { setTab("draft"); select(nv.id); loadList(); }} />}
          {!creating && !v && <section className="pnl pad sub">选择左边的一个版本查看详情。</section>}
          {!creating && v && (
            <section className="pnl" aria-label="版本详情">
              <div className="vhead">
                <div className="row"><span className="mono sub">{v.doc_id} · v{v.version}</span><Chip map={VERSION_STATUS} value={v.status} /></div>
                <h2 style={{ margin: 0, fontSize: 19, fontWeight: 700 }}>{v.title}</h2>
                <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                  <span className="chip c-c">来源：{{ public: "公开资料", expert: "专家萃取", project: "项目沉淀", internal: "内部制度改造" }[v.source_type] || v.source_type}</span>
                  <span className="chip c-n">去标识：{{ passed: "已通过", not_needed: "无需", pending: "未完成" }[v.deid_status] || v.deid_status}</span>
                  {v.applicability && <span className="chip c-n">适用：{v.applicability}</span>}
                  {v.ref_document && <span className="chip c-v">依据：{v.ref_document} {v.ref_version}</span>}
                  {v.review_date && <span className="chip c-n">复审 {v.review_date}</span>}
                </div>
              </div>
              <div className="pad" style={{ gap: 14 }}>
                {v.summary && <p style={{ margin: 0, color: "var(--tx2)", lineHeight: 1.75 }}>{v.summary}</p>}
                {v.change_note && <div className="sub">修订说明：{v.change_note}</div>}
                {v.diff.length > 0 && <Diff lines={v.diff} />}
                <details open={v.diff.length === 0}>
                  <summary className="cap" style={{ cursor: "pointer" }}>正文</summary>
                  <div className="preview" style={{ whiteSpace: "pre-wrap" }}>{v.body}</div>
                </details>
              </div>
            </section>
          )}
          {!creating && v?.status === "draft" && <EditDraft key={v.id} v={v} onSaved={refresh} />}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
          {v && !creating && (
            <>
              <section className="pnl pad" aria-label="审核">
                <h2 className="h2">审核</h2>
                <ul className="lst">
                  {v.reviews.map((r, i) => (
                    <li key={i} className="sb">
                      <span>{r.reviewer}<span className="sub">（{r.review_role === "content" ? "内容" : "合规"}）</span>
                        {r.comment && <div className="sub">{r.comment}</div>}</span>
                      <span className={`chip ${r.decision === "approve" ? "c-g" : "c-r"}`}>{r.decision === "approve" ? "采纳" : "退回"}</span>
                    </li>
                  ))}
                  {v.reviews.length === 0 && <li className="sub">还没有审核记录</li>}
                </ul>
                <div className="sub">双人审核 · 第 {new Set(v.reviews.filter((r) => r.decision === "approve").map((r) => r.reviewer)).size} / 2 人</div>
                {v.status === "in_review" && <ReviewActions v={v} onDone={refresh} />}
                {v.status === "published" && <PublishedActions v={v} onDone={refresh} />}
              </section>
              <section className="pnl pad" aria-label="相似条目">
                <div className="cap">相似的已发布条目（可能重复或冲突）</div>
                <ul className="lst">
                  {v.similar.map((s) => <li key={s.doc_id}><span className="mono sub">{s.doc_id}</span> {s.title}</li>)}
                  {v.similar.length === 0 && <li className="sub">没有发现</li>}
                </ul>
              </section>
              <section className="pnl pad" aria-label="发布影响">
                <div className="cap">发布影响</div>
                <div className="sub">引用此条目的交付物：交付链（S3）接入后显示。</div>
              </section>
            </>
          )}
          {!v && !creating && hasRole(user, "knowledge_admin") && <section className="pnl pad sub">知识管理员审合规；知识专家审内容。两人都采纳后自动发布。</section>}
        </div>
      </div>
    </>
  );
}
