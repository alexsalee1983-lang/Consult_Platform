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
agent.handoff（对话转交：为专业 Agent 新建对话，规则见 app/agents/handoff.py；二期 C2）、
quality.check（机密级检查）、kb.search（知识检索）、basis.lookup（依据查询，只查 L4）、
kb.mapping（控制映射查询，只读知识区映射矩阵；二期 B3，app/kb/mapping.py）；
deid.check（去标识检查，只检查不改数据：机密级命中退回、残留直接标识有条件通过，输出不含原文；三期 C7，app/kb/deid_check.py）；
kb.stage_write 在 M2 后续接入，目前是占位实现。
kb.search / basis.lookup 只查知识区已发布内容，受限条目按调用人的可见范围过滤（app/kb/search.py）。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..core import audit
from ..models import Agent, AgentToolGrant, Project, ProjectMember, Tool, User
from ..schemas import PHASES
from ..services.guard import check

DELIVERY, KNOWLEDGE, COLLECTOR = "delivery", "knowledge", "collector"   # 所在区：项目区 / 知识区 / 采集机区（C6-P）


class ToolError(Exception):
    """工具调用失败。status 与 detail 原样交给接口层：业务模块的 422 / 409 / 404 不能被统一成 400，
    机密级拦截的结构化详情（blocked、findings）也要保留，Agent 编排据此决定改参数、重读还是放弃。"""
    status = 400

    def __init__(self, detail: Any = "", status: int | None = None):
        self.detail = detail
        if status is not None:
            self.status = status
        super().__init__(detail if isinstance(detail, str) else detail.get("summary", "被拒绝"))

    @property
    def retryable(self) -> bool:
        """临时性失败（限流、冲突、服务端暂时不可用）可以换个时机重试；参数、授权类错误重试无益。"""
        return self.status in (408, 409, 425, 429) or self.status >= 500


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
    if agent_side == COLLECTOR:
        return "采集机区 Agent 不能使用平台工具"
    if agent_side not in (DELIVERY, KNOWLEDGE):
        return "Agent 所在区未登记"
    if agent_side == KNOWLEDGE and tool_zone == "project":
        return "知识侧 Agent 不能访问项目区"
    if agent_side == DELIVERY and tool_access == "write" and tool_zone in {"knowledge", "staging"}:
        return "交付侧 Agent 不能写知识区"
    return None


# ---- 工具实现 ----

def _product_of(ctx: ToolContext) -> dict:
    """项目配置快照里的产品代号和所选模块代号（二期 C1：运行时按它拼 md 的产品段）；快照读不出来时为空。"""
    from ..products import config
    from ..project import configs

    try:
        snap = configs.snapshot(ctx.db, ctx.project)
    except config.CatalogError:
        return {"product": "", "modules": []}
    out = {"product": str(snap.get("product") or ""), "modules": [m["code"] for m in snap.get("modules") or []]}
    ind = snap.get("industry")
    if isinstance(ind, dict):   # 三期行业包：术语、受访角色、威胁画像、立项提示进项目卡
        out["industry_pack"] = {**{k: ind.get(k) for k in ("code", "name", "level", "terms", "roles", "threats",
                                                            "tier_hint")},
                                "expert_pending": ind.get("code") in (snap.get("expert_pending") or [])}
    return out


def _project_read(ctx: ToolContext, args: dict) -> dict:
    p = ctx.db.scalar(select(Project).where(Project.id == ctx.project.id)
                      .options(selectinload(Project.deliverables), selectinload(Project.confirmations)))
    card = {"code": p.code, "service": p.service, "industry": p.industry, "tier": p.tier,
            "phase": PHASES[p.phase] if 0 <= p.phase < len(PHASES) else "", "week": p.week,
            "total_weeks": p.total_weeks, **_product_of(ctx)}
    if p.tier == "strict":  # 严格档：只给项目画像，事实只来自摘要卡（M4 ④，经 project.evidence 读取）
        return {"project": card, "note": "严格档项目：客户事实只来自去标识摘要卡（经 project.evidence 读取 S-xx），"
                                         "输出一律用占位符"}
    return {"project": card,
            "deliverables": [{"code": d.code, "title": d.title, "status": d.status} for d in p.deliverables],
            "open_confirmations": [{"id": c.id, "kind": c.kind, "title": c.title} for c in p.confirmations
                                   if c.status == "open"]}


