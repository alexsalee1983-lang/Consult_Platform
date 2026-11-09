import { useState } from "react";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";
import { BlockedNotice, useKbMeta } from "../components/kb.jsx";

export default function KbSearch() {
  const meta = useKbMeta();
  const [q, setQ] = useState("");
  const [f, setF] = useState({ product: "", stage: "", doc_type: "", layer: "" });
  const [basisOnly, setBasisOnly] = useState(false);
  const [out, setOut] = useState(null);
  const [open, setOpen] = useState(null);       // 展开查看的条目正文
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function run(e) {
    e?.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    setOpen(null);
    try {
      setOut(basisOnly ? await api.kbBasis(q.trim()) : await api.kbSearch({ q: q.trim(), ...f }));
    } catch (err) {
      setOut(null);
      setError(err.status === 422 && err.detail?.findings ? { blocked: err.detail } : { message: err.message });
    } finally {
      setBusy(false);
    }
  }

  async function show(h) {
    try {
      const item = await api.kbItem(h.doc_id);
      const v = item.current ? await api.kbVersion(item.current.id) : null;
      setOpen({ doc_id: h.doc_id, body: v?.body || "", title: h.title });
    } catch (err) {
      setError({ message: err.message });
    }
  }

  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  return (
    <>
      <TopBar cap="知识 · 只检索已发布的条目" title="知识检索" />
      <section className="pnl pad" aria-label="检索">
        <form onSubmit={run} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <div className="row">
            <input className="inp" style={{ flex: 1 }} value={q} maxLength={500} placeholder="例如：客户没有夜班，一级告警怎么处理？"
              onChange={(e) => setQ(e.target.value)} />
            <button className="btnp" type="submit" disabled={busy || !q.trim()} style={{ height: 36 }}>{busy ? "检索中……" : "检索"}</button>
          </div>
          {meta && (
            <div className="row" style={{ flexWrap: "wrap" }}>
              <label className="chk"><input type="checkbox" checked={basisOnly} onChange={(e) => setBasisOnly(e.target.checked)} />只查依据库（L4）</label>
              {!basisOnly && (
                <>
                  <select className="inp" value={f.product} onChange={set("product")}><option value="">全部服务产品</option>
                    {Object.entries(meta.products).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
                  <select className="inp" value={f.stage} onChange={set("stage")}><option value="">全部阶段</option>
                    {meta.stages.map((s) => <option key={s} value={s}>{s}</option>)}</select>
                  <select className="inp" value={f.doc_type} onChange={set("doc_type")}><option value="">全部类型</option>
                    {Object.entries(meta.doc_types).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
                  <select className="inp" value={f.layer} onChange={set("layer")}><option value="">全部层级</option>
                    {Object.entries(meta.layers).map(([k, v]) => <option key={k} value={k}>{k} {v}</option>)}</select>
                </>
              )}
            </div>
          )}
        </form>
        <div className="sub">检索词先做机密级检查；平台不记录检索词原文。</div>
      </section>
      {error?.blocked && <BlockedNotice detail={{ ...error.blocked, summary: error.blocked.summary }} />}
      {error?.message && <div className="err" role="alert">{error.message}</div>}
      {out && (
        <section className="pnl pad" aria-label="检索结果">
          <div className="sb"><h2 className="h2">结果</h2>
            <span className="sub">{out.items.length} 条 · 向量检索：{out.vector === "ok" ? "已用" : out.vector?.startsWith("unavailable") ? "暂不可用，只用全文" : "未启用"}</span></div>
          {out.note && <div className="note-amber">{out.note}</div>}
          <ul className="lst">
            {out.items.map((h) => (
              <li key={h.doc_id} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <div className="sb">
                  <span><span className="mono sub">{h.doc_id}</span> <b>{h.title}</b>{h.heading ? <span className="sub"> · {h.heading}</span> : null}</span>
                  <span className="row" style={{ gap: 4 }}>
                    <span className="chip c-n">{h.matched}</span>
                    {h.needs_update_check && <span className="chip c-a">请确认是否有更新</span>}
                  </span>
                </div>
                <div style={{ color: "var(--tx2)" }}>{h.snippet}</div>
                {h.citation && <div className="mono sub">{h.citation}</div>}
                <div><button type="button" className="btn sm" onClick={() => show(h)}>查看全文</button></div>
                {open?.doc_id === h.doc_id && <div className="preview" style={{ whiteSpace: "pre-wrap" }}>{open.body}</div>}
              </li>
            ))}
            {out.items.length === 0 && <li className="sub">没有找到相关条目</li>}
          </ul>
        </section>
      )}
    </>
  );
}
