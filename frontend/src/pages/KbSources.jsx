import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";
import { Loading, TopBar } from "../components/common.jsx";
import {
  AUTHORITY_KINDS, AUTHORITY_LEVEL_LABEL, SOURCE_STATUS, sourceActions, validateSource,
} from "../sourceRegistry.js";

// 三期 B16②：来源登记表页面。候选 → 试用 → 正式 → 降级 → 停用；转正式在页面上由知识管理员批准。
const FILTERS = [["", "全部"], ...Object.entries(SOURCE_STATUS).map(([k, v]) => [k, v.label])];
const EMPTY = { name: "", url: "", license: "", authority_kind: "", publisher: "", note: "" };
const fmt = (t) => (t ? String(t).slice(0, 16).replace("T", " ") : "—");

function RegisterForm({ onDone }) {
  const [f, setF] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    const pre = validateSource(f);
    if (pre) { setErr(pre); return; }
    setBusy(true);
    setErr("");
    try {
      const s = await api.kbSourceCreate(f);
      setF(EMPTY);
      onDone(s);
    } catch (ex) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="pnl pad" aria-label="登记来源" onSubmit={submit} style={{ gap: 10 }}>
      <h2 className="h2">登记候选来源</h2>
      <label className="field"><span>来源名称 *</span>
        <input value={f.name} maxLength={200} onChange={set("name")} /></label>
      <label className="field"><span>来源地址 *</span>
        <input value={f.url} maxLength={500} placeholder="https://" onChange={set("url")} /></label>
      <div className="form-grid">
        <label className="field"><span>权威类别 *</span>
          <select value={f.authority_kind} onChange={set("authority_kind")}>
            <option value="">请选择</option>
            {AUTHORITY_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select></label>
        <label className="field"><span>发布机构</span>
          <input value={f.publisher} maxLength={200} onChange={set("publisher")} /></label>
      </div>
      <label className="field"><span>许可 *</span>
        <input value={f.license} maxLength={300} placeholder="能否内部引用、是否须注明出处" onChange={set("license")} /></label>
      <label className="field"><span>备注</span>
        <textarea className="inp" rows={2} value={f.note} maxLength={2000} onChange={set("note")} /></label>
      {err && <div className="err" role="alert">{err}</div>}
      <div className="row"><button type="submit" className="btnp" disabled={busy}>登记为候选</button></div>
      <div className="sub">登记后为候选状态；采集机只抓试用、正式的来源。文字内容会过机密级检测，不要写口令、内网地址等。</div>
    </form>
  );
}

function Detail({ src, user, onChanged }) {
  const [events, setEvents] = useState(null);
  const [pending, setPending] = useState(null);   // 需要写理由的流转
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    setEvents(null);
    setPending(null);
    setErr("");
    api.kbSourceEvents(src.code).then(setEvents).catch((e) => setErr(e.message));
  }, [src.code, src.status]);

  async function go(to, why = "") {
    setBusy(true);
    setErr("");
    try {
      const s = await api.kbSourceStatus(src.code, to, why);
      setPending(null);
      setReason("");
      onChanged(s);
    } catch (ex) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  }

  const st = SOURCE_STATUS[src.status] || { label: src.status, cls: "c-n" };
  const acts = sourceActions(src, user);
  return (
    <section className="pnl pad" aria-label="来源详情" style={{ gap: 10 }}>
      <div className="sb"><h2 className="h2">{src.name}</h2><span className={`chip ${st.cls}`}>{st.label}</span></div>
      <div className="sub mono">{src.code} · {src.host}</div>
      <div className="sb"><span className="sub">地址</span><span className="mono" style={{ wordBreak: "break-all" }}>{src.url}</span></div>
      <div className="sb"><span className="sub">权威</span><span>{src.authority_kind} · {AUTHORITY_LEVEL_LABEL[src.authority_level] || src.authority_level}</span></div>
      <div className="sb"><span className="sub">许可</span><span>{src.license}</span></div>
      {src.publisher && <div className="sb"><span className="sub">发布机构</span><span>{src.publisher}</span></div>}
      {src.note && <div className="sub">{src.note}</div>}
      <div className="sb"><span className="sub">登记人</span><span>{src.created_by} · {fmt(src.created_at)}</span></div>
      {src.approved_by && <div className="sb"><span className="sub">批准为正式</span><span>{src.approved_by} · {fmt(src.approved_at)}</span></div>}

      <h2 className="h2" style={{ marginTop: 6 }}>状态流转</h2>
      <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        {acts.map((a) => (
          <button key={a.to} type="button" className={a.to === "active" ? "btnp" : "btn"}
            disabled={busy || a.disabled} title={a.hint || undefined}
            onClick={() => (a.needsReason ? (setPending(a), setReason("")) : go(a.to))}>{a.label}</button>
        ))}
      </div>
      {acts.filter((a) => a.hint).map((a) => <div key={a.to} className="sub">{a.label}：{a.hint}</div>)}
      {pending && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label className="field"><span>{pending.label}理由 *</span>
            <input value={reason} maxLength={300} autoFocus onChange={(e) => setReason(e.target.value)} /></label>
          <div className="row" style={{ gap: 6 }}>
            <button type="button" className="btnp" disabled={busy || !reason.trim()}
              onClick={() => go(pending.to, reason.trim())}>确认{pending.label}</button>
            <button type="button" className="btn" disabled={busy} onClick={() => setPending(null)}>取消</button>
          </div>
        </div>
      )}
      {err && <div className="err" role="alert">{err}</div>}

      <h2 className="h2" style={{ marginTop: 6 }}>事件记录</h2>
      {events === null ? <div className="sub">加载中……</div> : (
        <ul className="lst">
          {events.map((e, i) => (
            <li key={i} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span className="sb">
                <span>{e.from_status ? `${SOURCE_STATUS[e.from_status]?.label || e.from_status} → ` : "登记为 "}
                  {SOURCE_STATUS[e.to_status]?.label || e.to_status}</span>
                <span className="mono sub">{fmt(e.created_at)}</span>
              </span>
              <span className="sub">{e.actor}{e.reason ? `：${e.reason}` : ""}</span>
            </li>
          ))}
          {events.length === 0 && <li className="sub">暂无记录</li>}
        </ul>
      )}
    </section>
  );
}

