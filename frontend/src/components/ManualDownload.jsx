// 四期 B25：知识审批页“需人工下载”。采集机取不到的网址（网站拒绝脚本、解析不到域名等）和资料仓 backlog 的老条目列在这里；
// 知识专家、知识管理员用浏览器从官网下载原件后上传：网址必须在正式来源里（编号类条目要填下载网址），
// 后台按采集回传同一口径核对哈希与白名单，再走自动核验发布，通过才关缺口。
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { fmtDate } from "./kb.jsx";

const ACCEPT = ".pdf,.docx,.doc,.html,.htm,.txt";

function Item({ x, onDone }) {
  const input = useRef(null);
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState(x.is_url ? x.gap_key : "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const uploading = x.plan === "uploaded";

  async function submit(e) {
    e.preventDefault();
    const file = input.current?.files?.[0];
    if (!file) { setError("请选择原件"); return; }
    setBusy(true);
    setError("");
    try {
      await api.kbManualUpload(x.id, file, title.trim(), url.trim());
      await onDone();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span className="mono" style={{ overflowWrap: "anywhere" }}>{x.gap_key}</span>
      <span className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        <span className="chip c-a">{x.reason_label || "原因未记"}</span>
        {uploading ? <span className="chip c-c">已上传，处理中</span>
          : x.is_url && !x.in_whitelist && <span className="chip c-r">网址不在正式来源里，先登记来源</span>}
        <span className="chip c-n">得分 {Number(x.score).toFixed(2)}</span>
        <span className="chip c-n">权威等级 {x.authority}</span>
        <span className="sub">登记于 {fmtDate(x.opened_at)}</span>
      </span>
      {!uploading && (
        <form className="row" style={{ flexWrap: "wrap", gap: 8 }} onSubmit={submit} aria-label={`上传原件 ${x.gap_key}`}>
          <input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="文件名称（如：个人数据保护法 2012）" required />
          <input value={url} maxLength={500} onChange={(e) => setUrl(e.target.value)} placeholder="官方下载网址" required style={{ minWidth: 280 }} />
          <input ref={input} type="file" accept={ACCEPT} aria-label="原件" />
          <button type="submit" className="btnp" disabled={busy}>{busy ? "上传中…" : "上传原件"}</button>
        </form>
      )}
      {error && <div className="err" role="alert">{error}</div>}
    </li>
  );
}

export default function ManualDownload() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setError("");
    try {
      setItems(await api.kbManualQueue());
    } catch (e) {
      setError(e.message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
      <div className="sub">
        采集机自动取不到的资料。请用浏览器从官方网站下载原件后在这里上传：平台核对哈希与来源白名单，
        再按依据原文规则自动切条核验，通过才入库并关闭缺口；核对不通过会退回本清单。
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {items && items.length === 0 && <section className="pnl pad sub">没有需人工下载的资料。</section>}
      {items && items.length > 0 && (
        <section className="pnl pad" aria-label="需人工下载">
          <div className="sb"><span className="t">需人工下载</span><span className="sub">{items.length} 项</span></div>
          <ul className="lst">{items.map((x) => <Item key={x.id} x={x} onDone={load} />)}</ul>
        </section>
      )}
    </div>
  );
}
