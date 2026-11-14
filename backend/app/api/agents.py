"""Agent 与工具：查看可用 Agent 及其工具；以某个 Agent 的身份调用读取类工具（Agent 运行时也走同一个 invoke）。

写入类工具（doc.generate、confirm.create）只由平台运行时调用：经接口直接调用等于让真人冒用 Agent 的身份
写交付件、发确认卡（渗透测试 M5 ⑦），一律 403。

要求业务角色。工具授权只读，没有修改接口（改授权走迁移和代码审阅）。
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import runtime, tools
from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..models import Agent, Tool, User

router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(require_business)])


class AgentOut(BaseModel):
    code: str
    name: str
    side: str
    tools: list[str]
    ready: bool = False          # 能否在项目里发起对话
    reason: str = ""             # 不可用的原因
    level: str = "standard"      # 模型能力档（md 的 model_tier）
    prompt_version: str = ""     # 当前提示词版本（公共规范 + 角色 md 的内容哈希）


class ToolCallIn(BaseModel):
    project_code: str | None = Field(None, max_length=32)
    args: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db)):
    agents = db.scalars(select(Agent).where(Agent.enabled.is_(True)).order_by(Agent.side, Agent.code))
    out = []
    for a in agents:
        i = runtime.agent_info(db, a)
        out.append(AgentOut(code=i.code, name=i.name, side=i.side, tools=i.tools, ready=i.ready, reason=i.reason,
                            level=i.level, prompt_version=i.prompt_version))
    return out


@router.post("/{agent}/tools/{tool}")
def call_tool(agent: str, tool: str, body: ToolCallIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> dict:
    t = db.get(Tool, tool)
    if t is not None and t.access != "read":
        audit.record(db, actor=user.username, action="tool.call", result="blocked",
                     project_code=body.project_code or "", agent=agent, tool=tool, reason="write_via_api")
        raise HTTPException(403, "写入类工具只由平台运行时调用（Agent 对话、交付链），不能经接口直接调用")
    try:
        return tools.invoke(db, user, agent, tool, body.args, body.project_code)
    except tools.ToolError as e:
        raise HTTPException(e.status, e.detail) from e
