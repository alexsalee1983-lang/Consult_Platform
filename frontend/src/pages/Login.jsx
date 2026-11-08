import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";
import { useAuth } from "../auth.jsx";

export default function Login() {
  const { status, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [form, setForm] = useState({ username: "", password: "", totp: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // 只接受站内路径，防止跳转到外部地址
  const from = typeof location.state?.from === "string" && location.state.from.startsWith("/") &&
    !location.state.from.startsWith("//") ? location.state.from : "/projects";

  if (status === "authed") return <Navigate to={from} replace />;

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: k === "totp" ? e.target.value.replace(/\D/g, "").slice(0, 6) : e.target.value }));

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await login(form.username.trim(), form.password, form.totp);
      navigate(from, { replace: true });
    } catch (err) {
      setError(err.message || "登录失败");
      setForm((f) => ({ ...f, password: "", totp: "" }));
    } finally {
      setBusy(false);
    }
  }

  const ready = form.username.trim() && form.password && form.totp.length === 6;

  return (
    <main className="login">
      <form className="pnl pad login-card" onSubmit={submit} aria-labelledby="login-title">
        <div className="logo" style={{ padding: 0 }}>
          <div className="logo-mark">
            <svg aria-hidden="true" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#0A0D12" strokeWidth="2.4" strokeLinejoin="round">
              <path d="M12 2l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V5z" />
              <path d="M8.5 12l2.5 2.5 4.5-5" strokeLinecap="round" />
            </svg>
          </div>
          <div style={{ lineHeight: 1.2 }}>
            <div id="login-title" style={{ fontWeight: 700, fontSize: 15 }}>安全咨询专家平台</div>
            <div style={{ fontSize: 10, letterSpacing: ".16em", color: "var(--tx3)" }}>ADVISORY · 内部</div>
          </div>
        </div>

        <label className="field">
          <span>用户名</span>
          <input autoComplete="username" value={form.username} onChange={set("username")} autoFocus required />
        </label>
        <label className="field">
          <span>口令</span>
          <input type="password" autoComplete="current-password" value={form.password} onChange={set("password")} required />
        </label>
        <label className="field">
          <span>动态码（认证器中的 6 位数字）</span>
          <input className="mono" inputMode="numeric" autoComplete="one-time-code" value={form.totp} onChange={set("totp")}
            pattern="\d{6}" maxLength={6} required />
        </label>

        {error && <div className="err" role="alert">{error}</div>}

        <button className="btnp" type="submit" disabled={busy || !ready} style={{ height: 36 }}>
          {busy ? "正在登录……" : "登录"}
        </button>
        <div className="sub">
          过渡期使用本地账号 + 动态码，接入公司统一认证后改为单点登录。连续 5 次失败会临时锁定，30 分钟无操作自动退出。
        </div>
      </form>
    </main>
  );
}
