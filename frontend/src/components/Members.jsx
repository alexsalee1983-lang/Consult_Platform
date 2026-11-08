import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";

const PROJECT_ROLES = { manager: "项目经理", member: "项目成员" };

// 项目驾驶舱里的成员面板：成员都能看；只有本项目的项目经理能添加、调整、移出
export default function Members({ code }) {
  const { user } = useAuth();
  const [members, setMembers] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState("");
  const [found, setFound] = useState([]);
  const [role, setRole] = useState("member");

  const load = useCallback(() => api.members(code).then(setMembers).catch((e) => setError(e.message)), [code]);
  useEffect(() => { load(); }, [load]);

  const isManager = members.some((m) => m.username === user?.username && m.project_role === "manager");
  // 输入停顿 300 毫秒后再查，避免每敲一个字查一次
  useEffect(() => {
    const term = q.trim();
    if (!isManager || !term) { setFound([]); return undefined; }
    const inProject = new Set(members.map((m) => m.username));
    const t = setTimeout(() => {
      api.lookupUsers(term).then((rows) => setFound(rows.filter((r) => !inProject.has(r.username)))).catch(() => setFound([]));
    }, 300);
    return () => clearTimeout(t);
  }, [q, isManager, members]);

  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      setMembers(await fn());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const add = (username) => run(async () => { const r = await api.putMember(code, username, role); setQ(""); return r; });
  const change = (m, r) => run(() => api.putMember(code, m.username, r));
  const remove = (m) => {
    const self = m.username === user?.username;
    if (!window.confirm(self ? "把自己移出本项目？移出后你将看不到这个项目。" : `把 ${m.display_name} 移出本项目？`)) return;
    run(() => api.removeMember(code, m.username));
  };

  return (
    <section className="pnl pad" aria-label="项目成员">
      <div className="sb"><h2 className="h2">项目成员</h2><span className="mono sub">{members.length}</span></div>
      <ul className="lst">
        {members.map((m) => (
          <li key={m.username} className="sb" style={{ gap: 6 }}>
            <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
              {m.display_name}<span className="sub mono" style={{ marginLeft: 6 }}>{m.username}</span>
            </span>
            {isManager ? (
              <span className="row" style={{ gap: 4, flex: "none" }}>
                <label className="field">
                  <span className="sr">{m.display_name} 的项目身份</span>
                  <select className="sm" value={m.project_role} disabled={busy} onChange={(e) => change(m, e.target.value)}>
                    {Object.entries(PROJECT_ROLES).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </label>
                <button type="button" className="btn sm" disabled={busy} onClick={() => remove(m)} aria-label={`移出 ${m.display_name}`}>移出</button>
              </span>
            ) : (
              <span className={`chip ${m.project_role === "manager" ? "c-a" : "c-n"}`}>{PROJECT_ROLES[m.project_role] || m.project_role}</span>
            )}
          </li>
        ))}
      </ul>
      {isManager && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <label className="field" style={{ flex: 1, minWidth: 0 }}>
              <span>添加成员（按用户名或姓名查找）</span>
              <input value={q} onChange={(e) => setQ(e.target.value)} maxLength={64} />
            </label>
            <label className="field">
              <span className="sr">新成员的项目身份</span>
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                {Object.entries(PROJECT_ROLES).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          </div>
          {found.length > 0 && (
            <ul className="lst">
              {found.map((u) => (
                <li key={u.username} className="sb">
                  <span>{u.display_name}<span className="sub mono" style={{ marginLeft: 6 }}>{u.username}</span></span>
                  <button type="button" className="btn sm" disabled={busy} onClick={() => add(u.username)}>添加为{PROJECT_ROLES[role]}</button>
                </li>
              ))}
            </ul>
          )}
          {q.trim() && found.length === 0 && <div className="sub">没有找到可添加的同事（只能添加持业务角色、已启用的账号）</div>}
        </div>
      )}
      {!isManager && <div className="sub">成员由本项目的项目经理调整。</div>}
      {error && <div className="err" role="alert">{error}</div>}
    </section>
  );
}