def _int_arg(args: dict, name: str, default: int) -> int:
    """整数参数：Agent 传来的字符串数字照收，不是整数的返回 422（不能让 ValueError 变成 500）。"""
    value = args.get(name, default)
    if value in (None, ""):
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
        return int(value)
    raise ToolError(f"{name} 必须是整数", status=422)


def _project_evidence(ctx: ToolContext, args: dict) -> dict:
    from ..project import evidence

    offset, limit = _int_arg(args, "offset", 0), _int_arg(args, "limit", evidence.READ_CHARS)
    return evidence.read_for_agent(ctx.db, ctx.project, str(args.get("code", "")), offset, limit)


def _quality_check(ctx: ToolContext, args: dict) -> dict:
    r = check(str(args.get("text", "")))
    return {"blocked": r.blocked, "summary": r.summary(),
            "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt} for f in r.findings]}


def _deid_check(ctx: ToolContext, args: dict) -> dict:
    from ..kb import deid_check      # 延迟导入：kb 包依赖较多，工具层只在调用时加载
    return deid_check.check(str(args.get("text", ""))).to_json()


def _project_kb_prefs(ctx: ToolContext) -> dict:
    """项目上下文里检索的默认值（二期 B2）：快照的产品、所选模块的 kb_tags、basis 列出的标准
    （B2 补充：项目行业在快照 industry_basis 里有对应的，行业依据排在前面一并加权）。
    没有项目或快照读不出来时都为空（检索照常进行，不按产品排序）。"""
    if ctx.project is None:
        return {}
    from ..products import config
    from ..project import configs

    try:
        snap = configs.snapshot(ctx.db, ctx.project)
    except config.CatalogError:
        return {}
    industry = (ctx.project.industry or "").strip()
    ib = snap.get("industry_basis") or {}
    keys = dict.fromkeys([industry, *industry.split("/")]) if industry else {}   # 立项下拉的“银行/金融”任一部分对上
    pack = snap.get("industry") if isinstance(snap.get("industry"), dict) else {}
    by_industry = [*(pack.get("basis") or []), *(b for k in keys if k for b in (ib.get(k) or []))]   # 三期行业包在前
    basis = list(dict.fromkeys(by_industry + list(snap.get("basis") or [])))   # 行业依据在前（二期 B2 补充）
    return {"product": str(snap.get("product") or ""), "tags": list(snap.get("kb_tags") or []), "basis": basis}


def _kb_search(ctx: ToolContext, args: dict) -> dict:
    from ..kb import search as kb

    prefs = _project_kb_prefs(ctx)
    try:
        return kb.search(ctx.db, ctx.user, str(args.get("query", "")), layer=str(args.get("layer", "")),
                         product=str(args.get("product", "") or prefs.get("product", "")),
                         stage=str(args.get("stage", "")), doc_type=str(args.get("doc_type", "")),
                         limit=_int_arg(args, "limit", 8), boost_tags=prefs.get("tags", ()))
    except kb.SearchError as e:
        raise ToolError(e.detail, status=e.status) from e


def _basis_lookup(ctx: ToolContext, args: dict) -> dict:
    from ..kb import search as kb

    prefs = _project_kb_prefs(ctx)
    try:
        return kb.basis(ctx.db, ctx.user, str(args.get("query", "")), ref_document=str(args.get("ref_document", "")),
                        limit=_int_arg(args, "limit", 5), product=prefs.get("product", ""),
                        boost_refs=prefs.get("basis", ()))
    except kb.SearchError as e:
        raise ToolError(e.detail, status=e.status) from e


def _kb_mapping(ctx: ToolContext, args: dict) -> dict:
    from ..kb import mapping

    try:
        return mapping.lookup(ctx.db, ctx.user, str(args.get("source", "")), target=str(args.get("target", "")),
                              control=str(args.get("control", "")), query=str(args.get("query", "")),
                              limit=_int_arg(args, "limit", mapping.DEFAULT_LIMIT))
    except mapping.MappingError as e:
        raise ToolError(e.detail, status=e.status) from e


def _doc_generate(ctx: ToolContext, args: dict) -> dict:
    from ..project import deliverables

    try:
        return deliverables.generate_for_agent(ctx.db, ctx.user, ctx.project, ctx.agent.code, args)
    except deliverables.DeliverableError as e:
        raise ToolError(e.detail, status=e.status) from e


def _confirm_create(ctx: ToolContext, args: dict) -> dict:
    from ..project import confirmations

    try:
        c, created = confirmations.create(ctx.db, ctx.user, ctx.project, ctx.agent, args)
    except confirmations.ConfirmationError as e:
        raise ToolError(e.detail, status=e.status) from e
    return confirmations.card_for_agent(c, created)


def _agent_handoff(ctx: ToolContext, args: dict) -> dict:
    from . import handoff

    try:
        return handoff.create(ctx.db, ctx.user, ctx.project, ctx.agent, args)
    except handoff.HandoffError as e:
        raise ToolError(e.detail, status=e.status) from e


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
    "kb.mapping": _kb_mapping,
    "deid.check": _deid_check,
    "doc.generate": _doc_generate,
    "confirm.create": _confirm_create,
    "agent.handoff": _agent_handoff,
    "kb.stage_write": _placeholder("M2 投料", {"staged": False}),
}
NEEDS_PROJECT = frozenset({"project.read", "project.evidence", "doc.generate", "confirm.create", "agent.handoff"})
PROJECT_PREFS = frozenset({"kb.search", "basis.lookup"})   # 在项目里调用时按项目配置快照取默认产品等（B2）


