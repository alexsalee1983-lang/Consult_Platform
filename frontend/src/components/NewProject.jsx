import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";

// 网页立项（二期 A3）：项目负责人选产品、勾模块、定档、指定首位项目经理（默认本人）。规则见 app/project/configs.py
export default function NewProject({ onClose }) {
  const { user } = useAuth();
  const nav = useNavigate();
  const [products, setProducts] = useState(null);
  const [f, setF] = useState({ code: "", product: "", modules: [], tier: "", manager: "", industry: "", weeks: 12 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.catalogProducts().then((ps) => {
      setProducts(ps);
      if (ps.length) setF((x) => ({ ...x, product: ps[0].code }));
    }).catch((e) => setError(e.message));
  }, []);

  const prod = (products || []).find((p) => p.code === f.product);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const toggle = (m) => setF({ ...f, modules: f.modules.includes(m) ? f.modules.filter((x) => x !== m) : [...f.modules, m] });

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const p = await api.createProject({ ...f, code: f.code.trim().toUpperCase(), weeks: Number(f.weeks),
        manager: f.manager.trim() || user?.username });
      onClose?.();
      if (!f.manager.trim() || f.manager.trim() === user?.username) nav(`/project/${encodeURIComponent(p.code)}`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="新建项目" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="sb"><h2 className="h2">新建项目</h2><button className="btn sm" onClick={onClose}>取消</button></div>
      {products && products.length === 0 && <div className="sub err">没有可立项的服务产品（配置有问题时不能立项，请联系系统管理员）。</div>}
      <label className="sub">项目代号（只用客户代号，如 BANK-D，不写客户真实名称）
        <input className="inp mono" maxLength={15} value={f.code} onChange={set("code")} placeholder="BANK-D" />
      </label>
      <label className="sub">服务产品
        <select className="inp" value={f.product} onChange={(e) => setF({ ...f, product: e.target.value, modules: [] })}>
          {(products || []).map((p) => <option key={p.code} value={p.code}>{p.code} · {p.name}</option>)}
        </select>
      </label>
      {prod && prod.modules.length > 0 && (
        <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span>附加模块（可不选）</span>
          {prod.modules.map((m) => (
            <label key={m.code} className="row"><input type="checkbox" checked={f.modules.includes(m.code)} onChange={() => toggle(m.code)} />{m.name}</label>
          ))}
        </div>
      )}
      {prod && <div className="sub">交付件：{prod.deliverables.map((d) => `${d.code} ${d.title}`).join("、")}</div>}
      <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span>档位（必须选；拿不准时按严格档）</span>
        <label className="row"><input type="radio" name="tier" checked={f.tier === "standard"} onChange={() => setF({ ...f, tier: "standard" })} />标准档：客户资料可进入平台</label>
        <label className="row"><input type="radio" name="tier" checked={f.tier === "strict"} onChange={() => setF({ ...f, tier: "strict" })} />严格档：资料不出客户现场，只收客户书面同意后的去标识摘要卡</label>
      </div>
      <label className="sub">首位项目经理（用户名，留空为本人）
        <input className="inp" maxLength={64} value={f.manager} onChange={set("manager")} placeholder={user?.username} />
      </label>
      <div className="row">
        <label className="sub">行业<input className="inp" maxLength={32} value={f.industry} onChange={set("industry")} placeholder="银行" /></label>
        <label className="sub">计划周数<input className="inp" type="number" min={1} max={104} value={f.weeks} onChange={set("weeks")} /></label>
      </div>
      {error && <div className="sub err">{error}</div>}
      <div className="row"><button className="btnp" disabled={busy || !f.code.trim() || !f.product || !f.tier} onClick={submit}>立项</button>
        <span className="sub">立项后产品和模块不能再改；配置改版只影响之后立项的项目。</span></div>
    </section>
  );
}
