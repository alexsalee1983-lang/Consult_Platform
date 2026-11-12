import { useEffect, useState } from "react";
import { api, RUN_ACTIVE, RUN_STATUS } from "../api.js";

// 标准档交付链 3→4→7（与 backend/app/project/runs.py 的 STEPS 一致）
const STEPS = [
  { key: "facts", n: 3, name: "整理事实清单", agent: "调研访谈 Agent", out: "D3" },
  { key: "gap", n: 4, name: "差距分析", agent: "依据与差距分析 Agent", out: "" },
  { key: "draft", n: 7, name: "现状评估报告初稿", agent: "报告与汇报 Agent", out: "D4" },
];

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
      {strict ? (
        <div className="sub">严格档：资料不出客户现场，交付链不在平台上运行。</div>
      ) : (
        <>
          <ol className="run-steps">
            {STEPS.map((s) => {
              const ss = run ? stepState(run, s.key) : { label: "未开始", cls: "c-n" };
              return (
                <li key={s.key}>
                  <div className="sb">
                    <span><span className="mono sub">第 {s.n} 步</span> {s.name}</span>
                    <span className={`chip ${ss.cls}`}>{ss.label}</span>
                  </div>
                  <div className="sub">
                    {s.agent}{s.out && ` → 写 ${s.out}`}
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
          <div className="sub">读项目证据写 D3 事实清单 → 差距分析 → 写 D4 现状评估初稿。产出是草稿，需顾问审阅和质量评审后签署。</div>
        </>
      )}
    </section>
  );
}
