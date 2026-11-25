// 项目全景（四期 A25①）：列 = 产品阶段、行 = 泳道，数据由 GET /api/projects/{code}/panorama 按产品配置生成。
// 当前阶段展开、已完成阶段折叠、未来阶段只列要做的事；视图：全部 / 只看我的 / 只看例外 / 回放。
// A25②：数据由驾驶舱取好传入；默认“只看例外”，已自动完成的折叠成一行；点节点：drawer:* 交给驾驶舱开抽屉，
// 页面路由直接跳转；focus = 节点 id 时（各页“⌖ 全景”）切到全部视图并闪烁高亮该节点。
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

const COLOR = { ok: "var(--ok)", ai: "var(--vio)", cur: "var(--cyan)", blk: "var(--crim)", todo: "var(--tx3)" };
const LABEL = { ok: "已完成", ai: "AI 在做", cur: "进行中", blk: "卡住", todo: "未开始" };
const VIEWS = [["all", "全部"], ["mine", "只看我的"], ["exc", "只看例外"], ["replay", "回放"]];
const WIDTH = { done: 96, cur: 250, next: 180, later: 180 };
const LANE_W = 76, HEAD = 30, NODE_H = 32, GAP = 6, PAD = 8;

// 例外 = 卡住或进行中、需要人处理的节点；AI 已自动办完的（AI 泳道 ok）计入“自动完成”折叠行
export const isException = (n) => n.status === "blk" || n.status === "cur";
export const autoDone = (data) => data.nodes.filter((n) => n.lane === "ai" && n.status === "ok").length;

// 影响面 = 沿连线下游能到达的节点数（卡住 D3 的卡也卡住第 4 步、D4……）
export function impact(data, id) {
  const seen = new Set();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop();
    for (const e of data.edges) if (e.from === cur && !seen.has(e.to)) { seen.add(e.to); stack.push(e.to); }
  }
  return seen.size;
}

// 待你处理：未处理的确认卡 + 待签署的交付件，按影响面从大到小；同影响面确认卡在前
export function todos(data) {
  const rows = data.nodes
    .filter((n) => n.type === "card" || (n.type === "signoff" && n.status === "cur"))
    .map((n) => ({ ...n, impact: impact(data, n.id) }));
  return rows.sort((a, b) => b.impact - a.impact || (a.type === "card" ? 0 : 1) - (b.type === "card" ? 0 : 1));
}

