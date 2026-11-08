import { useState } from "react";
import { useNavigate } from "react-router";
import { homePath, useAuth } from "../auth.jsx";
import { TopBar } from "../components/common.jsx";

// 与后端 app/core/security.py 的口令策略一致（最短长度以服务端配置为准，默认 12）
const MIN_LENGTH = 12;

function policyHint(pw) {
  if (!pw) return "";
  const kinds = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(pw)).length;
  if (pw.length < MIN_LENGTH) return `还差 ${MIN_LENGTH - pw.length} 位`;
  if (kinds < 3) return "还需要包含大写字母、小写字母、数字、符号中的至少三类";
  return "";
}

export default function Password() {
  const { user, changePassword } = useAuth();
  const navigate = useNavigate();
  const forced = Boolean(user?.mustChangePassword);
  const [form, setForm] = useState({ old: "", next: "", again: "" });
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const hint = policyHint(form.next);
  const mismatch = form.again && form.again !== form.next;
  const ready = form.old && form.next && !hint && form.again === form.next;

  async function submit(e) {
    e.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError("");
    try {
      const wasForced = forced;
      await changePassword(form.old, form.next);
      setForm({ old: "", next: "", again: "" });
      if (wasForced) navigate(homePath(user), { replace: true });
      else setDone(true);
    } catch (err) {
      setError(err.message || "修改失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <TopBar cap="我的账号" title="修改口令" />
      <section className="pnl pad" style={{ maxWidth: 520 }} aria-label="修改口令">
        {forced && (
          <div className="blocked" role="note">
            <div style={{ fontWeight: 700, color: "var(--amber)" }}>请先把初始口令改成只有你自己知道的口令</div>
            <div className="sub" style={{ marginTop: 2 }}>初始口令由系统管理员转交，改完才能使用平台的其他功能。动态码不变。</div>
          </div>
        )}
        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label className="field">
            <span>{forced ? "初始口令" : "当前口令"}</span>
            <input type="password" autoComplete="current-password" value={form.old} onChange={set("old")} required autoFocus />
          </label>
          <label className="field">
            <span>新口令（至少 {MIN_LENGTH} 位，含大写、小写、数字、符号中的三类）</span>
            <input type="password" autoComplete="new-password" value={form.next} onChange={set("next")} required
              aria-describedby="pw-hint" />
          </label>
          {hint && <div id="pw-hint" className="sub">{hint}</div>}
          <label className="field">
            <span>再输入一次新口令</span>
            <input type="password" autoComplete="new-password" value={form.again} onChange={set("again")} required />
          </label>
          {mismatch && <div className="sub" style={{ color: "var(--crim)" }}>两次输入的新口令不一致</div>}
          {error && <div className="err" role="alert">{error}</div>}
          {done && <div role="status" style={{ color: "var(--ok)" }}>口令已修改。其他设备上的登录已失效，需要重新登录。</div>}
          <div className="row">
            <button className="btnp" type="submit" disabled={!ready || busy} style={{ height: 36 }}>
              {busy ? "正在修改……" : "修改口令"}
            </button>
          </div>
          <div className="sub">当前口令连续输错 5 次，账号会临时锁定 15 分钟。</div>
        </form>
      </section>
    </>
  );
}
