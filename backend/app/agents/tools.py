"""工具层：工具注册表 + 每次调用的授权检查。

Agent 读写数据只能经 invoke()。每次调用依次检查（任一不满足即拒绝，并写审计）：
1. Agent 已登记且启用；工具已登记、且有实现；
2. agent_tool_grants 中有这条授权；
3. 硬规则（与授权表无关，改表也绕不过）：
   - 知识侧 Agent 不能用任何项目区工具（“知识侧 Agent 读不到项目区”）；
   - 交付侧 Agent 不能写知识区和待入库区（“交付侧 Agent 写不了知识区”）；
4. 涉及项目的工具，发起对话的用户必须是该项目成员（非成员与项目不存在一样）。

审计只记 Agent、工具、项目、结果和参数长度，不记参数和结果内容。
已实现：project.read（真实数据）、project.evidence（查证据：清单或某条证据的正文，分段返回；M3）、
doc.generate（交付件分章节：读大纲 / 读章节、写大纲、写章节新版本，规则见 app/project/deliverables.py；M3）、
confirm.create（发确认卡：选项、推荐和理由、影响，规则见 app/project/confirmations.py；M3）、
quality.check（机密级检查）、kb.search（知识检索）、basis.lookup（依据查询，只查 L4）；
kb.stage_write 在 M2 后续接入，目前是占位实现。
kb.search / basis.lookup 只查知识区已发布内容，受限条目按调用人的可见范围过滤（app/kb/search.py）。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core import audit
from ..models import Agent, AgentToolGrant, Project, ProjectMember, Tool, User
from ..schemas import PHASES
from ..services.guard import check

DELIVERY, KNOWLEDGE = "delivery", "knowledge"


class ToolError(Exception):
    status = 400


class ToolDenied(ToolError):
    status = 403


class ToolNotFound(ToolError):
    status = 404


@dataclass
class ToolContext:
    db: Session
    user: User
    agent: Agent
    tool: Tool
    project: Project | None


def violates_hard_rule(agent_side: str, tool_zone: str, tool_access: str) -> str | None:
    """返回违反的硬规则说明；不违反时返回 None。授权写入和每次调用都检查。"""
    if agent_side == KNOWLEDGE and tool_zone == "project":
        return "知识侧 Agent 不能访问项目区"
    if agent_side == DELIVERY and tool_access == "write" and tool_zone in {"knowledge", "staging"}:
        return "交付侧 Agent 不能写知识区"
    return None


# ---- 工具实现 ----

def _project_read(ctx: ToolContext, args: dict) -> dict:
    p = ctx.db.scalar(select(Project).where(Project.id == ctx.project.id)
                      .options(selectinload(Project.deliverables), selectinload(Project.confirmations)))
    card = {"code": p.code, "service": p.service, "industry": p.industry, "tier": p.tier,
            "phase": PHASES[p.phase] if 0 <= p.phase < len(PHASES) else "", "week": p.week,
            "total_weeks": p.total_weeks}
    if p.tier == "strict":  # 严格档：只给项目画像，事实只来自摘要卡（M4）
        return {"project": card, "note": "严格档项目：Agent 只能使用摘要卡，摘要卡在 M4 接入"}
    return {"project": card,
            "deliverables": [{"code": d.code, "title": d.title, "status": d.status} for d in p.deliverables],
            "open_confirmations": [{"id": c.id, "kind": c.kind, "title": c.title} for c in p.confirmations
                                   if c.status == "open"]}


def _project_evidence(ctx: ToolContext, args: dict) -> dict:
    from ..project import evidence

    try:
        offset, limit = int(args.get("offset", 0)), int(args.get("limit", evidence.READ_CHARS))
    except (TypeError, ValueError) as e:
        raise ToolError("offset、limit 必须是整数") from e
    return evidence.read_for_agent(ctx.db, ctx.project, str(args.get("code", "")), offset, limit)


def _quality_check(ctx: ToolContext, args: dict) -> dict:
    r = check(str(args.get("text", "")))
    return {"blocked": r.blocked, "summary": r.summary(),
            "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt} for f in r.findings]}


def _kb_search(ctx: ToolContext, args: dict) -> dict:
    from ..kb import search as kb

    try:
        return kb.search(ctx.db, ctx.user, str(args.get("query", "")), layer=str(args.get("layer", "")),
                         product=str(args.get("product", "")), stage=str(args.get("stage", "")),
                         doc_type=str(args.get("doc_type", "")), limit=int(args.get("limit", 8)))
    except kb.SearchError as e:
        raise ToolError(e.detail if isinstance(e.detail, str) else e.detail.get("summary", "检索被拒绝")) from e


def _basis_lookup(ctx: ToolContext, args: dict) -> dict:
    from ..kb import search as kb

    try:
        return kb.basis(ctx.db, ctx.user, str(args.get("query", "")), ref_document=str(args.get("ref_document", "")),
                        limit=int(args.get("limit", 5)))
    except kb.SearchError as e:
        raise ToolError(e.detail if isinstance(e.detail, str) else e.detail.get("summary", "检索被拒绝")) from e


def _doc_generate(ctx: ToolContext, args: dict) -> dict:
    from ..project import deliverables

    try:
        return deliverables.generate_for_agent(ctx.db, ctx.user, ctx.project, ctx.agent.code, args)
    except deliverables.DeliverableError as e:
        raise ToolError(e.detail if isinstance(e.detail, str) else e.detail.get("summary", "被拒绝")) from e


def _confirm_create(ctx: ToolContext, args: dict) -> dict:
    from ..project import confirmations

    try:
        c, created = confirmations.create(ctx.db, ctx.user, ctx.project, ctx.agent, args)
    except confirmations.ConfirmationError as e:
        raise ToolError(e.detail if isinstance(e.detail, str) else e.detail.get("summary", "被拒绝")) from e
    return confirmations.card_for_agent(c, created)


def _placeholder(milestone: str, empty: dict) -> Callable[[ToolContext, dict], dict]:
    def run(ctx: ToolContext, args: dict) -> dict:
        return {**empty, "placeholder": True, "note": f"该工具在 {milestone} 接入，目前返回空结果"}
    return run


IMPLEMENTATIONS: dict[str, Callable[[ToolContext, dict], dict]] = {
    "project.read": _project_read,
    "project.evidence": _project_evidence,
    "quality.check": _quality_check,
    "kb.search": _kb_search,
    "basis.lookup": _basis_lookup,
    "doc.generate": _doc_generate,
    "confirm.create": _confirm_create,
    "kb.stage_write": _placeholder("M2 投料", {"staged": False}),
}
NEEDS_PROJECT = frozenset({"project.read", "project.evidence", "doc.generate", "confirm.create"})


def granted_tools(db: Session, agent_code: str) -> list[str]:
    return sorted(db.scalars(select(AgentToolGrant.tool_code).where(AgentToolGrant.agent_code == agent_code)))


def invoke(db: Session, user: User, agent_code: str, tool_code: str, args: dict[str, Any] | None = None,
           project_code: str | None = None) -> dict:
    args = args or {}
    meta = {"agent": agent_code, "tool": tool_code, "args_length": len(json.dumps(args, ensure_ascii=False))}

    def deny(exc: type[ToolError], reason: str, message: str):
        audit.record(db, actor=user.username, action="tool.call", result="blocked", project_code=project_code or "",
                     reason=reason, **meta)
        raise exc(message)

    agent = db.get(Agent, agent_code)
    if agent is None or not agent.enabled:
        deny(ToolNotFound, "agent_unknown_or_disabled", "Agent 不存在或未启用")
    tool = db.get(Tool, tool_code)
    if tool is None or tool_code not in IMPLEMENTATIONS:
        deny(ToolNotFound, "tool_unknown", "工具不存在")
    if db.get(AgentToolGrant, (agent_code, tool_code)) is None:
        deny(ToolDenied, "not_granted", f"{agent.name} Agent 没有“{tool.name}”工具的授权")
    rule = violates_hard_rule(agent.side, tool.zone, tool.access)
    if rule:
        deny(ToolDenied, "hard_rule", rule)

    project = None
    if tool_code in NEEDS_PROJECT:
        if not project_code:
            deny(ToolError, "project_required", "该工具需要指定项目")
        project = db.scalar(select(Project).join(ProjectMember, ProjectMember.project_id == Project.id)
                            .where(Project.code == project_code, ProjectMember.user_id == user.id))
        if project is None:
            deny(ToolNotFound, "not_member", "项目不存在")

    result = IMPLEMENTATIONS[tool_code](ToolContext(db, user, agent, tool, project), args)
    audit.record(db, actor=user.username, action="tool.call", result="allowed", project_code=project_code or "",
                 **meta)
    return result