export default function Panorama({ code, data, focus, onOpen }) {
  const [view, setView] = useState(focus ? "all" : "exc");
  const [step, setStep] = useState(-1);          // 回放：已显示到第几列
  const timer = useRef(null);
  const focusRef = useRef(null);
  const navigate = useNavigate();

  useEffect(() => { focusRef.current?.scrollIntoView?.({ behavior: "smooth", block: "center" }); }, [focus, data]);
  useEffect(() => {
    clearInterval(timer.current);
    if (view !== "replay" || !data) return undefined;
    setStep(0);
    timer.current = setInterval(() => setStep((s) => (s + 1 >= data.columns.length ? s : s + 1)), 700);
    return () => clearInterval(timer.current);
  }, [view, data]);

  if (!data) return null;

  // 布局：已完成列折叠成每条泳道一个汇总块；其余列逐个列节点
  const cols = data.columns.map((c, i) => ({ ...c, i, w: WIDTH[c.status] || 180 }));
  let x = LANE_W;
  for (const c of cols) { c.x = x; x += c.w; }
  const cell = (col, lane) => data.nodes.filter((n) => n.col === col.id && n.lane === lane.id);
  const rows = data.lanes.map((l) => Math.max(1, ...cols.map((c) => (c.status === "done" ? 1 : cell(c, l).length))));
  const laneY = [];
  let y = HEAD;
  rows.forEach((n) => { laneY.push(y); y += PAD * 2 + n * (NODE_H + GAP) - GAP; });

  const pos = {};
  const boxes = [];
  for (const c of cols) {
    data.lanes.forEach((l, li) => {
      const items = cell(c, l);
      if (!items.length) return;
      if (c.status === "done") {
        const blk = items.some((n) => n.status === "blk");
        const b = { id: `sum:${c.id}:${l.id}`, x: c.x + 6, y: laneY[li] + PAD, w: c.w - 12, status: blk ? "blk" : "ok",
          title: `${items.filter((n) => n.status === "ok").length}/${items.length} 完成`, subtitle: "", lane: l.id, col: c.id, folded: true };
        boxes.push(b);
        items.forEach((n) => { pos[n.id] = b; });
        return;
      }
      items.forEach((n, k) => {
        const b = { ...n, x: c.x + 6, y: laneY[li] + PAD + k * (NODE_H + GAP), w: c.w - 12 };
        boxes.push(b);
        pos[n.id] = b;
      });
    });
  }

  const visible = (b) => view !== "replay" || cols.find((c) => c.id === b.col).i <= step;
  const dim = (b) => (view === "mine" && !data.my_lanes.includes(b.lane)) || (view === "exc" && !isException(b));
  const open = (b) => {
    if (b.folded || !b.target) return;
    if (b.target.startsWith("drawer:")) onOpen(b.target);
    else if (b.type === "evidence") navigate(`/project/${encodeURIComponent(code)}/input`);   // 资料在“输入”页
    else navigate(b.target);
  };
  const cur = cols.find((c) => c.id === data.current);
  const lit = focus ? pos[focus] : null;
  const done = autoDone(data);

  return (
    <section className="pnl pad" aria-label="项目全景">
      <div className="sb">
        <h2 className="h2">项目全景 · {data.product_name}</h2>
        <div style={{ display: "flex", gap: 4 }}>
          {VIEWS.map(([k, t]) => <button key={k} type="button" className="btn sm" aria-pressed={view === k}
            style={view === k ? { borderColor: "var(--amber)", color: "var(--amber)" } : undefined} onClick={() => setView(k)}>{t}</button>)}
        </div>
      </div>
      <div style={{ overflowX: "auto" }}>
        <svg width={x + 4} height={y + 4} role="img" aria-label="全景泳道图" style={{ display: "block", fontSize: 12 }}>
          {data.lanes.map((l, li) => (
            <g key={l.id}>
              <rect x={0} y={laneY[li]} width={x} height={(laneY[li + 1] ?? y) - laneY[li]} style={{ fill: li % 2 ? "var(--s1)" : "var(--s2)" }} />
              <text x={8} y={laneY[li] + PAD + 20} style={{ fill: "var(--tx2)" }}>{l.name}</text>
            </g>
          ))}
          {cols.map((c) => (
            <g key={c.id}>
              <line x1={c.x} y1={0} x2={c.x} y2={y} style={{ stroke: "var(--ln)" }} />
              <text x={c.x + 6} y={20} style={{ fill: c.status === "cur" ? "var(--amber)" : c.status === "done" ? "var(--tx3)" : "var(--tx2)", fontWeight: c.status === "cur" ? 600 : 400 }}>
                {c.name}{c.status === "done" ? " ✓" : ""}
              </text>
            </g>
          ))}
          {cur && <g><line x1={cur.x + 2} y1={HEAD - 4} x2={cur.x + 2} y2={y} style={{ stroke: "var(--amber)", strokeDasharray: "4 3" }} />
            <text x={cur.x + cur.w - 34} y={20} style={{ fill: "var(--amber)" }}>今天</text></g>}
          {data.edges.map((e) => {
            const a = pos[e.from], b = pos[e.to];
            if (!a || !b || a === b || !visible(a) || !visible(b)) return null;
            const x1 = a.x + a.w, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
            const mx = (x1 + x2) / 2;
            return <path key={`${e.from}>${e.to}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
              style={{ fill: "none", stroke: "var(--ln2)", opacity: dim(a) || dim(b) ? 0.2 : 0.8 }} />;
          })}
          {boxes.filter(visible).map((b) => (
            <g key={b.id} data-node={b.id} ref={b === lit ? focusRef : undefined} className={b === lit ? "flash" : undefined}
              onClick={() => open(b)} style={{ cursor: b.folded ? "default" : "pointer", opacity: dim(b) ? 0.2 : 1 }}>
              <title>{`${b.title}${b.subtitle ? ` · ${b.subtitle}` : ""} · ${LABEL[b.status]}`}</title>
              <rect x={b.x} y={b.y} width={b.w} height={NODE_H} rx={5} style={{ fill: "var(--s3)", stroke: COLOR[b.status] }} />
              <circle cx={b.x + 10} cy={b.y + NODE_H / 2} r={4} style={{ fill: COLOR[b.status] }} />
              <text x={b.x + 20} y={b.y + 20} style={{ fill: b.status === "todo" ? "var(--tx3)" : "var(--tx)" }}>
                {b.title.length > b.w / 13 ? `${b.title.slice(0, Math.floor(b.w / 13))}…` : b.title}
              </text>
            </g>
          ))}
        </svg>
      </div>
      <div className="sub" style={{ display: "flex", gap: 12 }}>
        {Object.entries(LABEL).map(([k, t]) => <span key={k}><span style={{ color: COLOR[k] }}>●</span> {t}</span>)}
      </div>
      {view === "exc" && done > 0 && (
        <button type="button" className="btn sm" style={{ alignSelf: "flex-start" }} onClick={() => setView("all")}
          title="暂按交付链已自动完成的步骤计；本周口径待 F5 自动化账本接入">
          <span style={{ color: "var(--ok)" }}>✓</span> 本周自动完成 {done} 项 · 展开
        </button>
      )}
    </section>
  );
}
