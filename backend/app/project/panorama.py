"""项目全景（四期 A25①，docs/驾驶舱改版方案.md 第三节；四期 A29 精简，《平台规划刷新设计》4.1）：
按项目配置快照 + 项目现状生成泳道全景，只读。

- 列 = 阶段：快照里交付件的 stage，按首次出现的顺序（SOC、NSG 五段；IRE、DSM、ISO 多“落地辅导”）。
  阶段状态：交付件全部签署为 done；第一个没完成的为 cur，其后一个 next，再往后 later。
- 行 = 泳道：客户 / 顾问 / 项目经理（A29 去掉 AI·平台 泳道和步骤节点，AI 的工作只体现在交付件上）。
  顾问：每份交付件的撰写与审核、未处理的确认卡（证据不足、知识不足显示为“补资料”卡，写明影响几项）。
  项目经理：每份交付件的签署。客户：提供资料、对外交付件的接收。
- AI 在写某份交付件（交付链运行中，当前步骤的产出；只存运行记录的步骤落在下游交付件上）：节点 status=ai，
  灰字“处理中”，带 eta；超过预计用时 2 倍 late=True、灰字“延迟”，并在“我的任务”给项目经理推卡（run_delay）。
  预计用时 = 该步骤最近完成记录的平均用时，没有记录按 DEFAULT_STEP_MINUTES。
- 连线：交付件 → 下游交付件（按交付链输入，run:步骤 的间接引用解析到交付件），交付件 → 签署 → 客户接收。
- 已签署的交付件、签署、接收节点点开是只读面板（drawer:deliv:{code}）。
- runs=True（页面右上角“查看 Agent 运行情况”打开时）多出一行“Agent 运行”，只给每一步的状态。
- 节点状态：ok 已完成 / ai AI 在做 / cur 进行中 / blk 卡住 / todo 未开始。

只用配置里的标题和确认卡标题（项目成员本来就能看到），不读证据、章节正文；不写审计（只读接口，同项目详情）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.db import utcnow
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import AgentRun, AgentRunStep, Confirmation, Deliverable, Evidence, Project, User
from . import configs

LANES = [
    {"id": "client", "name": "客户"},
    {"id": "consultant", "name": "顾问"},
    {"id": "manager", "name": "项目经理"},
]
AGENT_LANE = {"id": "agent", "name": "Agent 运行"}
ACTIVE_RUN = {"queued", "running", "waiting"}   # 最近一次运行在做或停下等补证
WORKING_RUN = {"queued", "running"}             # AI 在做（不含停下等人）
DELIV = {"signed": "ok", "review": "ok", "drafting": "cur"}   # 交付件状态 → 顾问节点（缺省 todo）
SIGN = {"signed": "ok", "review": "cur"}                       # 交付件状态 → 签署节点（缺省 todo）
BU_ZI_LIAO = {"证据不足", "知识不足"}                            # AI 停下等人补资料的卡
DEFAULT_STEP_MINUTES = 10
LATE_FACTOR = 2


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def step_minutes(db: Session, step: str) -> float:
    """某步骤的预计用时（分钟）：最近 20 条完成记录的平均用时，没有记录按默认值。"""
    rows = db.execute(select(AgentRunStep.started_at, AgentRunStep.finished_at)
                      .where(AgentRunStep.step == step, AgentRunStep.status == "done",
                             AgentRunStep.finished_at.is_not(None))
                      .order_by(AgentRunStep.id.desc()).limit(20)).all()
    spans = [(_aware(f) - _aware(s)).total_seconds() / 60 for s, f in rows if f and s]
    spans = [x for x in spans if x > 0]
    return max(sum(spans) / len(spans), 1.0) if spans else float(DEFAULT_STEP_MINUTES)


def run_eta(db: Session, run: AgentRun, step: str) -> tuple[datetime, bool]:
    """正在跑的一步：(预计完成时间, 是否已超过预计用时 2 倍)。起点 = 运行最近一次推进的时间。"""
    since = _aware(run.updated_at or run.created_at)
    minutes = step_minutes(db, step)
    return since + timedelta(minutes=minutes), utcnow() - since > timedelta(minutes=minutes * LATE_FACTOR)


def latest_run(db: Session, project_id: int) -> AgentRun | None:
    return db.scalar(select(AgentRun).where(AgentRun.project_id == project_id).order_by(AgentRun.id.desc()).limit(1))


def resolve_targets(chain: list[dict], stage_of: dict[str, str]) -> dict[str, list[str]]:
    """每个步骤落在哪些交付件上：有产出的就是产出；只存运行记录的，取引用它（run:key）的下游步骤的交付件。"""
    out: dict[str, list[str]] = {s["key"]: [s["output"]] for s in chain if s.get("output") in stage_of}
    for _ in range(len(chain)):          # 引用可能是链式的（gap → design → policy），迭代到稳定
        for s in chain:
            if s["key"] in out:
                continue
            users = [d for t in chain if f"run:{s['key']}" in t.get("inputs", []) for d in out.get(t["key"], [])]
            if users:
                out[s["key"]] = list(dict.fromkeys(users))
    return out


def _upstream(by_key: dict[str, dict], stage_of: dict[str, str], step: dict, seen: set[str]) -> list[str]:
    """步骤的上游交付件：直接输入的交付件，加上 run:步骤 引用那一步的产出（没有产出再往上找）。"""
    ups: list[str] = []
    for i in step.get("inputs", []):
        if i in stage_of:
            ups.append(i)
        elif i.startswith("run:") and i[4:] in by_key and i[4:] not in seen:
            seen.add(i[4:])
            ref = by_key[i[4:]]
            ups += [ref["output"]] if ref.get("output") in stage_of else _upstream(by_key, stage_of, ref, seen)
    return list(dict.fromkeys(ups))


def _downstream(edges: list[dict[str, str]], nid: str) -> int:
    """沿连线下游能到达的交付件数（补资料卡、确认卡“影响几项”）。"""
    seen: set[str] = set()
    stack = [nid]
    while stack:
        cur = stack.pop()
        for e in edges:
            if e["from"] == cur and e["to"] not in seen:
                seen.add(e["to"])
                stack.append(e["to"])
    return sum(x.startswith("deliv:") for x in seen)


def auto_done(db: Session, project_ids: set[int]) -> int:
    """本周（近 7 天）AI 自动办完的步骤数，只给计数。"""
    if not project_ids:
        return 0
    since = utcnow() - timedelta(days=7)
    return db.scalar(select(func.count()).select_from(AgentRunStep).join(AgentRun, AgentRun.id == AgentRunStep.run_id)
                     .where(AgentRun.project_id.in_(project_ids), AgentRunStep.status == "done",
                            AgentRunStep.finished_at >= since)) or 0


def build(db: Session, project: Project, user: User, runs: bool = False) -> dict[str, Any]:
    """生成全景数据。配置有问题抛 config.CatalogError。"""
    snap = configs.snapshot(db, project)
    code = project.code
    dconf = snap["deliverables"]
    order = list(dict.fromkeys(d["stage"] for d in dconf))
    stage_of = {d["code"]: d["stage"] for d in dconf}
    col_id = {name: f"s{i}" for i, name in enumerate(order)}
    rows = {d.code: d for d in db.scalars(select(Deliverable).where(Deliverable.project_id == project.id))}
    dstatus = {c: (rows[c].status if c in rows else "not_started") for c in stage_of}

    # 阶段状态
    done = [all(dstatus[d["code"]] == "signed" for d in dconf if d["stage"] == s) for s in order]
    cur = next((i for i, ok in enumerate(done) if not ok), len(order))
    columns = []
    for i, name in enumerate(order):
        st = "done" if i < cur and done[i] else "cur" if i == cur else "next" if i == cur + 1 else \
            "done" if done[i] else "later"
        columns.append({"id": col_id[name], "name": name, "status": st,
                        "outputs": [d["code"] for d in dconf if d["stage"] == name]})
    cur_stage = order[min(cur, len(order) - 1)]

    # 交付链：AI 正在写哪几份交付件
    chain = snap["chain"]
    by_key = {s["key"]: s for s in chain}
    targets = resolve_targets(chain, stage_of)
    last = latest_run(db, project.id)
    now_key = ""
    if last is not None and last.status in ACTIVE_RUN and last.step_index < len(chain):
        now_key = chain[last.step_index]["key"]
    working: dict[str, tuple[datetime, bool]] = {}
    if now_key and last is not None and last.status in WORKING_RUN:
        eta = run_eta(db, last, now_key)
        working = {dc: eta for dc in targets.get(now_key, [])}

    cards = list(db.scalars(select(Confirmation).where(Confirmation.project_id == project.id,
                                                       Confirmation.status == "open").order_by(Confirmation.id)))
    blocked_deliv = {c.deliverable_code for c in cards if c.deliverable_code}
    evidence_n = db.scalar(select(func.count()).select_from(Evidence).where(Evidence.project_id == project.id,
                                                                                 Evidence.deleted_at.is_(None))) or 0

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    dpath = f"/project/{code}/deliverables"

    def add(nid: str, stage: str, lane: str, kind: str, status: str, title: str, sub: str, target: str,
            **extra: Any) -> None:
        nodes.append({"id": nid, "col": col_id[stage], "lane": lane, "type": kind, "status": status,
                      "title": title, "subtitle": sub, "target": target, **extra})

    def link(a: str, b: str) -> None:
        if a != b and {"from": a, "to": b} not in edges:
            edges.append({"from": a, "to": b})

    # 客户：提供资料（读证据的第一步落在的交付件所在阶段）
    ev_steps = [s for s in chain if "evidence" in s.get("inputs", [])]
    if ev_steps:
        first = targets.get(ev_steps[0]["key"], [])
        st = stage_of[first[0]] if first else order[0]
        add("client:evidence", st, "client", "evidence", "ok" if evidence_n else
            ("cur" if col_id[st] == col_id[cur_stage] else "todo"),
            "提供资料", f"已收 {evidence_n} 份", f"/project/{code}")
        for dc in first:
            link("client:evidence", f"deliv:{dc}")

    # 顾问：交付件；项目经理：签署；客户：对外交付件接收
    for d in dconf:
        dc, st = d["code"], d["stage"]
        s = dstatus[dc]
        sub = {"not_started": "未开始", "drafting": "起草中", "review": "待签署", "signed": "已签署"}.get(s, s)
        extra: dict[str, Any] = {}
        if dc in blocked_deliv and s != "signed":
            status = "blk"
        elif dc in working and s != "signed":
            eta, late = working[dc]
            status, extra = "ai", {"eta": eta.isoformat(), "late": late}
            sub = "延迟 · 已超过预计用时 2 倍" if late else "处理中"
        else:
            status = DELIV.get(s, "todo")
        target = f"drawer:deliv:{dc}" if s == "signed" else f"{dpath}/{dc}"   # 已签署：只读面板，不能直接改
        add(f"deliv:{dc}", st, "consultant", "deliverable", status, f"{dc} {d['title']}", sub, target, **extra)
        add(f"sign:{dc}", st, "manager", "signoff", SIGN.get(s, "todo"), f"签署 {dc}", "", target)
        link(f"deliv:{dc}", f"sign:{dc}")
        if d.get("client_facing"):
            add(f"client:{dc}", st, "client", "handover", "ok" if s == "signed" else "todo",
                f"接收 {dc}", "对外交付件", target)
            link(f"sign:{dc}", f"client:{dc}")

    # 交付件 → 下游交付件（只表示上下游关系）
    for s in chain:
        for dc in targets.get(s["key"], []):
            for up in _upstream(by_key, stage_of, s, set()):
                link(f"deliv:{up}", f"deliv:{dc}")

    # 顾问：未处理的确认卡（AI 停下等补证的显示为“补资料”卡），写明影响几项
    for c in cards:
        st = stage_of.get(c.deliverable_code, cur_stage)
        if c.deliverable_code in stage_of:
            link(f"card:{c.id}", f"deliv:{c.deliverable_code}")
        label = "补资料" if c.kind in BU_ZI_LIAO else "确认卡"
        add(f"card:{c.id}", st, "consultant", "card", "blk", c.title[:60],
            f"{label} · 影响 {_downstream(edges, f'card:{c.id}')} 项", f"drawer:card:{c.id}")

    m = membership(db, project.id, user.id)
    is_pm = m is not None and m.project_role == PROJECT_MANAGER
    lanes = list(LANES)
    if runs:   # “查看 Agent 运行情况”打开：多出一行，只给每一步的状态
        lanes.append(AGENT_LANE)
        done_steps = set(db.scalars(select(AgentRunStep.step).join(AgentRun, AgentRun.id == AgentRunStep.run_id)
                                    .where(AgentRun.project_id == project.id, AgentRunStep.status == "done")))
        for s in chain:
            key = s["key"]
            if key == now_key and last is not None:
                status = "blk" if last.status == "waiting" else "ai"
            else:
                status = "ok" if key in done_steps else "todo"
            dcs = targets.get(key, [])
            add(f"step:{key}", stage_of[dcs[0]] if dcs else order[0], "agent", "step", status,
                f"第 {s['number']} 步 {s['name']}", s["output"] or "运行记录", "drawer:run")
    return {"project": code, "product": snap["product"], "product_name": snap["product_name"],
            "current": col_id[cur_stage] if cur < len(order) else "", "columns": columns, "lanes": lanes,
            "nodes": nodes, "edges": edges, "my_lanes": ["consultant", "manager"] if is_pm else ["consultant"],
            "auto_done": auto_done(db, {project.id}), "can_manage_runs": is_pm}
