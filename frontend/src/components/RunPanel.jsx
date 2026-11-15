import { useEffect, useState } from "react";
import { api, RUN_ACTIVE, RUN_STATUS } from "../api.js";

// 交付链步骤来自 GET /api/projects/{code}/chain（项目立项时的配置快照，二期 A5）

function stepState(run, key) {
  const rec = run.steps?.find((s) => s.step === key);
  if (rec?.status === "done") return { label: "完成", cls: "c-g", rec };
  if (rec?.status === "cancelled") return { label: "已取消", cls: "c-n", rec };
  if (run.next_step === key) {
    if (run.status === "running") return { label: "运行中", cls: "c-c" };
    if (run.status === "queued") return { label: "排队中", cls: "c-c" };
    if (run.status === "waiting") return { label: "等你确认", cls: "c-a" };
    if (run.status === "failed") return { label: "失败", cls: "c-r" };
  }
  return { label: "未开始", cls: "c-n" };
}

const fmt = (t) => (t ? t.replace("T", " ").slice(5, 16) : "");

// 交付链运行：发起、进度、取消；运行中每 3 秒刷新，状态变化时通知驾驶舱刷新（确认卡、交付件）
export default function RunPanel({ code, strict, onChange }) {
  const [run, setRun] = useState(undefined);   // undefined 加载中，null 没有运行
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [steps, setSteps] = useState([]);

  useEffect(() => { api.chain(code).then(setSteps).catch(setError); }, [code]);

  useEffect(() => {
    let alive = true;
    let timer;
    let last = "";
    const tick = () => api.runs(code)
      .then((rs) => (rs.length ? api.run(code, rs[0].id) : null))
      .then((r) => {
        if (!alive) return;
        setRun(r);
        const sig = r ? `${r.id}:${r.status}:${r.next_step}` : "";
        if (last && sig !== last) onChange();
        last = sig;
        if (r && RUN_ACTIVE.has(r.status)) timer = setTimeout(tick, 3000);
      })
      .catch((e) => alive && setError(e));
    tick();
    return () => { alive = false; clearTimeout(timer); };
  }, [code, busy]);   // onChange 每次渲染都是新函数，不放进依赖

  const act = (fn) => {
    setBusy(true);
    setError(null);
    fn().catch(setError).finally(() => setBusy(false));
  };

  const active = run && RUN_ACTIVE.has(run.status);
  const st = run ? RUN_STATUS[run.status] || RUN_STATUS.queued : null;
  return (
    <section className="pnl pad" aria-label="交付链">
      <div className="sb">
        <h2 className="h2">交付链 · Agent 组</h2>
        {st && <span className={`chip ${st.cls}`}>#{run.id} {st.label}</span>}
      </div>
      <>
          <ol className="run-steps">
            {steps.map((s) => {
              const ss = run ? stepState(run, s.key) : { label: "未开始", cls: "c-n" };
              return (
                <li key={s.key}>
                  <div className="sb">
                    <span><span className="mono sub">第 {s.number} 步</span> {s.name}</span>
                    <span className={`chip ${ss.cls}`}>{ss.label}</span>
                  </div>
                  <div className="sub">
                    {s.agent_name}{s.output && ` → 写 ${s.output}`}
                    {ss.rec && ` · ${fmt(ss.rec.finished_at)} · ${ss.rec.model || "—"} · ${ss.rec.prompt_tokens + ss.rec.completion_tokens} tokens`}
                  </div>
                </li>
              );
            })}
          </ol>
          {run?.status === "waiting" && (
            <div className="sub" style={{ color: "var(--amber)" }}>
              等你处理 {run.waiting_cards.length} 张确认卡（见“待你确认”），处理完自动继续。
            </div>
          )}
          {run?.status === "failed" && run.error && <div className="err sub">失败原因：{run.error}</div>}
          {run && <div className="sub">发起：{run.created_by} · {fmt(run.created_at)}{run.finished_at && ` · 结束 ${fmt(run.finished_at)}`}</div>}
          {error && <div className="err sub">{error.message}</div>}
          <div className="row" style={{ justifyContent: "flex-end" }}>
            {active ? (
              <button className="btn sm" disabled={busy} onClick={() => window.confirm("取消本次运行？已写入的章节保留。")
                && act(() => api.cancelRun(code, run.id))}>取消运行</button>
            ) : (
              <button className="btnp" disabled={busy || run === undefined} onClick={() => act(() => api.startRun(code))}>
                {run ? "重新运行交付链" : "运行交付链"}
              </button>
            )}
          </div>
          <div className="sub">{strict
            ? "读摘要卡（S-xx）起草，全文只用占位符。需要有效的客户同意和摘要卡。"
            : "读项目证据，按上面的步骤依次起草；方案取舍、证据不足时发确认卡，等你处理后继续。"}产出是草稿，需顾问审阅和质量评审后签署。</div>
      </>
    </section>
  );
}
