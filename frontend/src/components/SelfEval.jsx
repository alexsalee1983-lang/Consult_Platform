import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { canDecrypt, decryptPackage } from "../fieldPackage.js";
import { confirmText, importBody, importSummary, REMIND_TEXT, SELFEVAL_STATUS } from "../survey/selfeval.js";

const inDays = (n) => new Date(Date.now() + n * 86400000).toISOString().slice(0, 10);

function saveText(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/html;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// 四期 E3②：客户自评包。从调研包按受访角色生成加密离线网页 + Excel 模板；顾问用公司邮箱发出，平台只记发出时间并到期提醒。
// 交回的加密答卷在本机解密（口令不上传）后导入答案池；未答过半、与资料大面积不一致时先确认；可撤销本次导入。严格档不在这里做。
export default function SelfEval({ code, packs }) {
  const [list, setList] = useState([]);
  const [form, setForm] = useState({ pack: "", respondent_role: "", due_date: inDays(7) });
  const [made, setMade] = useState(null);          // 生成结果（含口令，只在内存，收起即丢）
  const [imp, setImp] = useState(null);            // {code, file, pass, xlsx}
  const [pending, setPending] = useState(null);    // 需确认：{text, retry}
  const [error, setError] = useState("");
  const [ok, setOk] = useState("");
  const [extras, setExtras] = useState([]);        // 备注与表外内容：不入池，列给顾问决定
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.selfevalPacks(code).then(setList).catch(() => setList([]));
  }, [code]);
  useEffect(load, [load]);
  const packCode = form.pack || packs[0]?.code || "";

  async function run(fn) {
    setBusy(true);
    setError("");
    setOk("");
    setExtras([]);
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const create = () => run(async () => {
    const r = await api.createSelfeval(code, { ...form, pack: packCode, respondent_role: form.respondent_role.trim() });
    setMade(r);
    saveText(r.filename, r.html);
    load();
  });

  async function doImport(sp, send) {
    setPending(null);
    try {
      const r = await send(false);
      setOk(importSummary(r));
      setExtras(r.extras || []);
      setImp(null);
      load();
    } catch (e) {
      const text = e.status === 409 ? confirmText(e.detail) : "";
      if (!text) throw e;
      setPending({ code: sp.code, text, retry: () => run(async () => {
        const r = await send(true);
        setPending(null);
        setOk(importSummary(r));
        setExtras(r.extras || []);
        setImp(null);
        load();
      }) });
    }
  }

  const importPage = (sp) => run(async () => {
    const pkg = JSON.parse(await imp.file.text());
    const { kind, body } = await decryptPackage(pkg, imp.pass);
    await doImport(sp, (confirm) => api.selfevalImport(code, sp.code, importBody(kind, body, sp, confirm)));
  });
  const importXlsx = (sp) => run(() => doImport(sp, (confirm) => api.selfevalImportXlsx(code, sp.code, imp.file, confirm)));

  const undo = (sp) => run(async () => {
    const r = await api.selfevalUndo(code, sp.code);
    setOk(`已撤销 ${sp.code} 的导入，移除答案 ${r.removed} 条`);
    load();
  });

  return (
    <div className="pnl pad" style={{ gap: 8 }} aria-label="客户自评">
      <div className="sb">
        <span className="h2">客户自评</span>
        <span className="sub">封闭事实题 · 加密离线网页 + Excel 兜底</span>
      </div>
      <div className="sub">
        生成后下载网页和 Excel 模板，用公司邮箱作附件发给客户，口令另行告知（电话或短信，不和附件同一封邮件）。
        平台不对外发信，只记发出时间；到期前一天、当天提醒你，逾期 2 天升级项目经理。答卷在本机解密，口令不上传。
      </div>
      {error && <div className="err sub">{error}</div>}
      {ok && <div className="sub" style={{ color: "var(--ok)" }}>{ok}</div>}
      {extras.length > 0 && (
        <div className="sub">
          <span className="cap">备注与表外内容（未入答案池，请判断是否在调研助手代录或现场追问）</span>
          <ul>{extras.map((x, i) => <li key={i}><span className="mono">{x.qid || `第 ${x.row} 行`}</span> {x.text}</li>)}</ul>
        </div>
      )}

      {packs.length === 0 ? <span className="sub">先生成调研包，再从调研包拆出客户自评包。</span> : (
        <div className="row" style={{ flexWrap: "wrap" }}>
          <label className="sub">调研包
            <select className="inp" value={packCode} onChange={(e) => setForm({ ...form, pack: e.target.value })}>
              {packs.map((k) => <option key={k.code} value={k.code}>{k.code}</option>)}
            </select>
          </label>
          <label className="sub">受访角色（写角色不写姓名）
            <input className="inp" maxLength={32} value={form.respondent_role} placeholder="例如 安全运营负责人"
              onChange={(e) => setForm({ ...form, respondent_role: e.target.value })} />
          </label>
          <label className="sub">截止日
            <input className="inp" type="date" value={form.due_date} onChange={(e) => setForm({ ...form, due_date: e.target.value })} />
          </label>
          <button className="btn sm" disabled={busy || !form.respondent_role.trim() || !form.due_date} onClick={create}>生成客户自评包</button>
        </div>
      )}

      {made && (
        <div className="pnl pad" style={{ gap: 6 }}>
          <div className="sb">
            <span className="h2">{made.code} 第 {made.version} 版已生成</span>
            <button className="btn sm" onClick={() => setMade(null)}>我已记下口令，收起</button>
          </div>
          <div className="sub">打开口令（只显示这一次，平台不保存；收起后无法再看，丢了只能重新生成新版本）：</div>
          <div className="mono" style={{ fontSize: 16 }}>{made.password}</div>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <button className="btn sm" onClick={() => saveText(made.filename, made.html)}>再次下载离线网页</button>
            <button className="btn sm" onClick={() => run(() => api.selfevalTemplate(code, made.code))}>下载 Excel 模板</button>
          </div>
          <label className="sub">邮件正文（复制到公司邮箱）
            <textarea className="inp" rows={6} readOnly value={made.mail_text} />
          </label>
        </div>
      )}

      {list.map((sp) => {
        const [label, cls] = SELFEVAL_STATUS[sp.status] || [sp.status, ""];
        const open = sp.status === "issued" || sp.status === "sent";
        return (
          <div key={sp.code} style={{ display: "grid", gap: 4 }}>
            <div className="row" style={{ flexWrap: "wrap" }}>
              <span className="mono sub">{sp.code} v{sp.version}</span>
              <span className="sub">{sp.pack} · {sp.respondent_role} · {sp.questions} 题 · 截止 {sp.due_date}</span>
              <span className={`chip ${cls}`}>{label}</span>
              {open && sp.remind_stage > 0 && <span className={`chip ${sp.remind_stage === 3 ? "c-r" : "c-a"}`}>{REMIND_TEXT[sp.remind_stage]}</span>}
              {sp.status === "imported" && <span className="sub">导入答案 {sp.answers} 条</span>}
              {open && (
                <>
                  <button className="btn sm" disabled={busy} onClick={() => run(() => api.selfevalTemplate(code, sp.code))}>Excel 模板</button>
                  {sp.status === "issued" && <button className="btn sm" disabled={busy} onClick={() => run(async () => { await api.selfevalSent(code, sp.code); load(); })}>已发出</button>}
                  <button className="btn sm" disabled={busy} onClick={() => setImp({ code: sp.code, file: null, pass: "", xlsx: false })}>导入答卷</button>
                </>
              )}
              {sp.status === "imported" && <button className="btn sm" disabled={busy} onClick={() => undo(sp)}>撤销本次导入</button>}
            </div>
            {imp?.code === sp.code && (
              <div className="row" style={{ flexWrap: "wrap" }}>
                <label className="sub">
                  <input type="checkbox" checked={imp.xlsx} onChange={(e) => setImp({ ...imp, xlsx: e.target.checked, file: null })} /> Excel 答卷
                </label>
                <input className="inp" type="file" accept={imp.xlsx ? ".xlsx" : ".cpself"}
                  onChange={(e) => setImp({ ...imp, file: e.target.files?.[0] || null })} />
                {!imp.xlsx && (
                  <input className="inp" type="password" autoComplete="off" style={{ width: 220 }} placeholder="答卷口令（只在本机解密）"
                    value={imp.pass} onChange={(e) => setImp({ ...imp, pass: e.target.value })} />
                )}
                <button className="btnp sm" disabled={busy || !imp.file || (!imp.xlsx && (!imp.pass || !canDecrypt()))}
                  onClick={() => (imp.xlsx ? importXlsx(sp) : importPage(sp))}>导入</button>
                <button className="btn sm" onClick={() => { setImp(null); setPending(null); }}>取消</button>
                {!imp.xlsx && !canDecrypt() && <span className="sub">当前页面不是 HTTPS，浏览器不能解密；请改用 Excel 答卷</span>}
              </div>
            )}
            {pending?.code === sp.code && (
              <div className="row" style={{ flexWrap: "wrap" }}>
                <span className="chip c-a">{pending.text}</span>
                <button className="btnp sm" disabled={busy} onClick={pending.retry}>已核对，确认导入</button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
