"""Agent 运行时：组装系统提示、注入项目卡、带上对话历史，经模型网关调用。

- 系统提示 = 01_公共规范.md + 角色 md 正文 + 平台生成的项目卡（公共规范第 6 节格式）。
- 项目卡经工具层读取（tools.invoke 'project.read'），所以同样受 Agent × 工具授权、硬规则和项目成员检查约束，
  并写审计；知识侧 Agent 没有读项目区的授权，不能在项目里对话（知识区对话在 M2 知识服务提供）。
- 二期 C1：md 正文按项目裁剪（registry.select_body），只拼入本项目产品的“## 产品：”段和所选模块的“### 模块：”段；
  拼了哪几段由 md 版本和项目配置快照唯一确定。
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


class Card(str):
    """项目卡正文，另带项目的产品和所选模块代号（system_prompt 用来裁剪 md）。"""
    product: str | None = None
    modules: tuple[str, ...] = ()


def project_card(db: Session, user: User, agent_code: str, project_code: str) -> Card:
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
    if p.get("modules"):
        lines.append(f"- 所选模块：{'、'.join(p['modules'])}")
    pack = p.get("industry_pack")
    if pack:   # 三期行业包（4A v4.0 §2.4）
        mark = "，待专家复核" if pack.get("expert_pending") else ""
        lines.append(f"- 行业包：{pack.get('name')}（{pack.get('level')}{mark}）")
        for label, key in (("行业术语与系统名", "terms"), ("常见受访角色", "roles"), ("行业威胁画像", "threats")):
            if pack.get(key):
                lines.append(f"  - {label}：{'；'.join(pack[key])}")
        if pack.get("tier_hint"):
            lines.append(f"  - 立项提示：{pack['tier_hint']}")
    if "deliverables" in data:
        lines.append("- 交付件进度：" + "；".join(f"{d['code']} {d['title']}（{d['status']}）"
                                              for d in data["deliverables"]))
        if data.get("open_confirmations"):
            lines.append("- 待顾问确认事项：" + "；".join(f"[{c['kind']}] {c['title']}"
                                                   for c in data["open_confirmations"]))
    if data.get("note"):
        lines.append(f"- 平台说明：{data['note']}")
    lines.append("- 合同范围、适用监管要求、已确认事项、关键干系人、已知敏感点：平台暂未录入，需要时向顾问索取")
    card = Card("\n".join(lines))
    card.product, card.modules = str(p.get("product") or ""), tuple(p.get("modules") or ())
    return card


def system_prompt(doc: registry.AgentDoc, card: str, knowledge: str = "", tool_mode: bool = False) -> str:
    """公共规范 + 角色 md（按项目卡的产品和模块裁剪，二期 C1）+ 项目卡 + 本轮知识库检索结果（app/agents/knowledge.py）。
    tool_mode（二期 A7）：对话里由 Agent 自己调用工具，不附检索结果，改为说明怎么用工具。"""
    if tool_mode:
        body, _ = registry.select_body(doc.body, getattr(card, "product", None), getattr(card, "modules", ()))
        note = ("（平台说明：以上项目卡由平台根据项目区数据生成。本次对话你可以调用平台提供的工具"
                "（知识检索、依据查询、读项目证据等，以实际提供的为准）：回答涉及公司做法、法规标准、项目事实时先调用工具查，"
                "不要凭记忆。使用知识库内容注明 [doc_id]，项目事实注明证据编号；法规标准条款只能按依据查询返回的引用格式引用，"
                "查不到的按公共规范第 3 节标注【依据待核实】，不得补写条款号或原文。工具返回错误时换参数重试或说明查不到。）")
        return f"{doc.common.strip()}\n\n---\n\n{body.strip()}\n\n---\n\n{card}\n\n{note}"
    note = ("（平台说明：以上项目卡由平台根据项目区数据生成。平台在每次提问前按问题检索知识库，命中的条目附在下面；"
            "使用其中内容时注明 [doc_id]；法规标准条款只能按 L4 条目给出的引用格式引用。"
            "检索结果里没有的依据，按公共规范第 3 节标注【依据待核实】，不得凭记忆补写条款号或原文。）")
    tail = f"\n\n---\n\n{knowledge}" if knowledge else "\n\n（本轮没有检索到相关知识库条目。）"
    body, _ = registry.select_body(doc.body, getattr(card, "product", None), getattr(card, "modules", ()))
    return f"{doc.common.strip()}\n\n---\n\n{body.strip()}\n\n---\n\n{card}\n\n{note}{tail}"


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
