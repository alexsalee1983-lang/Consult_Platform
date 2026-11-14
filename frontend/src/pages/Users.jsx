import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { ROLE_NAMES, useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";

// 网页上能授予的角色：安全审计员由单独任命，只能在服务器命令行授予（三权分立）
const BUSINESS = ["consultant", "project_lead", "knowledge_expert", "knowledge_admin"];
const SYS_ADMIN = "sys_admin";
const AUDITOR = "security_auditor";
const USERNAME_RE = /^[A-Za-z][A-Za-z0-9_.-]{1,63}$/;

function fmt(at) {
  return at ? new Date(at).toLocaleString("zh-CN", { hour12: false }) : "从未登录";
}

// 系统管理员与其他角色互斥：勾选系统管理员时清掉业务角色，反之亦然
function toggleRole(roles, code) {
  if (roles.includes(code)) return roles.filter((r) => r !== code);
  if (code === SYS_ADMIN) return [SYS_ADMIN];
  return [...roles.filter((r) => r !== SYS_ADMIN), code];
}

function RolePicker({ value, onChange, idPrefix }) {
  return (
    <fieldset className="roles" aria-label="系统角色">
      {[...BUSINESS, SYS_ADMIN].map((code) => (
        <label key={code} className="chk" htmlFor={`${idPrefix}-${code}`}>
          <input id={`${idPrefix}-${code}`} type="checkbox" checked={value.includes(code)} onChange={() => onChange(toggleRole(value, code))} />
          {ROLE_NAMES[code]}
        </label>
      ))}
      <label className="chk" title="安全审计员只能在服务器命令行任命" style={{ opacity: 0.5 }}>
        <input type="checkbox" disabled checked={false} readOnly aria-label={`${ROLE_NAMES[AUDITOR]}（只能在服务器命令行任命）`} />{ROLE_NAMES[AUDITOR]}（命令行任命）
      </label>
    </fieldset>
  );
}

function Copyable({ label, value }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="cred-row">
      <span className="sub" style={{ width: 84, flex: "none" }}>{label}</span>
      <span className="mono cred-v">{value}</span>
      <button type="button" className="btn" style={{ height: 26, padding: "0 8px", flex: "none" }} onClick={copy}>{copied ? "已复制" : "复制"}</button>
    </div>
  );
}

// 建账号、重置口令、重置动态码后显示一次的凭据
function Credentials({ cred, onClose }) {
  const { user, initial_password: pw, totp_secret: secret, totp_uri: uri, kind } = cred;
  const grouped = secret ? secret.match(/.{1,4}/g).join(" ") : "";
  return (
    <section className="pnl pad cred" role="alert" aria-label="一次性凭据">
      <div className="sb">
        <h2 className="h2">{{ create: "账号已创建", password: "口令已重置", totp: "动态码已重置" }[kind]} · {user.display_name}（{user.username}）</h2>
        <span className="chip c-a">只显示这一次</span>
      </div>
      <div className="sub">
        请当面或通过公司内部渠道交给本人，不要发到外部聊天工具或邮件；关闭后无法再次查看，只能重新生成。{kind !== "create" && "该账号原来的登录已全部失效。"}
      </div>
      <Copyable label="用户名" value={user.username} />
      {pw && <Copyable label="初始口令" value={pw} />}
      {secret && <Copyable label="动态码密钥" value={grouped} />}
      {uri && <Copyable label="导入链接" value={uri} />}
      <ol className="sub" style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
        {secret && <li>本人在手机动态码应用（如 Microsoft Authenticator、Google Authenticator）里选“手动输入密钥”，账户名填用户名，密钥填上面的动态码密钥，类型选“基于时间”。</li>}
        {pw && <li>本人用初始口令和动态码登录后，平台会要求先改口令。</li>}
        {!pw && <li>口令不变，用新的动态码登录。</li>}
      </ol>
      <div className="row"><button type="button" className="btnp" onClick={onClose}>已转交，关闭</button></div>
    </section>
  );
}

