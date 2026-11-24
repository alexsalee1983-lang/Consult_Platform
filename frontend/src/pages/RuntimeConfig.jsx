import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";
import { STATUS, STATUS_CLS, REASON_MAX, buildChanges, validateChange, canDecide, rollbackTargets, versionTitle,
  valueText } from "../runtimeConfig.js";

// 运行层配置编辑（四期 G3③，系统管理员）：改值须填理由；切模型、开自动发布等敏感项进待确认，须另一位系统管理员确认。
function ValueInput({ item, draft, onChange }) {
  const text = draft ?? (typeof item.value === "boolean" ? String(item.value) : String(item.value ?? ""));
  if (typeof item.value === "boolean") {
    return (
      <select className="inp" value={text} onChange={(e) => onChange(e.target.value)} aria-label={item.key}>
        <option value="true">是</option>
        <option value="false">否</option>
      </select>
    );
  }
  return <input className="inp mono" value={text} onChange={(e) => onChange(e.target.value)} aria-label={item.key} />;
}

function Items({ items }) {
  if (!items.length) return <span className="sub">无变化</span>;
  return items.map((i) => (
    <div key={i.key} className="mono">{i.key}：{valueText(i.old_value)} → {valueText(i.value)}</div>
  ));
}

export default function RuntimeConfig() {
  const { user } = useAuth();
  const me = user?.username || "";
  const [items, setItems] = useState(null);
  const [versions, setVersions] = useState([]);
  const [sha, setSha] = useState("");
  const [drafts, setDrafts] = useState({});
  const [reason, setReason] = useState("");
  const [rollbackTo, setRollbackTo] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setError("");
    try {
      const [reg, hist] = await Promise.all([api.configRegistry(), api.runtimeVersions()]);
      setItems(reg.layers.find((l) => l.key === "runtime")?.items || []);
      setSha(reg.sha);
      setVersions(hist.versions || []);
    } catch (e) {
      setError(e.message);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const { values, errors } = buildChanges(items || [], drafts);
  const changeErr = validateChange(reason, values);
  const pending = versions.filter((v) => v.status === "pending");
  const targets = rollbackTargets(versions);

  async function act(fn, ok) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const r = await fn();
      setNotice(r?.needs_confirm && r.status === "pending" ? `${ok}，含敏感项，待另一位系统管理员确认` : ok);
      setDrafts({});
      setReason("");
      setRollbackTo("");
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const sensitiveChanged = (items || []).some((i) => i.confirm && i.key in values);

  return (
    <>
      <TopBar cap="系统管理 · 四期 G3" title="运行层配置">
        <button type="button" className="btn" disabled={busy} onClick={load}>刷新</button>
      </TopBar>
      <div className="sub">
        只能改运行层配置；锁定层（数据保护开关、密钥、模型地址）仍只在部署机 .env 里改。每次改动、回滚都要填理由并记审计（只记长度和哈希）。
        标“须确认”的项（切模型、开自动发布等）提交后进待确认，须另一位系统管理员确认才生效。
      </div>
      {sha && <div className="sub">当前生效配置哈希：<span className="mono">{sha}</span></div>}
      {error && <div className="err" role="alert">{error}</div>}
      {notice && <div className="note-amber" role="status">{notice}</div>}
      {!items && !error && <div className="sub">加载中……</div>}

      {pending.length > 0 && (
        <section className="pnl pad" aria-label="待二次确认">
          <h2 className="h2">待二次确认（{pending.length}）</h2>
          <table className="tbl">
            <thead><tr><th>版本</th><th>改动</th><th>理由</th><th>发起人</th><th>操作</th></tr></thead>
            <tbody>
              {pending.map((v) => {
                const ok = canDecide(v, me);
                const tip = ok ? "" : "发起人本人不能确认或驳回";
                return (
                  <tr key={v.id}>
                    <td>{versionTitle(v)}</td>
                    <td><Items items={v.items} /></td>
                    <td>{v.reason}</td>
                    <td>{v.requested_by}</td>
                    <td className="row" style={{ gap: 6 }}>
                      <button type="button" className="btn sm" disabled={busy || !ok} title={tip}
                        onClick={() => act(() => api.runtimeDecide(v.id, true), `版本 ${v.id} 已确认生效`)}>确认</button>
                      <button type="button" className="btn sm" disabled={busy || !ok} title={tip}
                        onClick={() => act(() => api.runtimeDecide(v.id, false), `版本 ${v.id} 已驳回`)}>驳回</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}

      {items && (
        <section className="pnl pad" aria-label="运行层配置">
          <h2 className="h2">运行层配置</h2>
          <table className="tbl">
            <thead><tr><th>配置项</th><th>当前值</th><th>来源</th><th>新值</th></tr></thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.key}>
                  <td className="mono">{it.key} {it.confirm && <span className="chip c-a">须确认</span>}</td>
                  <td className="mono">{valueText(it.value)}</td>
                  <td>{it.source}</td>
                  <td>
                    <ValueInput item={it} draft={drafts[it.key]}
                      onChange={(t) => setDrafts((d) => ({ ...d, [it.key]: t }))} />
                    {errors[it.key] && <span className="sub" style={{ color: "var(--crim)" }}>{errors[it.key]}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <label className="field">
            <span>改动或回滚理由（必填，不超过 {REASON_MAX} 字）</span>
            <textarea className="inp" rows={2} maxLength={REASON_MAX} value={reason}
              onChange={(e) => setReason(e.target.value)} />
          </label>
          <div className="row" style={{ gap: 8 }}>
            <button type="button" className="btnp" disabled={busy || !!changeErr || Object.keys(errors).length > 0}
              title={changeErr || ""} onClick={() => act(() => api.runtimeChange(reason.trim(), values), "改动已提交")}>
              提交改动{Object.keys(values).length ? `（${Object.keys(values).length} 项）` : ""}
            </button>
            {sensitiveChanged && <span className="chip c-a">含须确认项</span>}
            {changeErr && reason && <span className="sub">{changeErr}</span>}
          </div>
        </section>
      )}

      <section className="pnl pad" aria-label="版本历史">
        <div className="sb">
          <h2 className="h2">版本历史</h2>
          <div className="row" style={{ gap: 6 }}>
            <select className="inp" value={rollbackTo} onChange={(e) => setRollbackTo(e.target.value)} aria-label="回滚到">
              <option value="">回滚到……</option>
              {targets.map((t) => <option key={t} value={t}>{t === 0 ? "全部恢复 .env" : `版本 ${t} 之后的状态`}</option>)}
            </select>
            <button type="button" className="btn" disabled={busy || rollbackTo === "" || !reason.trim()}
              title={reason.trim() ? "" : "先在上方填写理由"}
              onClick={() => {
                const to = Number(rollbackTo);
                const what = to === 0 ? "全部恢复 .env" : `回滚到版本 ${to}`;
                if (window.confirm(`确定${what}？`)) act(() => api.runtimeRollback(reason.trim(), to), "回滚已提交");
              }}>回滚</button>
          </div>
        </div>
        {versions.length === 0 ? <div className="sub">还没有改动，全部取 .env。</div> : (
          <table className="tbl">
            <thead><tr><th>版本</th><th>状态</th><th>改动</th><th>理由</th><th>发起 / 决定</th><th>时间</th></tr></thead>
            <tbody>
              {versions.map((v) => (
                <tr key={v.id}>
                  <td>{versionTitle(v)}</td>
                  <td><span className={`chip ${STATUS_CLS[v.status] || "c-n"}`}>{STATUS[v.status] || v.status}</span></td>
                  <td><Items items={v.items} /></td>
                  <td>{v.reason}</td>
                  <td>{v.requested_by}{v.decided_by && v.decided_by !== v.requested_by ? ` / ${v.decided_by}` : ""}</td>
                  <td className="sub">{(v.decided_at || v.created_at || "").replace("T", " ").slice(0, 16)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
