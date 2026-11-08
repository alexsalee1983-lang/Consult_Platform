"""Agent 运行时：组装系统提示、注入项目卡、带上对话历史，经模型网关调用。

- 系统提示 = 01_公共规范.md + 角色 md 正文 + 平台生成的项目卡（公共规范第 6 节格式）。
- 项目卡经工具层读取（tools.invoke 'project.read'），所以同样受 Agent × 工具授权、硬规则和项目成员检查约束，
  并写审计；知识侧 Agent 没有读项目区的授权，不能在项目里对话（知识区对话在 M2 知识服务提供）。
- 方案设计 Agent 等按项目卡的“服务产品”选模块，运行时不需要特殊处理。
- 模型能力档取 md 的 model_tier（推理 / 标准 / 轻量），没有写时用标准档。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..models import Agent, Message, User
from . import registry, tools

TIER_NAMES = {"standard": "标准档", "strict": "严格档"}
LEVEL_NAMES = {"reasoning": "推理", "standard": "标准", "light": "轻量"}


class AgentNotReady(Exception):
    pass


@dataclass
class AgentInfo:
    code: str
    name: str
    side: str
    tools: list[str]
    ready: bool
    reason: str
    level: str
    prompt_version: str
    notes: list[str]


def agent_info(db: Session, agent: Agent) -> AgentInfo:
    granted = tools.granted_tools(db, agent.code)
    doc = registry.load(agent.code)
    if not agent.enabled:
        return AgentInfo(agent.code, agent.name, agent.side, granted, False, "预留，未启用", "standard", "", [])
    if doc is None:
        return AgentInfo(agent.code, agent.name, agent.side, granted, False, "缺少 md 或 md 的 name 不一致",
                         "standard", "", [])
    if agent.side != tools.DELIVERY:
        return AgentInfo(agent.code, agent.name, agent.side, granted, False, "知识侧 Agent，知识区对话在 M2 提供",
                         doc.level, doc.version, registry.tools_mismatch(doc, granted))
    return AgentInfo(agent.code, agent.name, agent.side, granted, True, "", doc.level, doc.version,
                     registry.tools_mismatch(doc, granted))


def list_agents(db: Session) -> list[AgentInfo]:
    return [agent_info(db, a) for a in db.scalars(select(Agent).order_by(Agent.side, Agent.code))]


def ready_doc(db: Session, agent_code: str) -> registry.AgentDoc:
    agent = db.get(Agent, agent_code)
    if agent is None:
        raise AgentNotReady("Agent 不存在")
    info = agent_info(db, agent)
    if not info.ready:
        raise AgentNotReady(f"{agent.name} Agent 暂不可用：{info.reason}")
    return registry.load(agent_code)


def project_card(db: Session, user: User, agent_code: str, project_code: str) -> str:
    """经工具层读项目区，渲染成公共规范第 6 节的项目卡。"""
    data = tools.invoke(db, user, agent_code, "project.read", {}, project_code)
    p = data["project"]
    lines = [
        "# 项目卡（平台生成）",
        f"- 客户代号*：{p['code']}",
        f"- 数据档位*：{TIER_NAMES.get(p['tier'], p['tier'])}",
        f"- 行业*：{p['industry']}",
        f"- 服务产品*：{p['service']}",
        f"- 当前阶段*：{p['phase']}（第 {p['week']} / {p['total_weeks']} 周）",
    ]
    if "deliverables" in data:
        lines.append("- 交付件进度：" + "；".join(f"{d['code']} {d['title']}（{d['status']}）"
                                              for d in data["deliverables"]))
        if data.get("open_confirmations"):
            lines.append("- 待顾问确认事项：" + "；".join(f"[{c['kind']}] {c['title']}"
                                                   for c in data["open_confirmations"]))
    if data.get("note"):
        lines.append(f"- 平台说明：{data['note']}")
    lines.append("- 合同范围、适用监管要求、已确认事项、关键干系人、已知敏感点：平台暂未录入，需要时向顾问索取")
    return "\n".join(lines)


def system_prompt(doc: registry.AgentDoc, card: str) -> str:
    return (f"{doc.common.strip()}\n\n---\n\n{doc.body.strip()}\n\n---\n\n{card}\n\n"
            "（平台说明：以上项目卡由平台根据项目区数据生成。知识检索、依据查询等工具在后续版本接入，"
            "目前查不到依据库条目时按公共规范第 3 节标注【依据待核实】。）")


def history(messages: list[Message]) -> list[dict]:
    """从最近往前取，总字数不超过 AGENT_HISTORY_CHARS；中断的不完整回复不带上。"""
    budget = get_settings().agent_history_chars
    picked: list[dict] = []
    for m in reversed(messages):
        if m.status != "ok":
            continue
        if budget - len(m.content) < 0:
            break
        budget -= len(m.content)
        picked.append({"role": m.role, "content": m.content})
    return list(reversed(picked))