function CreateForm({ onCreated, onCancel }) {
  const [form, setForm] = useState({ username: "", display_name: "", roles: ["consultant"] });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const validName = USERNAME_RE.test(form.username);

  async function submit(e) {
    e.preventDefault();
    if (!validName || busy) return;
    setBusy(true);
    setError("");
    try {
      onCreated(await api.createUser(form.username, form.display_name.trim(), form.roles));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="新建用户">
      <h2 className="h2">新建用户</h2>
      <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div className="row" style={{ flexWrap: "wrap", alignItems: "flex-start" }}>
          <label className="field" style={{ minWidth: 220 }}>
            <span>用户名（字母开头，可含数字 . _ -）</span>
            <input value={form.username} onChange={(e) => setForm((f) => ({ ...f, username: e.target.value.trim() }))} autoFocus required />
            {form.username && !validName && <span className="sub" style={{ color: "var(--crim)" }}>用户名格式不对</span>}
          </label>
          <label className="field" style={{ minWidth: 220 }}>
            <span>姓名</span>
            <input value={form.display_name} maxLength={64} onChange={(e) => setForm((f) => ({ ...f, display_name: e.target.value }))} />
          </label>
        </div>
        <RolePicker value={form.roles} onChange={(roles) => setForm((f) => ({ ...f, roles }))} idPrefix="new" />
        <div className="sub">平台会生成一次性初始口令和动态码密钥，创建后只显示一次。项目成员由各项目的项目经理在项目驾驶舱里添加。</div>
        {error && <div className="err" role="alert">{error}</div>}
        <div className="row">
          <button className="btnp" type="submit" disabled={!validName || busy}>{busy ? "创建中……" : "创建"}</button>
          <button className="btn" type="button" onClick={onCancel}>取消</button>
        </div>
      </form>
    </section>
  );
}

function Status({ u }) {
  return (
    <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>
      <span className={`chip ${u.is_active ? "c-g" : "c-n"}`}>{u.is_active ? "启用" : "已停用"}</span>
      {u.locked && <span className="chip c-r">已锁定</span>}
      {u.must_change_password && <span className="chip c-a">待改初始口令</span>}
      {!u.must_change_password && u.password_expired && <span className="chip c-a">口令已过期</span>}
      {u.inactive && <span className="chip c-a" title="长期没有登录，确认不再使用的请停用">长期未登录</span>}
    </span>
  );
}

const CONFIRM = {
  disable: (u) => `停用 ${u.display_name}（${u.username}）？该账号会立即退出登录，之后无法登录。`,
  "reset-password": (u) => `为 ${u.display_name}（${u.username}）生成新的初始口令？原口令立即失效，账号会退出登录。`,
  "reset-totp": (u) => `为 ${u.display_name}（${u.username}）重置动态码？原手机上的动态码立即失效，账号会退出登录。`,
};

export default function Users() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState([]);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [cred, setCred] = useState(null);
  const [editing, setEditing] = useState(null);   // { username, roles }
  const [busy, setBusy] = useState("");

  const load = useCallback(() => api.adminUsers().then(setUsers).catch((e) => setError(e.message)), []);
  useEffect(() => { load(); }, [load]);

  async function act(u, action) {
    if (CONFIRM[action] && !window.confirm(CONFIRM[action](u))) return;
    setBusy(`${u.username}:${action}`);
    setError("");
    try {
      const r = await api.userAction(u.username, action);
      if (action === "reset-password") setCred({ ...r, kind: "password" });
      if (action === "reset-totp") setCred({ ...r, kind: "totp" });
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function saveRoles() {
    if (editing.roles.includes(SYS_ADMIN) && !window.confirm("设为系统管理员后，该账号会退出所有项目，并且看不到任何业务数据。继续？")) return;
    setBusy(`${editing.username}:roles`);
    setError("");
    try {
      await api.setRoles(editing.username, editing.roles);
      setEditing(null);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <TopBar cap="账号管理 · 三权分立" title="用户与角色">
        {!creating && <button type="button" className="btnp" onClick={() => { setCreating(true); setCred(null); }}>＋ 新建用户</button>}
      </TopBar>
      {cred && <Credentials cred={cred} onClose={() => setCred(null)} />}
      {creating && (
        <CreateForm onCancel={() => setCreating(false)}
          onCreated={(r) => { setCreating(false); setCred({ ...r, kind: "create" }); load(); }} />
      )}
      <section className="pnl pad" aria-label="用户列表">
        <div className="sub">
          系统管理员只管账号和角色，看不到项目和对话；安全审计员由单独任命，只能在服务器命令行授予，这里不能修改审计员账号。
          你不能修改自己的角色或停用自己。所有操作都写入审计日志。
        </div>
        {error && <div className="err" role="alert">{error}</div>}
        <div style={{ overflowX: "auto" }}>
          <table className="tbl">
            <thead>
              <tr><th>用户名</th><th>姓名</th><th>系统角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const self = u.username === me?.username;
                const auditor = u.roles.includes(AUDITOR);
                const locked = self || auditor;
                const why = self ? "不能修改自己的账号" : auditor ? "审计员账号只能在服务器命令行管理" : undefined;
                const isBusy = busy.startsWith(`${u.username}:`);
                const isEditing = editing?.username === u.username;
                return [
                  <tr key={u.username}>
                    <td className="mono">{u.username}{self && <span className="sub">（我）</span>}</td>
                    <td>{u.display_name}</td>
                    <td>
                      <span className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                        {u.roles.map((r) => <span key={r} className={`chip ${r === AUDITOR || r === SYS_ADMIN ? "c-v" : "c-c"}`}>{ROLE_NAMES[r] || r}</span>)}
                        {u.roles.length === 0 && <span className="sub">未分配</span>}
                      </span>
                    </td>
                    <td><Status u={u} /></td>
                    <td style={{ whiteSpace: "nowrap" }}>{fmt(u.last_login_at)}</td>
                    <td>
                      <span className="row" style={{ gap: 4, flexWrap: "wrap" }} title={why}>
                        <button type="button" className="btn sm" disabled={locked || isBusy}
                          onClick={() => setEditing(isEditing ? null : { username: u.username, roles: u.roles })}>角色</button>
                        {u.is_active
                          ? <button type="button" className="btn sm" disabled={locked || isBusy} onClick={() => act(u, "disable")}>停用</button>
                          : <button type="button" className="btn sm" disabled={locked || isBusy} onClick={() => act(u, "enable")}>启用</button>}
                        {u.locked && <button type="button" className="btn sm" disabled={locked || isBusy} onClick={() => act(u, "unlock")}>解锁</button>}
                        <button type="button" className="btn sm" disabled={locked || isBusy} onClick={() => act(u, "reset-password")}>重置口令</button>
                        <button type="button" className="btn sm" disabled={locked || isBusy} onClick={() => act(u, "reset-totp")}>重置动态码</button>
                      </span>
                    </td>
                  </tr>,
                  isEditing && (
                    <tr key={`${u.username}-roles`}>
                      <td colSpan={6} style={{ background: "var(--s2)" }}>
                        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "4px 0" }}>
                          <RolePicker value={editing.roles} onChange={(roles) => setEditing((x) => ({ ...x, roles }))} idPrefix={`edit-${u.username}`} />
                          <div className="row">
                            <button type="button" className="btnp" disabled={isBusy} onClick={saveRoles}>保存角色</button>
                            <button type="button" className="btn" onClick={() => setEditing(null)}>取消</button>
                            <span className="sub">去掉全部业务角色后，该账号看不到任何项目。</span>
                          </div>
                        </div>
                      </td>
                    </tr>
                  ),
                ];
              })}
              {users.length === 0 && !error && <tr><td colSpan={6} className="sub">加载中……</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
