import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";
import { canDecrypt, decryptPackage, uploadCards } from "../fieldPackage.js";

const today = () => new Date().toLocaleDateString("sv-SE");   // YYYY-MM-DD（本地日期）
const FIELD = { item: "评估项", finding: "现状", magnitude: "数量级", onsite_refs: "现场证据编号", client_reviewed_on: "审阅日期" };

function download(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1000);
}

// 严格档项目驾驶舱：客户书面同意记录 + 去标识摘要卡（M4 ④⑥）。
// 摘要卡在现场用离线现场工具填写、自检、加密导出（.cpcard）；这里在浏览器里解密后整批上传，口令不发给服务器。
export default function StrictCards({ code, onChange }) {
  const { user } = useAuth();
  const [isManager, setIsManager] = useState(false);
  const [consents, setConsents] = useState([]);
  const [cards, setCards] = useState(null);
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);         // {mode: "consent"|"revoke", ref, date}
  const [pending, setPending] = useState(null);   // 解密后待上传的卡
  const [problems, setProblems] = useState([]);
  const [pass, setPass] = useState("");
  const [pasted, setPasted] = useState("");
  const [showAll, setShowAll] = useState(false);
  const fileRef = useRef(null);

  const load = useCallback(() => {
    api.consents(code).then(setConsents).catch(() => setConsents([]));
    api.summaryCards(code).then(setCards).catch(() => setCards([]));
  }, [code]);
  useEffect(load, [load]);
  useEffect(() => {
    api.members(code)
      .then((ms) => setIsManager(ms.some((m) => m.username === user?.username && m.project_role === "manager")))
      .catch(() => setIsManager(false));
  }, [code, user]);

  const active = consents.find((c) => !c.revoked);
  const live = (cards || []).filter((c) => !c.withdrawn);
  const shown = showAll ? cards || [] : live;

  async function run(fn, done) {
    setBusy(true);
    setError("");
    setOk("");
    try {
      await fn();
      if (done) setOk(done);
      load();
      onChange?.();
      return true;
    } catch (e) {
      setError(e.message);
      // 平台按第几张（从 1 起）回报：规则问题 {card, field, kinds}，重复 {card, same_as}
      setProblems([...(e.detail?.problems || []),
        ...(e.detail?.duplicates || []).map((d) => ({ card: d.card, field: "dup", kinds: [`与 ${d.same_as} 内容相同`] }))]);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function openFile(e) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    setError("");
    setOk("");
    setProblems([]);
    setBusy(true);
    try {
      const { kind, body } = await decryptPackage(JSON.parse(await f.text()), pass);
      setPending(uploadCards(kind, body, code));
    } catch (err) {
      setPending(null);
      setError(err instanceof SyntaxError ? "不是现场工具导出的文件" : err.message);
    } finally {
      setPass("");
      setBusy(false);
    }
  }

  function readPasted() {
    setError("");
    setProblems([]);
    try {
      const body = JSON.parse(pasted);
      setPending(uploadCards("upload", body, code));
      setPasted("");
    } catch (err) {
      setError(err instanceof SyntaxError ? "粘贴的内容不是 JSON" : err.message);
    }
  }

  async function upload() {
    const n = pending.length;
    if (await run(() => api.uploadCards(code, pending), `已上传 ${n} 张摘要卡`)) {
      setPending(null);
      setProblems([]);
    }
  }

  async function template() {
    setError("");
    try {
      download(`${code}-摘要卡模板-${today().replace(/-/g, "")}.json`, JSON.stringify(await api.cardTemplate(code), null, 1));
    } catch (e) {
      setError(e.message);
    }
  }

  const submitForm = async () => {
    const done = form.mode === "consent"
      ? await run(() => api.recordConsent(code, form.ref.trim(), form.date), "已登记客户书面同意")
      : await run(() => api.revokeConsent(code, active.id, form.ref.trim(), form.date),
        "已登记撤回：摘要卡内容和据此写成的未签署草稿已清空，未结束的交付链已取消");
    if (done) setForm(null);
  };

  return (
    <section className="pnl pad" aria-label="客户同意与摘要卡">
      <div className="sb">
        <h2 className="h2">客户同意与摘要卡</h2>
        <span className={`chip ${active ? "c-g" : "c-r"}`}>{active ? "客户已书面同意" : "没有有效同意"}</span>
      </div>
      <div className="sub">
        资料不出客户现场。顾问在现场用离线现场工具填写去标识摘要卡，客户对接人逐份审阅签字后导出加密上传包，离场后在这里上传。
        Agent 只读摘要卡（S-xx），交付件只写占位符版本。
      </div>

      {active ? (
        <div className="sub">同意记录：文号 <span className="mono">{active.doc_ref}</span> · 签署 {active.signed_on} · {active.recorded_by} 登记</div>
      ) : (
        <div className="note-amber">没有有效的客户书面同意，不能上传摘要卡，交付件写入和交付链也不可用。{isManager ? "" : "请项目经理登记。"}</div>
      )}
      {consents.filter((c) => c.revoked).map((c) => (
        <div key={c.id} className="sub">已撤回：文号 <span className="mono">{c.doc_ref}</span> · 撤回文号 <span className="mono">{c.revoke_ref}</span> · {c.revoked_on}</div>
      ))}

      {isManager && !form && (
        <div className="row">
          {!active && <button className="btn sm" onClick={() => setForm({ mode: "consent", ref: "", date: today() })}>登记客户书面同意</button>}
          {active && <button className="btn sm" onClick={() => setForm({ mode: "revoke", ref: "", date: today() })}>客户书面撤回同意</button>}
        </div>
      )}
      {form && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {form.mode === "revoke" && (
            <div className="err sub">撤回后立即清空：全部摘要卡内容、据摘要卡写成的未签署交付件正文、评审意见和交付链产出，未结束的运行取消。已签署的交付件保留。不能恢复。</div>
          )}
          <label className="sub">{form.mode === "consent" ? "书面同意文号" : "书面撤回通知文号"}（不写客户名称）
            <input className="inp" maxLength={64} value={form.ref} onChange={(e) => setForm({ ...form, ref: e.target.value })} />
          </label>
          <label className="sub">{form.mode === "consent" ? "签署日期" : "收到日期"}
            <input className="inp" type="date" max={today()} value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value })} />
          </label>
          <div className="row">
            <button className="btnp" disabled={busy || !form.ref.trim() || !form.date}
              onClick={() => (form.mode === "consent" || window.confirm("确认登记撤回？清空的内容不能恢复。")) && submitForm()}>
              {form.mode === "consent" ? "登记" : "确认撤回"}
            </button>
            <button className="btn" disabled={busy} onClick={() => setForm(null)}>取消</button>
          </div>
        </div>
      )}

      <div className="row" style={{ flexWrap: "wrap" }}>
        <button className="btn sm" onClick={template}>下载空白模板（离场前，供现场工具导入）</button>
      </div>

      {active && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="cap">上传摘要卡</span>
          {canDecrypt() ? (
            <div className="row" style={{ flexWrap: "wrap" }}>
              <input className="inp" type="password" autoComplete="off" placeholder="上传包口令" value={pass}
                onChange={(e) => setPass(e.target.value)} style={{ width: 200 }} />
              <button className="btn sm" disabled={busy || !pass} onClick={() => fileRef.current?.click()}>{busy ? "解密中…" : "选择上传包（.cpcard）"}</button>
              <input ref={fileRef} type="file" hidden accept=".cpcard,application/json" onChange={openFile} />
            </div>
          ) : (
            <>
              <div className="sub">当前页面不是 HTTPS，浏览器不能解密上传包。请在现场工具“查看加密包”解密，把显示的内容粘贴到下面。</div>
              <textarea className="inp" rows={4} value={pasted} onChange={(e) => setPasted(e.target.value)} placeholder='{"project_code": "...", "cards": [...]}' />
              <div className="row"><button className="btn sm" disabled={!pasted.trim()} onClick={readPasted}>读取</button></div>
            </>
          )}
          <div className="sub">口令只在本机浏览器里用于解密，不发给服务器。</div>
        </div>
      )}

      {pending && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="cap">待上传 {pending.length} 张（核对后上传；平台整批检查，任何一张不过整批不保存）</span>
          <ul className="lst">
            {pending.map((c, i) => (
              <li key={i}>
                <div className="sb"><span><span className="mono sub">第 {i + 1} 张</span> {c.item}</span><span className="sub">{c.magnitude}</span></div>
                <div className="sub">{c.finding}</div>
                <div className="sub">现场 {c.onsite_refs.join("、") || "—"} · 审阅 {c.client_reviewed_on || "—"}</div>
                {problems.filter((p) => p.card === i + 1).map((p) => (
                  <div key={p.field} className="err sub">{p.field === "dup" ? "重复" : FIELD[p.field] || p.field}：{p.kinds.join("、")}</div>
                ))}
              </li>
            ))}
          </ul>
          <div className="row">
            <button className="btnp" disabled={busy} onClick={upload}>上传</button>
            <button className="btn" disabled={busy} onClick={() => { setPending(null); setProblems([]); }}>放弃</button>
          </div>
        </div>
      )}

      {error && <div className="err" role="alert">{error}{pending && problems.length > 0 && "；问题已在上面逐张标出"}</div>}
      {ok && <div className="sub" style={{ color: "var(--ok)" }} role="status">{ok}</div>}

      <div className="sb">
        <span className="cap">摘要卡 {live.length} 张</span>
        {(cards || []).length > live.length && (
          <button className="btn sm" onClick={() => setShowAll(!showAll)}>{showAll ? "只看可用的" : "含已撤下的"}</button>
        )}
      </div>
      <ul className="lst">
        {shown.map((c) => (
          <li key={c.code}>
            <div className="sb">
              <span><span className="mono" style={{ marginRight: 8 }}>{c.code}</span>{c.withdrawn ? <span className="sub">（已撤下）</span> : c.item}</span>
              {!c.withdrawn && (isManager || c.uploaded_by === user?.username) && (
                <button className="btn sm" disabled={busy}
                  onClick={() => window.confirm(`撤下 ${c.code}？内容立即清空，交付件里引用 [${c.code}] 的地方会核对不上。`)
                    && run(() => api.withdrawCard(code, c.code), `已撤下 ${c.code}`)}>撤下</button>
              )}
            </div>
            {!c.withdrawn && (
              <>
                <div className="sub">{c.finding}</div>
                <div className="sub">
                  {c.magnitude && `${c.magnitude} · `}现场 {c.onsite_refs.join("、") || "—"} · 客户审阅 {c.client_reviewed_on} · {c.uploaded_by} 上传
                </div>
              </>
            )}
            {c.withdrawn && <div className="sub">{c.withdrawn_by} 撤下</div>}
          </li>
        ))}
        {cards && shown.length === 0 && <li className="sub">还没有摘要卡</li>}
      </ul>
    </section>
  );
}