export default function KbSources() {
  const { user } = useAuth();
  const [status, setStatus] = useState("");
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [sel, setSel] = useState("");

  const load = useCallback(() => {
    api.kbSources(status).then((xs) => { setList(xs); setError(null); }).catch(setError);
  }, [status]);
  useEffect(load, [load]);

  function changed(s) {
    setSel(s.code);
    setList((xs) => {
      const rest = (xs || []).filter((x) => x.code !== s.code);
      return !status || s.status === status ? [s, ...rest] : rest;
    });
    load();
  }

  const current = (list || []).find((x) => x.code === sel);
  return (
    <>
      <TopBar cap="知识 · 采集白名单" title="来源登记表" />
      <div className="grid-main">
        <section className="pnl pad" aria-label="来源列表" style={{ gap: 10 }}>
          <div className="tabs" role="tablist">
            {FILTERS.map(([k, l]) => (
              <button key={k || "all"} type="button" role="tab" aria-selected={status === k}
                className={status === k ? "on" : undefined} onClick={() => setStatus(k)}>{l}</button>
            ))}
          </div>
          {!list ? <Loading error={error} /> : (
            <div style={{ overflowX: "auto" }}>
              <table className="tbl">
                <thead><tr><th>编号</th><th>来源</th><th>权威类别</th><th>等级</th><th>许可</th><th>状态</th></tr></thead>
                <tbody>
                  {list.map((s) => {
                    const st = SOURCE_STATUS[s.status] || { label: s.status, cls: "c-n" };
                    return (
                      <tr key={s.code} onClick={() => setSel(s.code)} aria-selected={s.code === sel}
                        style={{ cursor: "pointer", background: s.code === sel ? "var(--s2)" : undefined }}>
                        <td className="mono">{s.code}</td>
                        <td>{s.name}<div className="sub mono">{s.host}</div></td>
                        <td>{s.authority_kind}</td>
                        <td className="mono">{s.authority_level}</td>
                        <td className="sub">{s.license}</td>
                        <td><span className={`chip ${st.cls}`}>{st.label}</span></td>
                      </tr>
                    );
                  })}
                  {list.length === 0 && <tr><td colSpan={6} className="sub">没有{status ? SOURCE_STATUS[status].label : ""}来源</td></tr>}
                </tbody>
              </table>
            </div>
          )}
          <div className="sub">按权威等级排序（一级法律法规与监管文件、二级标准、三级参考资料）。正式来源属已批准来源，修订后直接下发采集机，入库审核把关。</div>
        </section>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, minWidth: 0 }}>
          {current && <Detail src={current} user={user} onChanged={changed} />}
          <RegisterForm onDone={changed} />
        </div>
      </div>
    </>
  );
}
