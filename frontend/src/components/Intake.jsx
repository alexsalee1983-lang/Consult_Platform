import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../api.js";

// 四期 A32：合同要点卡（含授权台账），放在“输入”页最上面；新建项目第二、三步也在这里做。
// 规则见 app/project/intake.py：字段状态四选一；已核对的卡不改（补充协议或合同到达上传出新版本）；
// 附件一、附件三（严格档附件二）签署前客户资料一律拒收。只放人要做的事（负责人 11-27），后台抽取不展示过程。
const STATE_CLS = { verified: "c-g", unstated: "c-n", clarify: "c-a", extra: "c-v" };
const UPLOAD_KINDS = ["contract", "supplement", "award", "letter", "proposal", "presales", "email"];

export function defaultState(v) {
  return v.state || (v.value ? "verified" : "unstated");
}

export default function Intake({ code, step }) {
  const [st, setSt] = useState(null);
  const [vals, setVals] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);
  const [kind, setKind] = useState("contract");
  const [auth, setAuth] = useState({ kind: "annex3", status: "signed", version: "", signed_on: "" });

  const load = useCallback(() => api.intake(code).then((x) => {
    setSt(x);
    if (x.card && x.card.status !== "checked") {
      setVals(Object.fromEntries(Object.entries(x.card.fields).map(([k, v]) => [k, { value: v.value, state: defaultState(v) }])));
    } else setVals(null);
    return x;
  }).catch((e) => setError(e.message)), [code]);
  useEffect(() => { load(); }, [load]);
  // 抽取中每 3 秒取一次结果（一次性后台任务，完成即停）
  useEffect(() => {
    if (st?.card?.status !== "extracting") return undefined;
    const t = setTimeout(load, 3000);
    return () => clearTimeout(t);
  }, [st, load]);

  if (!st) return error ? <section className="pnl pad err" role="alert">{error}</section> : null;
  if (!st.intake) return null;                        // 旧流程项目：合同要点仍在 D1 表单里导入
  const card = st.card;
  const run = async (fn) => {
    setBusy(true);
    setError("");
    try { await fn(); await load(); } catch (e) { setError(typeof e.message === "string" ? e.message : "保存失败"); } finally { setBusy(false); }
  };
  const save = (checked) => run(() => api.saveContractCard(code, vals, checked));
  const upload = () => run(async () => { await api.uploadBasis(code, file, kind); setFile(null); });
  const blank = () => setVals(Object.fromEntries(st.fields.map((f) => [f.key, { value: "", state: "unstated" }])));
  const isManager = st.is_manager;
  const es = st.early_start;
  const strict = st.tier === "strict";
  const kinds = UPLOAD_KINDS.filter((k) => es || k === "contract" || k === "supplement");

  return (
    <section className="pnl pad" aria-label="合同要点卡" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="sb">
        <h2 className="h2">合同要点卡{card && <span className="sub mono"> v{card.version}</span>}
          {card?.provisional && <span className="chip c-a" style={{ marginLeft: 8 }}>先行启动 · 待合同确认</span>}
          {card?.status === "checked" && <span className="chip c-g" style={{ marginLeft: 8 }}>已核对</span>}
        </h2>
        {step && <span className="sub">新建项目 · 第 {step} 步，共 3 步</span>}
      </div>
      {es && !es.contract_signed && (
        <div className={`chip ${es.overdue ? "c-r" : "c-a"}`} role="status">
          先行启动第 {es.days} 天 · 合同未签{es.approved ? "" : " · 待部门负责人确认"}{es.overdue ? ` · 已满 ${es.escalate_days} 天，已提醒部门负责人` : ""}
        </div>
      )}
      {st.gate1.missing.length > 0 && <div className="sub">关口 1 还差：{st.gate1.missing.join("、")}</div>}
      {error && <div className="err" role="alert">{error}</div>}

      {card?.status === "extracting" && <div className="sub">正在从{card.basis_name}抽取要点…</div>}
      {card?.status === "failed" && <div className="err">{card.error}</div>}

      {isManager && (!card || card.status === "checked" || card.status === "failed") && !strict && (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span className="sub">{card ? "补充协议或合同到达：上传出新版本" : "第 1 步 · 上传依据"}</span>
          <select className="inp" aria-label="依据类型" value={kind} onChange={(e) => setKind(e.target.value)}>
            {kinds.map((k) => <option key={k} value={k}>{st.basis_kinds[k]}</option>)}
          </select>
          <input type="file" aria-label="依据文件" accept=".docx,.pdf,.md,.txt" onChange={(e) => setFile(e.target.files[0] || null)} />
          <button className="btn sm" disabled={busy || !file} onClick={upload}>上传并抽取</button>
          {!card && <button className="btn sm" disabled={busy} onClick={blank}>没有文件，手填</button>}
        </div>
      )}
      {isManager && strict && !card && !vals && (
        <div className="row"><span className="sub">严格档不上传合同，由项目经理手填要点卡。</span>
          <button className="btn sm" onClick={blank}>开始手填</button></div>
      )}

      {vals && isManager && (
        <>
          <div className="sub">第 2 步 · 核对要点卡：逐项标状态；“待与客户澄清”的要点之后生成澄清清单。</div>
          <table className="tbl">
            <thead><tr><th>要点</th><th>内容</th><th>状态</th><th>出处</th></tr></thead>
            <tbody>
              {st.fields.map((f) => (
                <tr key={f.key}>
                  <td>{f.name}<div className="sub">{f.drives}</div></td>
                  <td><textarea className="inp" aria-label={f.name} rows={2} style={{ width: "100%" }} maxLength={2000}
                    value={vals[f.key]?.value || ""} onChange={(e) => setVals({ ...vals, [f.key]: { ...vals[f.key], value: e.target.value } })} /></td>
                  <td><select className="inp" aria-label={`${f.name}状态`} value={vals[f.key]?.state || ""}
                    onChange={(e) => setVals({ ...vals, [f.key]: { ...vals[f.key], state: e.target.value } })}>
                    {Object.entries(st.states).map(([k, n]) => <option key={k} value={k}>{n}</option>)}
                  </select></td>
                  <td className="sub">{card?.fields?.[f.key]?.source || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="row">
            <button className="btnp" disabled={busy} onClick={() => save(true)}>核对完成</button>
            <button className="btn sm" disabled={busy} onClick={() => save(false)}>先存草稿</button>
            <span className="sub">出现测评、认证、密评结论用语的不能保存，请改为差距分析、符合性初判口径。</span>
          </div>
        </>
      )}

      {card?.status === "checked" && (
        <table className="tbl">
          <tbody>
            {st.fields.map((f) => {
              const v = card.fields[f.key];
              return (
                <tr key={f.key}>
                  <td style={{ whiteSpace: "nowrap" }}>{f.name}</td>
                  <td style={{ whiteSpace: "pre-wrap" }}>{v.value || <span className="sub">—</span>}</td>
                  <td><span className={`chip ${STATE_CLS[v.state] || "c-n"}`}>{st.states[v.state] || "未标"}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3 className="h2" style={{ fontSize: 15 }}>授权与同意（授权台账）</h3>
      {st.data_refusal && <div className="chip c-a" role="status">{st.data_refusal}</div>}
      <table className="tbl">
        <thead><tr><th>授权</th><th>状态</th><th>版本</th><th>签署日期</th><th>记录人</th></tr></thead>
        <tbody>
          {st.ledger.map((x) => (
            <tr key={x.kind}>
              <td>{x.name}{x.required && <span className="sub"> · 关口 1 必需</span>}</td>
              <td>{x.current ? <span className={`chip ${x.current.status === "signed" ? "c-g" : "c-r"}`}>{x.current.status_name}</span> : <span className="sub">未登记</span>}</td>
              <td className="mono">{x.current?.version || ""}</td>
              <td className="mono">{x.current?.signed_on || ""}</td>
              <td>{x.current?.recorded_by || ""}</td>
            </tr>
          ))}
          {strict && (
            <tr><td>严格档摘要卡书面同意</td>
              <td>{st.strict_consent ? <span className="chip c-g">已签</span> : <span className="sub">在摘要卡页登记</span>}</td>
              <td className="mono">{st.strict_consent?.doc_ref || ""}</td><td className="mono">{st.strict_consent?.signed_on || ""}</td><td /></tr>
          )}
        </tbody>
      </table>
      {isManager && (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <span className="sub">第 3 步 · 登记授权</span>
          <select className="inp" aria-label="授权类型" value={auth.kind} onChange={(e) => setAuth({ ...auth, kind: e.target.value })}>
            {st.ledger.map((x) => <option key={x.kind} value={x.kind}>{x.name}</option>)}
          </select>
          <select className="inp" aria-label="授权状态" value={auth.status} onChange={(e) => setAuth({ ...auth, status: e.target.value })}>
            <option value="signed">已签</option><option value="withdrawn">已撤回</option>
          </select>
          <input className="inp" aria-label="版本" placeholder="版本，如 v2026.1" maxLength={32} value={auth.version} onChange={(e) => setAuth({ ...auth, version: e.target.value })} />
          <input className="inp" type="date" aria-label="签署日期" value={auth.signed_on} onChange={(e) => setAuth({ ...auth, signed_on: e.target.value })} />
          <button className="btn sm" disabled={busy || !auth.signed_on} onClick={() => run(() => api.recordAuthorization(code, auth))}>登记</button>
          <Link className="btn sm" to={`/project/${encodeURIComponent(code)}/settings?new=1`} style={{ display: "inline-flex", alignItems: "center", textDecoration: "none" }}>配置成员</Link>
        </div>
      )}
      <div className="sub">台账只记签署状态，不存附件正文；变更留历史（共 {st.history.length} 条）。</div>
    </section>
  );
}
