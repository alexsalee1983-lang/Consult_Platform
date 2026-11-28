import { useEffect, useState } from "react";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";

// 项目驾驶舱的“结项清单”与“终止项目”（四期 A31 ⑦⑧）：成员可看清单；终止仅项目经理可见，需填原因并二次确认
export default function Closeout({ p, onChange }) {
  const { user } = useAuth();
  const [list, setList] = useState(null);
  const [isManager, setIsManager] = useState(false);
  const [step, setStep] = useState(0);   // 0 收起 / 1 填原因 / 2 二次确认
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => api.closeout(p.code).then(setList).catch(() => setList(null));
  useEffect(() => { load(); }, [p.code]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    api.members(p.code)
      .then((ms) => setIsManager(ms.some((m) => m.username === user?.username && m.project_role === "manager")))
      .catch(() => setIsManager(false));
  }, [p.code, user]);

  async function terminate() {
    setBusy(true);
    setError("");
    try {
      await api.terminateProject(p.code, reason.trim());
      setStep(0);
      setReason("");
      await load();
      onChange?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!list) return null;
  const rows = [
    ["未签署的交付件", list.unsigned.map((d) => `${d.code} ${d.title}`)],
    ["未关闭的确认卡", list.open_cards.map((c) => c.title)],
    ["需更新的章", list.stale_sections.map((s) => `${s.deliverable_code} 第 ${s.seq} 章 ${s.title}`)],
  ];
  return (
    <section className="pnl pad" aria-label="结项清单">
      <div className="sb">
        <h2 className="h2">结项清单</h2>
        {list.terminated_on
          ? <span className="chip c-r">已于 {list.terminated_on} 终止</span>
          : <span className={`chip ${list.ready ? "c-g" : "c-a"}`}>{list.ready ? "可以结项" : "尚有未完成项"}</span>}
      </div>
      {rows.map(([label, items]) => (
        <div key={label} className="sub">
          <b style={{ color: items.length ? "var(--amber)" : "var(--ok)" }}>{label} {items.length}</b>
          {items.length > 0 && <ul className="lst">{items.map((t) => <li key={t}>{t}</li>)}</ul>}
        </div>
      ))}
      {isManager && !list.terminated_on && step === 0 && (
        <div className="row"><button className="btn sm" onClick={() => setStep(1)}>终止项目</button></div>
      )}
      {isManager && step > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label className="sub">终止原因（不写客户名称，最多 200 字）
            <input className="inp" maxLength={200} value={reason} disabled={step === 2}
              onChange={(e) => setReason(e.target.value)} />
          </label>
          {step === 2 && (
            <div className="sub" style={{ color: "var(--crim)" }}>
              确认终止？将停止自动推进、关闭全部待办确认卡，保留期从今天起算，此操作不能撤销。
            </div>
          )}
          {error && <div className="err" role="alert">{error}</div>}
          <div className="row">
            {step === 1
              ? <button className="btnp" disabled={!reason.trim()} onClick={() => setStep(2)}>下一步</button>
              : <button className="btnp" disabled={busy} onClick={terminate}>确认终止</button>}
            <button className="btn" disabled={busy} onClick={() => { setStep(0); setError(""); }}>取消</button>
          </div>
        </div>
      )}
    </section>
  );
}
