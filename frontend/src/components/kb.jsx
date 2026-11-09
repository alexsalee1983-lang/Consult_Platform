import { useEffect, useState } from "react";
import { api } from "../api.js";

// 投料状态
export const UPLOAD_STATUS = {
  received: { label: "待解析", cls: "c-n" },
  parsed: { label: "去标识中", cls: "c-c" },
  deid_pending: { label: "去标识待确认", cls: "c-a" },
  deid_passed: { label: "去标识通过", cls: "c-g" },
  deid_rejected: { label: "已退回", cls: "c-r" },
  failed: { label: "解析失败", cls: "c-r" },
};

// 版本状态
export const VERSION_STATUS = {
  draft: { label: "草稿", cls: "c-n" },
  in_review: { label: "待审", cls: "c-a" },
  rejected: { label: "已退回", cls: "c-r" },
  published: { label: "已发布", cls: "c-g" },
  superseded: { label: "已替代", cls: "c-n" },
  retired: { label: "已下线", cls: "c-n" },
};

export const DEID_CATEGORIES = ["客户名称", "人名", "系统名称", "网络标识", "联系方式", "编号", "金额", "其他"];

export function Chip({ map, value }) {
  const s = map[value] || { label: value, cls: "c-n" };
  return <span className={`chip ${s.cls}`}>{s.label}</span>;
}

export function fmtDate(at) {
  return at ? new Date(at).toLocaleString("zh-CN", { hour12: false, month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
}

export function fmtSize(n) {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

// 机密级拦截的说明（与对话页一致：只显示打码摘录）
export function BlockedNotice({ detail }) {
  return (
    <div className="blocked" role="alert">
      <div style={{ fontWeight: 700, color: "var(--crim)" }}>已拦截：{detail.summary}</div>
      <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
        {(detail.findings || []).map((f, i) => <li key={i}><span className="mono">{f.excerpt}</span>　{f.label}</li>)}
      </ul>
      <div className="sub" style={{ marginTop: 4 }}>文件没有保存。请删掉机密级内容（口令、密钥、IP、个人信息等）后重新上传。</div>
    </div>
  );
}

// 元数据取值表（公共规范第 7 节），整个会话只取一次
let metaCache = null;
export function useKbMeta() {
  const [meta, setMeta] = useState(metaCache);
  useEffect(() => {
    if (!metaCache) api.kbMeta().then((m) => { metaCache = m; setMeta(m); }).catch(() => {});
  }, []);
  return meta;
}

export function Select({ label, value, onChange, options, required }) {
  return (
    <label className="field">
      <span>{label}{required ? " *" : ""}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

export const EMPTY_META = {
  layer: "L2", domain: "OPS", product: "SOC", doc_type: "MTD", stage: "设计", capabilities: ["运营"],
  industries: ["通用"], classification: "internal", visible_roles: ["consultant", "admin"],
};

// 条目元数据表单（层级、目录、产品、类型、阶段、能力域、行业、密级）
export function MetaForm({ value, onChange }) {
  const meta = useKbMeta();
  if (!meta) return <div className="sub">加载取值表……</div>;
  const set = (k) => (v) => onChange({ ...value, [k]: v });
  const opts = (o) => Object.entries(o).map(([k, v]) => [k, `${k} ${v}`]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="form-grid">
        <Select label="层级" value={value.layer} onChange={set("layer")} options={opts(meta.layers)} required />
        <Select label="一级目录" value={value.domain} onChange={set("domain")} options={opts(meta.domains)} required />
        <Select label="服务产品" value={value.product} onChange={set("product")} options={opts(meta.products)} required />
        <Select label="交付物类型" value={value.doc_type} onChange={set("doc_type")} options={opts(meta.doc_types)} required />
        <Select label="交付阶段" value={value.stage} onChange={set("stage")} options={meta.stages.map((s) => [s, s])} required />
        <Select label="密级" value={value.classification} onChange={set("classification")}
          options={Object.entries(meta.classifications)} />
      </div>
      <fieldset className="roles" aria-label="能力域标签">
        <span className="sub" style={{ marginRight: 4 }}>能力域 *</span>
        {meta.capabilities.map((c) => (
          <label key={c} className="chk">
            <input type="checkbox" checked={value.capabilities.includes(c)}
              onChange={() => set("capabilities")(value.capabilities.includes(c)
                ? value.capabilities.filter((x) => x !== c) : [...value.capabilities, c])} />{c}
          </label>
        ))}
      </fieldset>
      <label className="field">
        <span>适用行业（逗号分隔；通用的填“通用”）</span>
        <input value={value.industries.join("，")}
          onChange={(e) => set("industries")(e.target.value.split(/[,，、]/).map((x) => x.trim()).filter(Boolean))} />
      </label>
    </div>
  );
}