def granted_tools(db: Session, agent_code: str) -> list[str]:
    return sorted(db.scalars(select(AgentToolGrant.tool_code).where(AgentToolGrant.agent_code == agent_code)))


def invoke(db: Session, user: User, agent_code: str, tool_code: str, args: dict[str, Any] | None = None,
           project_code: str | None = None, conversation_id: int | None = None) -> dict:
    args = args or {}
    meta: dict[str, Any] = {"agent": agent_code, "tool": tool_code,
                            "args_length": len(json.dumps(args, ensure_ascii=False))}
    if conversation_id is not None:
        meta["conversation_id"] = conversation_id    # 对话里调用时带对话号，便于按对话追溯（只记编号，不记原文）

    def deny(exc: type[ToolError], reason: str, message: str):
        audit.record(db, actor=user.username, action="tool.call", result="blocked", project_code=project_code or "",
                     reason=reason, **meta)
        raise exc(message)

    from . import roster

    resolved = roster.resolve(db, agent_code)    # C6-P：旧代号已停用时转到收敛后的新代号
    if resolved != agent_code:
        meta["alias_of"], meta["agent"], agent_code = agent_code, resolved, resolved
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
    elif project_code and tool_code in PROJECT_PREFS and agent.side == DELIVERY:
        # 知识检索不需要项目，但交付侧在项目里调用时按项目配置快照排序（B2）；不是成员就当没有项目，不报错、不泄露
        project = db.scalar(select(Project).join(ProjectMember, ProjectMember.project_id == Project.id)
                            .where(Project.code == project_code, ProjectMember.user_id == user.id))

    result = IMPLEMENTATIONS[tool_code](ToolContext(db, user, agent, tool, project), args)
    audit.record(db, actor=user.username, action="tool.call", result="allowed", project_code=project_code or "",
                 **meta)
    return result
