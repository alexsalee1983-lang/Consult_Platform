"""项目全景（四期 A25①，docs/驾驶舱改版方案.md 第三节）：按项目配置快照 + 项目现状生成泳道全景，只读。

- 列 = 阶段：快照里交付件的 stage，按首次出现的顺序（SOC、NSG 五段；IRE、DSM、ISO 多“落地辅导”）。
  阶段状态：交付件全部签署为 done；第一个没完成的为 cur，其后一个 next，再往后 later。
- 行 = 泳道：客户 / 顾问 / 项目经理 / AI·平台。
  AI·平台：交付链步骤（有产出交付件的放在该交付件的阶段；只存运行记录的，放在引用它的步骤里最早的阶段）。
  顾问：每份交付件的撰写与审核、未处理的确认卡（挂交付件的放该交付件阶段，否则放当前阶段）。
  项目经理：每份交付件的签署。
  客户：提供资料（交付链读证据那一步所在阶段）、对外交付件的接收。
- 节点状态：ok 已完成 / ai AI 在做 / cur 进行中 / blk 卡住（有未处理确认卡、交付链停下等补证）/ todo 未开始。
- 跳转目标：drawer:card:{id}、drawer:run 由前端打开已有组件；其余为页面路由。

只用配置里的标题和确认卡标题（项目成员本来就能看到），不读证据、章节正文；不写审计（只读接口，同项目详情）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.permissions import PROJECT_MANAGER, membership
from ..models import AgentRun, AgentRunStep, Confirmation, Deliverable, Evidence, Project, User
from . import configs

LANES = [
    {"id": "client", "name": "客户"},
    {"id": "consultant", "name": "顾问"},
    {"id": "manager", "name": "项目经理"},
    {"id": "ai", "name": "AI·平台"},
]
ACTIVE_RUN = {"queued", "running"}


def _step_stages(chain: list[dict], stage_of: dict[str, str], order: list[str]) -> dict[str, str]:
    """每个交付链步骤所在阶段：有产出交付件的取该交付件阶段；否则取引用它（run:key）的步骤里最早的阶段。"""
    out: dict[str, str] = {s["key"]: stage_of[s["output"]] for s in chain if s.get("output") in stage_of}
    for _ in range(len(chain)):          # 引用可能是链式的（gap → design → policy），迭代到稳定
        for s in chain:
            if s["key"] in out:
                continue
            users = [out[t["key"]] for t in chain if f"run:{s['key']}" in t.get("inputs", []) and t["key"] in out]
            if users:
                out[s["key"]] = min(users, key=order.index)
    for s in chain:                      # 兜底：读交付件的取输入交付件阶段，再不行放第一列
        if s["key"] not in out:
            ins = [stage_of[i] for i in s.get("inputs", []) if i in stage_of]
            out[s["key"]] = max(ins, key=order.index) if ins else order[0]
    return out


def _deliv_status(status: str) -> str:
    return {"signed": "ok", "review": "ok", "drafting": "cur"}.get(status, "todo")


def _sign_status(status: str) -> str:
    return {"signed": "ok", "review": "cur"}.get(status, "todo")


def build(db: Session, project: Project, user: User) -> dict[str, Any]:
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

    # 交付链运行：完成过的步骤、最近一次运行在做 / 停下的步骤
    chain = snap["chain"]
    done_steps = set(db.scalars(select(AgentRunStep.step).join(AgentRun, AgentRun.id == AgentRunStep.run_id)
                                .where(AgentRun.project_id == project.id, AgentRunStep.status == "done")))
    last = db.scalar(select(AgentRun).where(AgentRun.project_id == project.id).order_by(AgentRun.id.desc()).limit(1))
    now_key = ""
    if last is not None and (last.status in ACTIVE_RUN or last.status == "waiting") and last.step_index < len(chain):
        now_key = chain[last.step_index]["key"]
    step_stage = _step_stages(chain, stage_of, order)

    cards = list(db.scalars(select(Confirmation).where(Confirmation.project_id == project.id,
                                                       Confirmation.status == "open").order_by(Confirmation.id)))
    blocked_deliv = {c.deliverable_code for c in cards if c.deliverable_code}
    evidence_n = db.scalar(select(func.count()).select_from(Evidence).where(Evidence.project_id == project.id,
                                                                                 Evidence.deleted_at.is_(None))) or 0

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    dpath = f"/project/{code}/deliverables"

    def add(nid: str, stage: str, lane: str, kind: str, status: str, title: str, sub: str, target: str) -> None:
        nodes.append({"id": nid, "col": col_id[stage], "lane": lane, "type": kind, "status": status,
                      "title": title, "subtitle": sub, "target": target})

    # 客户：提供资料（读证据的步骤所在阶段）
    ev_steps = [s for s in chain if "evidence" in s.get("inputs", [])]
    if ev_steps:
        st = step_stage[ev_steps[0]["key"]]
        add("client:evidence", st, "client", "evidence", "ok" if evidence_n else
            ("cur" if col_id[st] == col_id[cur_stage] else "todo"),
            "提供资料", f"已收 {evidence_n} 份", f"/project/{code}")
        edges.append({"from": "client:evidence", "to": f"step:{ev_steps[0]['key']}"})

    # AI·平台：交付链步骤
    for s in chain:
        key = s["key"]
        if key == now_key and last is not None:
            status = "blk" if last.status == "waiting" else "ai"
        elif key in done_steps:
            status = "ok"
        else:
            status = "todo"
        add(f"step:{key}", step_stage[key], "ai", "step", status, f"第 {s['number']} 步 {s['name']}",
            s["output"] or "运行记录", "drawer:run")
        for i in s.get("inputs", []):
            if i.startswith("run:"):
                edges.append({"from": f"step:{i[4:]}", "to": f"step:{key}"})
            elif i in stage_of:
                edges.append({"from": f"deliv:{i}", "to": f"step:{key}"})
        if s.get("output") in stage_of:
            edges.append({"from": f"step:{key}", "to": f"deliv:{s['output']}"})

    # 顾问：交付件；项目经理：签署；客户：对外交付件接收
    for d in dconf:
        dc, st = d["code"], d["stage"]
        s = dstatus[dc]
        status = "blk" if dc in blocked_deliv and s != "signed" else _deliv_status(s)
        add(f"deliv:{dc}", st, "consultant", "deliverable", status, f"{dc} {d['title']}",
            {"not_started": "未开始", "drafting": "起草中", "review": "待签署", "signed": "已签署"}.get(s, s),
            f"{dpath}/{dc}")
        add(f"sign:{dc}", st, "manager", "signoff", _sign_status(s), f"签署 {dc}", "", f"{dpath}/{dc}")
        edges.append({"from": f"deliv:{dc}", "to": f"sign:{dc}"})
        if d.get("client_facing"):
            add(f"client:{dc}", st, "client", "handover", "ok" if s == "signed" else "todo",
                f"接收 {dc}", "对外交付件", f"{dpath}/{dc}")
            edges.append({"from": f"sign:{dc}", "to": f"client:{dc}"})

    # 顾问：未处理的确认卡
    for c in cards:
        st = stage_of.get(c.deliverable_code, cur_stage)
        add(f"card:{c.id}", st, "consultant", "card", "blk", c.title[:60], c.kind, f"drawer:card:{c.id}")
        if c.deliverable_code in stage_of:
            edges.append({"from": f"card:{c.id}", "to": f"deliv:{c.deliverable_code}"})

    m = membership(db, project.id, user.id)
    my_lanes = ["consultant", "manager"] if m is not None and m.project_role == PROJECT_MANAGER else ["consultant"]
    return {"project": code, "product": snap["product"], "product_name": snap["product_name"],
            "current": col_id[cur_stage] if cur < len(order) else "", "columns": columns, "lanes": LANES,
            "nodes": nodes, "edges": edges, "my_lanes": my_lanes}
