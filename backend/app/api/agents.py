"""Agent 与工具：查看可用 Agent 及其工具；以某个 Agent 的身份调用工具（Agent 运行时也走同一个 invoke）。

要求业务角色。工具授权只读，没有修改接口（改授权走迁移和代码审阅）。
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import tools
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..models import Agent, User

router = APIRouter(prefix="/api/agents", tags=["agents"], dependencies=[Depends(require_business)])


class AgentOut(BaseModel):
    code: str
    name: str
    side: str
    tools: list[str]


class ToolCallIn(BaseModel):
    project_code: str | None = Field(None, max_length=32)
    args: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db)):
    agents = db.scalars(select(Agent).where(Agent.enabled.is_(True)).order_by(Agent.side, Agent.code))
    return [AgentOut(code=a.code, name=a.name, side=a.side, tools=tools.granted_tools(db, a.code)) for a in agents]


@router.post("/{agent}/tools/{tool}")
def call_tool(agent: str, tool: str, body: ToolCallIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)) -> dict:
    try:
        return tools.invoke(db, user, agent, tool, body.args, body.project_code)
    except tools.ToolError as e:
        raise HTTPException(e.status, str(e)) from e
