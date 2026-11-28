import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api.js";
import { useAuth } from "../auth.jsx";

// 立项行业下拉（负责人 11-16）；产品配置 industry_basis 的键对上任一部分即加权（app/agents/tools.py），新增行业在这里加
const INDUSTRIES = ["银行/金融", "电力", "制造", "政府", "其他"];
// 组织类型（可不填），取值同 app/project/configs.py ORG_TYPES；经验和评测按组织类型分层
const ORG_TYPES = [["central_soe", "央企"], ["local_soe", "地方国企"], ["private", "民营企业"], ["foreign", "外资企业"],
  ["government", "政府机关"], ["institution", "事业单位"], ["other", "其他"]];

// 先行启动的立项依据（四期 A32，docs/交付链输入输出方案.md 4.7）
const BASIS_EARLY = [["award", "中标通知书"], ["letter", "委托函"], ["proposal", "建议书"], ["presales", "售前沟通纪要"],
  ["email", "客户确认范围的邮件"], ["contract", "合同"]];

// 网页立项（二期 A3）：项目负责人选产品、勾模块、定档、指定首位项目经理（默认本人）。规则见 app/project/configs.py
export default function NewProject({ onClose }) {
  const { user } = useAuth();
  const nav = useNavigate();
  const [products, setProducts] = useState(null);
  const [f, setF] = useState({ code: "", product: "", modules: [], tier: "", manager: "", industry: "", org_type: "", weeks: 12, early_start: false });
  const [basis, setBasis] = useState({ kind: "contract", file: null });   // 四期 A32 第 1 步：上传依据
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.catalogProducts().then((ps) => {
      setProducts(ps);
      const first = ps.find((p) => p.ready);
      if (first) setF((x) => ({ ...x, product: first.code }));
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
        manager: f.manager.trim() || user?.username, intake: true });
      const self = !f.manager.trim() || f.manager.trim() === user?.username;
      // 新建项目三步（四期 A32）：上传依据 → 核对要点卡 → 授权与成员；后两步在“输入”页的合同要点卡里做
      if (self && basis.file && f.tier === "standard") {
        try { await api.uploadBasis(p.code, basis.file, basis.kind); } catch { /* 上传失败在输入页重传或手填 */ }
      }
      onClose?.();
      if (self) nav(`/project/${encodeURIComponent(p.code)}/input?new=1`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="pnl pad" aria-label="新建项目" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div className="sb"><h2 className="h2">新建项目</h2><button className="btn sm" onClick={onClose}>取消</button></div>
      {products && !products.some((p) => p.ready) && <div className="sub err">没有可立项的服务产品（配置有问题时不能立项，请联系系统管理员）。</div>}
      <label className="sub">项目代号（只用客户代号，如 BANK-D，不写客户真实名称）
        <input className="inp mono" maxLength={15} value={f.code} onChange={set("code")} placeholder="BANK-D" />
      </label>
      <label className="sub">服务产品
        <select className="inp" value={f.product} onChange={(e) => setF({ ...f, product: e.target.value, modules: [] })}>
          {(products || []).map((p) => <option key={p.code} value={p.code} disabled={!p.ready}>
            {p.code} · {p.name}{p.ready ? (p.expert_review === "done" ? "" : "（公开方法版，待专家复核）") : "（筹备中）"}</option>)}
        </select>
      </label>
      {prod && prod.modules.length > 0 && (
        <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span>附加模块（可不选）</span>
          {prod.modules.map((m) => (
            <label key={m.code} className="row"><input type="checkbox" checked={f.modules.includes(m.code)} onChange={() => toggle(m.code)} />{m.name}{m.expert_review === "done" ? "" : "（待专家复核）"}</label>
          ))}
        </div>
      )}
      {prod && <div className="sub">交付件：{prod.deliverables.map((d) => `${d.code} ${d.title}`).join("、")}</div>}
      <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span>档位（必须选；拿不准时按严格档）</span>
        <label className="row"><input type="radio" name="tier" checked={f.tier === "standard"} onChange={() => setF({ ...f, tier: "standard" })} />标准档：客户资料可进入平台</label>
        <label className="row"><input type="radio" name="tier" checked={f.tier === "strict"} onChange={() => setF({ ...f, tier: "strict" })} />严格档：资料不出客户现场，只收客户书面同意后的去标识摘要卡</label>
      </div>
      <div className="sub" style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <span>立项依据（第 1 步，共 3 步）</span>
        <label className="row"><input type="checkbox" checked={f.early_start}
          onChange={(e) => { setF({ ...f, early_start: e.target.checked }); setBasis({ ...basis, kind: e.target.checked ? "award" : "contract" }); }} />
          合同未签，先行启动（须部门负责人确认；授权签署前不收客户资料）</label>
        {f.tier === "strict" ? <span>严格档不上传合同，立项后手填合同要点卡。</span> : (
          <div className="row">
            <select className="inp" aria-label="依据类型" value={basis.kind} onChange={(e) => setBasis({ ...basis, kind: e.target.value })}>
              {(f.early_start ? BASIS_EARLY : [["contract", "合同"]]).map(([k, n]) => <option key={k} value={k}>{n}</option>)}
            </select>
            <input type="file" aria-label="依据文件" accept=".docx,.pdf,.md,.txt" onChange={(e) => setBasis({ ...basis, file: e.target.files[0] || null })} />
          </div>
        )}
        <span>合同原文抽成要点卡后即删除，要点卡全程保留；没有文件可立项后手填。</span>
      </div>
      <label className="sub">首位项目经理（用户名，留空为本人）
        <input className="inp" maxLength={64} value={f.manager} onChange={set("manager")} placeholder={user?.username} />
      </label>
      <div className="row">
        <label className="sub">行业
          <select className="inp" value={f.industry} onChange={set("industry")}>
            <option value="">请选择</option>
            {INDUSTRIES.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
        <label className="sub">组织类型
          <select className="inp" value={f.org_type} onChange={set("org_type")}>
            <option value="">不填</option>
            {ORG_TYPES.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="sub">计划周数<input className="inp" type="number" min={1} max={104} value={f.weeks} onChange={set("weeks")} /></label>
      </div>
      {error && <div className="sub err">{error}</div>}
      <div className="row"><button className="btnp" disabled={busy || !f.code.trim() || !prod?.ready || !f.tier || !f.industry} onClick={submit}>立项</button>
        <span className="sub">立项后产品和模块不能再改；配置改版只影响之后立项的项目。</span></div>
    </section>
  );
}
