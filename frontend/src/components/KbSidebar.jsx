// 四期 A32③：编辑器知识侧栏。按当前章节推荐相关条款（L4 依据）、公司做法和黄金样例（复用知识检索，规则见 app/project/kb_suggest.py），
// “插入”把摘录连同出处（【依据：…｜编号】或 [编号]）放到光标处；保存仍走交付件写入，平台核对来源。侧栏只读，不写知识库。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

const GROUPS = [["clauses", "相关条款"], ["practices", "公司做法"], ["golden", "黄金样例"]];

export default function KbSidebar({ code, dcode, seq, onInsert }) {
  const [q, setQ] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (extra) => {
    setBusy(true);
    setError("");
    try {
      setData(await api.kbSuggest(code, dcode, seq, extra));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }, [code, dcode, seq]);
  useEffect(() => { setData(null); load(""); }, [load]);

  const empty = data && GROUPS.every(([k]) => !data[k]?.length);
  return (
    <section className="pnl pad kb-side" aria-label="知识侧栏">
      <div className="sb"><h3 className="h2" style={{ fontSize: 15 }}>知识侧栏</h3><span className="sub">按本章推荐</span></div>
      <div className="row">
        <input className="inp" maxLength={100} value={q} placeholder="补充检索词（可空）" onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(q.trim()); }} />
        <button className="btn sm" disabled={busy} onClick={() => load(q.trim())}>{busy ? "检索中…" : "检索"}</button>
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {empty && <div className="sub">没有找到相关知识</div>}
      {data && GROUPS.map(([k, label]) => data[k]?.length > 0 && (
        <div key={k} className="kb-side" aria-label={label}>
          <div className="cap">{label}</div>
          {data[k].map((x) => (
            <div key={x.doc_id} className="kb-hit">
              <div className="sb"><span className="mono" style={{ color: "var(--cyan)" }}>{x.doc_id}</span>
                <button className="btn sm" onClick={() => onInsert(x.insert)}>插入</button></div>
              <div style={{ fontWeight: 600 }}>{x.title}{x.heading ? ` / ${x.heading}` : ""}</div>
              {x.authority_label && <div className="sub">{x.authority_label}</div>}
              <div className="sub">{x.excerpt}</div>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}
