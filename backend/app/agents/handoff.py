"""对话转交（二期 C2）：咨询师 Agent 在回复里写交接信息，顾问点“转交”后由平台为目标专业 Agent 新建对话。

- 交接信息格式见公共规范第 9 节：一行以“【交接】”开头，含“目标 Agent：××”，其余字段（项目卡、任务、已有材料、
  期望输出）原样作为交接说明。目标可以写名称（“调研访谈”“调研访谈 Agent”）或代号（research-interview）。
- 转交经工具层 tools.invoke('agent.handoff')：只有咨询师 Agent 有授权（迁移 0033），项目成员检查、审计照常；
  调试接口不能调（写入类工具）。目标必须是已启用、可对话的交付侧 Agent，且不是来源 Agent 本身。
- 新对话记下来源对话 handoff_from；交接说明不自动发给模型，作为草稿交给顾问，顾问可修改后发送（发送时照常过机密级拦截）。
- 审计只记来源和新对话编号、目标 Agent、说明长度，不记说明内容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..models import Agent, Conversation
from ..services.guard import check

MARK = "【交接】"
_TARGET = re.compile(r"目标\s*Agent\s*[：:]\s*([^｜|\n]+)")
_TASK = re.compile(r"任务\s*[：:]\s*([^｜|\n]+)")
BRIEF_MAX = 4000


class HandoffError(Exception):
    def __init__(self, detail: str, status: int = 422):
        self.detail, self.status = detail, status
        super().__init__(detail)


@dataclass
class Handoff:
    agent_code: str
    agent_name: str
    brief: str


def _norm(name: str) -> str:
    name = re.sub(r"\s+", "", name).strip("*`")
    return re.sub(r"(?i)agent$", "", name)


def _targets(db: Session) -> dict[str, Agent]:
    """名称、代号 → 交付侧 Agent（含未启用的，是否可用在新建时判断）。"""
    out: dict[str, Agent] = {}
    for a in db.scalars(select(Agent).where(Agent.side == "delivery")):
        out[_norm(a.code).lower()] = a
        out[_norm(a.name)] = a
    return out


def _prefix(targets: dict[str, Agent], key: str) -> Agent | None:
    """“质量评审与异议应答”这类全称：取名称是它前缀的最长一个。"""
    names = [n for n in targets if len(n) >= 2 and key.startswith(n)]
    return targets[max(names, key=len)] if names else None


def parse(db: Session, text: str, source: str) -> list[Handoff]:
    """从一条回复里找出交接信息；同一目标只取第一条，目标认不出或是来源本身的跳过。"""
    found: list[Handoff] = []
    targets = None
    for line in text.splitlines():
        line = line.strip().strip("`").strip()
        if not line.startswith(MARK):
            continue
        m = _TARGET.search(line)
        if not m:
            continue
        targets = targets if targets is not None else _targets(db)
        key = _norm(m.group(1))
        agent = targets.get(key) or targets.get(key.lower()) or _prefix(targets, key)
        if agent is None or agent.code == source or any(h.agent_code == agent.code for h in found):
            continue
        found.append(Handoff(agent.code, agent.name, line[:BRIEF_MAX]))
    return found


def draft(source_name: str, brief: str) -> str:
    return f"（由{source_name} Agent 转交）\n{brief}\n\n请按上面的交接信息开始。"


def create(db: Session, user, project, source: Agent, args: dict) -> dict:
    """工具 agent.handoff 的实现：校验来源对话和目标，新建对话，返回新对话编号和草稿。"""
    from . import roster, runtime

    try:
        cid = int(args.get("from_conversation"))
    except (TypeError, ValueError) as e:
        raise HandoffError("from_conversation 必须是对话编号") from e
    src = db.get(Conversation, cid)
    if src is None or src.project_id != project.id or src.user_id != user.id or roster.resolve(db, src.agent_code) != source.code:
        raise HandoffError("来源对话不存在", 404)
    target = roster.resolve(db, str(args.get("target", "")))    # C6-P：旧代号已停用时转到新代号
    if target == source.code:
        raise HandoffError("不能转交给自己")
    agent = db.get(Agent, target)
    if agent is None or agent.side != "delivery":
        raise HandoffError("目标 Agent 不存在", 404)
    try:
        doc = runtime.ready_doc(db, target)
    except runtime.AgentNotReady as e:
        raise HandoffError(str(e), 409) from e
    brief = str(args.get("brief", "")).strip()
    if not brief or len(brief) > BRIEF_MAX:
        raise HandoffError(f"交接说明不能为空，至多 {BRIEF_MAX} 字")
    r = check(brief)
    if r.blocked:
        raise HandoffError(f"交接说明含机密级内容：{r.summary()}")
    m = _TASK.search(brief)
    title = f"转交：{m.group(1).strip()}" if m else f"由{source.name}转交"
    c = Conversation(project_id=project.id, user_id=user.id, agent_code=target, title=title[:40],
                     prompt_version=doc.version, handoff_from=src.id)
    db.add(c)
    db.commit()
    audit.record(db, actor=user.username, action="conversation.create", result="allowed", project_code=project.code,
                 agent=target, conversation_id=c.id, prompt_version=doc.version, handoff_from=src.id,
                 brief_length=len(brief))
    return {"conversation_id": c.id, "agent_code": target, "agent_name": agent.name,
            "draft": draft(source.name, brief)}
