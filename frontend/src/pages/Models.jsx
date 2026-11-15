import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { TopBar } from "../components/common.jsx";

// 模型配置状态（系统管理员，只读）：配置在部署机 .env 里改、重启后端生效；页面不显示密钥。
function Status({ value }) {
  if (!value) return <span className="sub">—</span>;
  const cls = value === "ok" ? "c-g" : value.startsWith("down") ? "c-r" : "c-a";
  const label = value === "ok" ? "正常" : value === "not enabled" ? "未启用" : value.startsWith("demo") ? "演示模式" : "异常";
  return <span className="row" style={{ gap: 6 }}><span className={`chip ${cls}`}>{label}</span>{value !== "ok" && <span className="sub">{value}</span>}</span>;
}

function yes(v) {
  return v ? "是" : "否";
}

function Service({ title, data, rows }) {
  return (
    <section className="pnl pad" aria-label={title}>
      <div className="sb"><h2 className="h2">{title}</h2><Status value={data.status} /></div>
      <table className="tbl">
        <tbody>
          <tr><th>服务地址</th><td className="mono">{data.endpoint || <span className="sub">未配置</span>}</td></tr>
          <tr><th>密钥</th><td>{data.key_configured ? "已配置" : "未配置"}</td></tr>
          {rows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v}</td></tr>)}
        </tbody>
      </table>
    </section>
  );
}

export default function Models() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      setData(await api.adminModels());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  return (
    <>
      <TopBar cap="系统管理 · 只读" title="模型配置状态">
        <button type="button" className="btn" disabled={busy} onClick={load}>刷新</button>
      </TopBar>
      <div className="sub">配置在部署机的 .env 里修改，重启后端生效；页面不显示密钥。连通状态有约 30 秒缓存；重排最近一次调用失败时直接显示异常。</div>
      {error && <div className="err" role="alert">{error}</div>}
      {!data && !error && <div className="sub">加载中……</div>}
      {data?.llm.external && (
        <div className="note-amber" role="note">当前接的是外部测试模型：只允许处理虚拟数据，严格档项目会被拒绝。</div>
      )}
      {data && (
        <>
          <Service title="大模型" data={data.llm} rows={[
            ["推理档模型", <span className="mono">{data.llm.models.reasoning}</span>],
            ["标准档模型", <span className="mono">{data.llm.models.standard}</span>],
            ["轻量档模型", <span className="mono">{data.llm.models.light}</span>],
            ["外部模型", yes(data.llm.external)],
            ["假名化", yes(data.llm.pseudonymize || data.llm.external)],
            ["超时 / 重试", `${data.llm.timeout_seconds} 秒 / ${data.llm.max_retries} 次`],
          ]} />
          <Service title="向量服务" data={data.embedding} rows={[
            ["模型", <span className="mono">{data.embedding.model}</span>],
            ["向量维度", data.embedding.dim],
          ]} />
          <Service title="重排服务" data={data.rerank} rows={[
            ["模型", <span className="mono">{data.rerank.model}</span>],
            ["外部重排", yes(data.rerank.external)],
            ["候选数 / 每段字数", `${data.rerank.candidates} / ${data.rerank.max_chars}`],
            ["依据查询门槛 / 检索门槛", `${data.rerank.basis_min} / ${data.rerank.search_min}`],
          ]} />
        </>
      )}
    </>
  );
}
