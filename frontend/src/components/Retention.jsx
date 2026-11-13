import { useEffect, useState } from "react";
import { api, DELETION_KIND, DELETION_STATUS, FLOWBACK, RETENTION } from "../api.js";
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
  const [dels, setDels] = useState([]);
  const [early, setEarly] = useState(false);
  const [ref, setRef] = useState("");
  const [received, setReceived] = useState(today());

  const loadDels = () => api.projectDeletions(p.code).then(setDels).catch(() => setDels([]));
  useEffect(() => { loadDels(); }, [p.code]);   // eslint-disable-line react-hooks/exhaustive-deps

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

  const openDel = dels.find((d) => d.status === "pending" || d.status === "confirmed");

  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      setEarly(false);
      setRef("");
      await loadDels();
      onChange?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const prepare = p.flowback_allowed === false
    ? "请提前导出客户需要的交付件（含已签署版本）。"
    : "请提前导出客户需要的交付件（含已签署版本），并把可复用的做法去标识后经投料箱回流。";

  return (
    <section className="pnl pad" aria-label="验收与保留期">
      <div className="sb"><h2 className="h2">验收与保留期</h2><span className={`chip ${st.cls}`}>{st.label}</span></div>
      {p.accepted_on ? (
        <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span>验收日 {p.accepted_on} · 保留 {p.retention_months} 个月</span>
          <span>到期日 <span className="mono">{p.deletion_due_on}</span>
            {left > 0 ? ` · 还有 ${left} 天` : left === 0 ? " · 今天到期" : ` · 已过期 ${-left} 天`}</span>
          {p.retention_state === "due_soon" && <span style={{ color: "var(--amber)" }}>30 天内到期：{prepare}到期后由项目负责人确认删除项目资料。</span>}
          {p.retention_state === "due" && <span style={{ color: "var(--crim)" }}>已到期：待项目负责人确认删除项目资料并形成删除记录。</span>}
        </div>
      ) : (
        <div className="sub">项目验收后登记验收日，平台按保留期（默认 6 个月）计算到期日，到期前 30 天提醒。</div>
      )}
      {isManager && !open && (
        <div className="row"><button className="btn sm" onClick={() => setOpen(true)}>{p.accepted_on ? "更正" : "登记验收"}</button></div>
      )}
      {p.flowback_allowed === false && <div className="sub">客户不同意回流：本项目的做法不回流到知识库。</div>}
      {dels.map((d) => {
        const st = DELETION_STATUS[d.status] || DELETION_STATUS.pending;
        return (
          <div key={d.id} className="sub row" style={{ flexWrap: "wrap" }}>
            <span className={`chip ${st.cls}`}>删除 · {st.label}</span>
            <span>{DELETION_KIND[d.kind]}{d.request_ref ? `（${d.request_ref}）` : ""} · 应完成 {d.due_by}</span>
            {d.overdue && <span className="chip c-r">已逾期</span>}
            {d.flowback && <span>经验回流：{FLOWBACK[d.flowback]}</span>}
            {d.status === "cancelled" && <span>已撤销</span>}
          </div>
        );
      })}
      {isManager && !open && !early && (
        <div className="row">
          <button className="btn sm" disabled={busy} onClick={() => run(() => api.setFlowback(p.code, p.flowback_allowed === false))}>
            {p.flowback_allowed === false ? "取消“客户不同意回流”" : "标记客户不同意回流"}
          </button>
          {!openDel && <button className="btn sm" onClick={() => setEarly(true)}>客户要求提前删除</button>}
        </div>
      )}
      {isManager && early && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div className="sub">收到客户书面要求后登记，项目负责人确认后删除，10 个工作日内完成。已签署的交付件也会删除。</div>
          <label className="sub">书面要求文号（不写客户名称）
            <input className="inp" maxLength={64} value={ref} onChange={(e) => setRef(e.target.value)} />
          </label>
          <label className="sub">收到日期
            <input className="inp" type="date" max={today()} value={received} onChange={(e) => setReceived(e.target.value)} />
          </label>
          <div className="row">
            <button className="btnp" disabled={busy || !ref.trim() || !received}
              onClick={() => run(() => api.requestEarlyDeletion(p.code, ref.trim(), received))}>提交</button>
            <button className="btn" disabled={busy} onClick={() => setEarly(false)}>取消</button>
          </div>
        </div>
      )}
      {error && !open && <div className="err" role="alert">{error}</div>}
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
