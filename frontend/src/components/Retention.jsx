import { useEffect, useState } from "react";
import { api, RETENTION } from "../api.js";
import { useAuth } from "../auth.jsx";

const today = () => new Date().toLocaleDateString("sv-SE");   // YYYY-MM-DD（本地日期）

// 项目驾驶舱里的“验收与保留期”：成员都能看；项目经理登记或更正验收日和保留期
export default function Retention({ p, onChange }) {
  const { user } = useAuth();
  const [isManager, setIsManager] = useState(false);
  const [open, setOpen] = useState(false);
  const [date, setDate] = useState(p.accepted_on || today());
  const [months, setMonths] = useState(p.retention_months ?? 6);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.members(p.code)
      .then((ms) => setIsManager(ms.some((m) => m.username === user?.username && m.project_role === "manager")))
      .catch(() => setIsManager(false));
  }, [p.code, user]);

  const st = RETENTION[p.retention_state] || RETENTION.active;
  const left = p.days_to_deletion;

  async function submit() {
    setBusy(true);
    setError("");
    try {
      await api.setAcceptance(p.code, date, Number(months));
      setOpen(false);
      onChange?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="验收与保留期">
      <div className="sb"><h2 className="h2">验收与保留期</h2><span className={`chip ${st.cls}`}>{st.label}</span></div>
      {p.accepted_on ? (
        <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span>验收日 {p.accepted_on} · 保留 {p.retention_months} 个月</span>
          <span>到期日 <span className="mono">{p.deletion_due_on}</span>
            {left > 0 ? ` · 还有 ${left} 天` : left === 0 ? " · 今天到期" : ` · 已过期 ${-left} 天`}</span>
          {p.retention_state === "due_soon" && <span style={{ color: "var(--amber)" }}>30 天内到期：请提前导出要归档的交付件，到期后经人工确认删除项目资料。</span>}
          {p.retention_state === "due" && <span style={{ color: "var(--crim)" }}>已到期：待人工确认删除项目资料并形成删除记录。</span>}
        </div>
      ) : (
        <div className="sub">项目验收后登记验收日，平台按保留期（默认 6 个月）计算到期日，到期前 30 天提醒。</div>
      )}
      {isManager && !open && (
        <div className="row"><button className="btn sm" onClick={() => setOpen(true)}>{p.accepted_on ? "更正" : "登记验收"}</button></div>
      )}
      {isManager && open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label className="sub">验收日
            <input className="inp" type="date" max={today()} value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <label className="sub">保留期（月，0～24；默认 6，按合同约定或客户制度可调整）
            <input className="inp" type="number" min={0} max={24} value={months} onChange={(e) => setMonths(e.target.value)} />
          </label>
          {error && <div className="err" role="alert">{error}</div>}
          <div className="row">
            <button className="btnp" disabled={busy || !date || months === ""} onClick={submit}>保存</button>
            <button className="btn" disabled={busy} onClick={() => setOpen(false)}>取消</button>
          </div>
        </div>
      )}
    </section>
  );
}
