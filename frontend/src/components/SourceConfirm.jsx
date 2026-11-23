// 三期 B25②：知识审批队列“来源确认”类。本周计划落在 confirm 的知识缺口按网址主机分组（编号类一组）；
// 知识管理员按组勾选后“确认”（可顺带登记覆盖这批网址的候选来源，许可、权威类别必填）或“不做”；知识专家只读。
// 确认不绕过白名单：登记的来源仍是候选，转正式进白名单后每周任务才排进采集下发包。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { hasRole, useAuth } from "../auth.jsx";
import { fmtDate } from "./kb.jsx";

const KIND_LABELS = { schedule: "定时巡检", external: "外部变化", usage: "使用信号", business: "业务需求", lifecycle: "生命周期" };
// 与后端 app/kb/source_registry.py 的 AUTHORITY_LEVELS 一致
const AUTHORITY_KINDS = ["法律", "行政法规", "监管通报", "国家标准", "行业标准", "白皮书", "厂商资料"];
const NUMBER_GROUP = "编号";
const EMPTY_SOURCE = { name: "", url: "", license: "", authority_kind: "", publisher: "", note: "" };

function commonPrefix(urls) {
  if (!urls.length) return "";
  let p = urls[0];
  for (const u of urls.slice(1)) {
    let i = 0;
    while (i < p.length && i < u.length && p[i] === u[i]) i += 1;
    p = p.slice(0, i);
  }
  return p.slice(0, p.lastIndexOf("/") + 1) || p;
}

function SourceForm({ value, onChange }) {
  const set = (k) => (e) => onChange({ ...value, [k]: e.target.value });
  return (
    <div className="form-grid" aria-label="来源登记">
      <label className="field"><span>来源名称 *</span><input value={value.name} maxLength={200} onChange={set("name")} required /></label>
      <label className="field"><span>来源地址 *（须覆盖所选网址）</span><input value={value.url} maxLength={500} onChange={set("url")} required /></label>
      <label className="field"><span>许可 *</span><input value={value.license} maxLength={300} onChange={set("license")} placeholder="如：政府公开信息，可转载注明出处" required /></label>
      <label className="field"><span>权威类别 *</span>
        <select value={value.authority_kind} onChange={set("authority_kind")} required>
          <option value="">请选择</option>
          {AUTHORITY_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </label>
      <label className="field"><span>发布机构</span><input value={value.publisher} maxLength={200} onChange={set("publisher")} /></label>
      <label className="field"><span>备注</span><input value={value.note} maxLength={2000} onChange={set("note")} /></label>
    </div>
  );
}

function Group({ g, canDecide, onDone }) {
  const [picked, setPicked] = useState(() => new Set());
  const [withSource, setWithSource] = useState(false);
  const [src, setSrc] = useState(EMPTY_SOURCE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const isUrl = g.group !== NUMBER_GROUP;
  const all = g.items.length > 0 && picked.size === g.items.length;
  const ids = g.items.filter((x) => picked.has(x.id)).map((x) => x.id);
  const sourceOk = !withSource || (src.name.trim() && src.url.trim() && src.license.trim() && src.authority_kind);

  const toggle = (id) => setPicked((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const toggleAll = () => setPicked(all ? new Set() : new Set(g.items.map((x) => x.id)));
  function openSource(on) {
    setWithSource(on);
    if (on && !src.url) {
      const urls = g.items.filter((x) => picked.has(x.id)).map((x) => x.gap_key);
      setSrc((s) => ({ ...s, url: commonPrefix(urls.length ? urls : g.items.map((x) => x.gap_key)) }));
    }
  }

  async function decide(decision) {
    if (decision === "reject" && !window.confirm(`所选 ${ids.length} 个缺口将关闭为“不做”。确认？`)) return;
    setBusy(true);
    setError("");
    try {
      const source = decision === "approve" && withSource ? src : null;
      await api.kbConfirmDecide(ids, decision, source);
      setPicked(new Set());
      setWithSource(false);
      setSrc(EMPTY_SOURCE);
      await onDone();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label={`来源确认 ${g.group}`} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="sb">
        <span className="t mono">{isUrl ? g.group : "编号类（标准号、文号）"}</span>
        <span className="sub">{g.items.length} 个缺口</span>
      </div>
      {canDecide && (
        <label className="chk"><input type="checkbox" checked={all} onChange={toggleAll} aria-label={`全选 ${g.group}`} />全选本组</label>
      )}
      <ul className="lst">
        {g.items.map((x) => (
          <li key={x.id} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            {canDecide && <input type="checkbox" checked={picked.has(x.id)} onChange={() => toggle(x.id)} aria-label={`选择 ${x.gap_key}`} />}
            <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
              <span className="mono" style={{ overflowWrap: "anywhere" }}>{x.gap_key}</span>
              <span className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                {Object.entries(x.kinds || {}).map(([k, n]) => <span key={k} className="chip c-c">{KIND_LABELS[k] || k} ×{n}</span>)}
                <span className="chip c-n">得分 {Number(x.score).toFixed(2)}</span>
                <span className="chip c-n">影响 {x.impact}</span>
                <span className="chip c-n">权威等级 {x.authority}</span>
                {x.due && <span className="chip c-a">期限 {x.due}</span>}
              </span>
              <span className="sub">发现于 {fmtDate(x.opened_at)}</span>
            </div>
          </li>
        ))}
      </ul>
      {canDecide && isUrl && (
        <label className="chk"><input type="checkbox" checked={withSource} onChange={(e) => openSource(e.target.checked)} />确认时登记为候选来源</label>
      )}
      {canDecide && withSource && <SourceForm value={src} onChange={setSrc} />}
      {error && <div className="err" role="alert">{error}</div>}
      {canDecide && (
        <div className="row">
          <button type="button" className="btnp" disabled={busy || !ids.length || !sourceOk} onClick={() => decide("approve")}>确认（{ids.length}）</button>
          <button type="button" className="btn" disabled={busy || !ids.length} onClick={() => decide("reject")}>不做</button>
        </div>
      )}
    </section>
  );
}

export default function SourceConfirm() {
  const { user } = useAuth();
  const canDecide = hasRole(user, "knowledge_admin");
  const [groups, setGroups] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => {
    setError("");
    try {
      setGroups(await api.kbConfirmQueue());
    } catch (e) {
      setError(e.message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
      <div className="sub">
        本周采集计划中不在来源白名单内的知识缺口，按网址主机分组。确认后缺口转为“已确认”，登记的来源仍是候选，
        须由另一位知识管理员转正式进白名单后才排进采集下发包；“不做”关闭缺口。
        {!canDecide && " 你是知识专家，只能查看；确认与不做由知识管理员操作。"}
      </div>
      {error && <div className="err" role="alert">{error}</div>}
      {groups && groups.length === 0 && <section className="pnl pad sub">本周没有待确认的来源。</section>}
      {(groups || []).map((g) => <Group key={g.group} g={g} canDecide={canDecide} onDone={load} />)}
    </div>
  );
}
