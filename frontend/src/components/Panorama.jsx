// 项目全景（四期 A25①）：列 = 产品阶段、行 = 泳道，数据由 GET /api/projects/{code}/panorama 按产品配置生成。
// A25②：数据由驾驶舱取好传入；点节点：drawer:* 交给驾驶舱开抽屉，页面路由直接跳转；
// focus = 节点 id 时（各页“⌖ 全景”）切到全部视图并闪烁高亮该节点。
// A29（《平台规划刷新设计》4.1）：只有客户、顾问、项目经理三行（打开“查看 Agent 运行情况”时多出“Agent 运行”一行）；
// AI 在写的交付件灰字“处理中 · 预计 hh:mm”，超过预计 2 倍显示“延迟”；连线交付件 → 交付件；
// 视图只留“要我做的 / 全部”，去掉回放和“自动完成”折叠行；已完成阶段折叠，点一下原位展开；
// 已签署交付件点开是只读面板（drawer:deliv:*，驾驶舱负责打开）。
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";

const COLOR = { ok: "var(--ok)", ai: "var(--tx3)", cur: "var(--cyan)", blk: "var(--crim)", todo: "var(--tx3)" };
const LABEL = { ok: "已完成", ai: "处理中", cur: "进行中", blk: "卡住", todo: "未开始" };
const VIEWS = [["mine", "要我做的"], ["all", "全部"]];
const WIDTH = { done: 96, cur: 250, next: 180, later: 180 };
const LANE_W = 76, HEAD = 30, NODE_H = 32, GAP = 6, PAD = 8;

// 要我做的 = 在我的泳道里、卡住或进行中的节点
export const isMine = (data, n) => data.my_lanes.includes(n.lane) && (n.status === "blk" || n.status === "cur");
const hhmm = (t) => new Date(t).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
// AI 在写的交付件：灰字“处理中 · 预计 hh:mm”，超过预计 2 倍“延迟”
export const aiNote = (n) => (n.status !== "ai" ? "" : n.late ? "延迟" : `处理中${n.eta ? ` · 预计 ${hhmm(n.eta)}` : ""}`);

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
  const [view, setView] = useState(focus ? "all" : "mine");
  const [opened, setOpened] = useState(() => new Set());   // 已完成阶段里点开（原位展开）的列
  const focusRef = useRef(null);
  const navigate = useNavigate();

  useEffect(() => { focusRef.current?.scrollIntoView?.({ behavior: "smooth", block: "center" }); }, [focus, data]);

  if (!data) return null;

  // 布局：已完成列折叠成每条泳道一个汇总块（点一下原位展开）；其余列逐个列节点
  const focusCol = focus ? data.nodes.find((n) => n.id === focus)?.col : null;
  const folded = (c) => c.status === "done" && !opened.has(c.id) && c.id !== focusCol;
  const cols = data.columns.map((c, i) => ({ ...c, i, fold: folded(c), w: folded(c) ? WIDTH.done : (WIDTH[c.status] || 180) }));
  for (const c of cols) if (c.status === "done" && !c.fold) c.w = WIDTH.cur;
  let x = LANE_W;
  for (const c of cols) { c.x = x; x += c.w; }
  const cell = (col, lane) => data.nodes.filter((n) => n.col === col.id && n.lane === lane.id);
  const rows = data.lanes.map((l) => Math.max(1, ...cols.map((c) => (c.fold ? 1 : cell(c, l).length))));
  const laneY = [];
  let y = HEAD;
  rows.forEach((n) => { laneY.push(y); y += PAD * 2 + n * (NODE_H + GAP) - GAP; });

  const pos = {};
  const boxes = [];
  for (const c of cols) {
    data.lanes.forEach((l, li) => {
      const items = cell(c, l);
      if (!items.length) return;
      if (c.fold) {
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

  const dim = (b) => view === "mine" && !b.folded && !isMine(data, b);
  const open = (b) => {
    if (b.folded) { setOpened((s) => new Set(s).add(b.col)); return; }
    if (!b.target) return;
    if (b.target.startsWith("drawer:")) onOpen(b.target);
    else if (b.type === "evidence") navigate(`/project/${encodeURIComponent(code)}/input`);   // 资料在“输入”页
    else navigate(b.target);
  };
  const cur = cols.find((c) => c.id === data.current);
  const lit = focus ? pos[focus] : null;

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
            if (!a || !b || a === b) return null;
            const x1 = a.x + a.w, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2;
            const mx = (x1 + x2) / 2;
            return <path key={`${e.from}>${e.to}`} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
              style={{ fill: "none", stroke: "var(--ln2)", opacity: dim(a) || dim(b) ? 0.2 : 0.8 }} />;
          })}
          {boxes.map((b) => (
            <g key={b.id} data-node={b.folded ? undefined : b.id} data-fold={b.folded ? `${b.col}:${b.lane}` : undefined}
              ref={b === lit ? focusRef : undefined} className={`pn pn-${b.status}${b === lit ? " flash" : ""}`}
              onClick={() => open(b)} style={{ cursor: "pointer", opacity: dim(b) ? 0.2 : 1 }}>
              <title>{b.folded ? `${b.title} · 点一下展开` : `${b.title}${b.subtitle ? ` · ${b.subtitle}` : ""}${b.status === "ai" ? "" : ` · ${LABEL[b.status]}`}`}</title>
              <rect x={b.x} y={b.y} width={b.w} height={NODE_H} rx={5} style={{ fill: "var(--s3)", stroke: COLOR[b.status] }} />
              <circle cx={b.x + 10} cy={b.y + NODE_H / 2} r={4} style={{ fill: COLOR[b.status] }} />
              <text x={b.x + 20} y={b.y + 20} style={{ fill: b.status === "todo" || b.status === "ai" ? "var(--tx3)" : "var(--tx)" }}>
                {(() => {
                  const note = aiNote(b);
                  const room = Math.max(4, Math.floor(b.w / 13) - (note ? note.length : 0));
                  const t = b.title.length > room ? `${b.title.slice(0, room)}…` : b.title;
                  return <>{t}{note && <tspan style={{ fill: b.late ? "var(--amber)" : "var(--tx3)" }}> {note}</tspan>}</>;
                })()}
              </text>
            </g>
          ))}
        </svg>
      </div>
      <div className="sub" style={{ display: "flex", gap: 12 }}>
        {Object.entries(LABEL).map(([k, t]) => <span key={k}><span style={{ color: COLOR[k] }}>●</span> {t}</span>)}
      </div>
    </section>
  );
}
