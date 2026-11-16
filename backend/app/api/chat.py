"""机密级检查接口和 Agent 对话（api/conversations.py）共用的审计、用量函数。要求业务角色。

顾问提问只经 Agent 对话（系统提示 = 公共规范 + 角色 md + 项目卡），裸模型接口 /api/chat、/api/chat/stream
已删除（决策记录 2026-11-16）。每次对话：机密级拦截 → 模型网关 → 写审计（不记原文）→ 写用量（不记内容）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..models import User
from ..schemas import ChatIn, FindingOut, GuardIn, GuardOut
from ..services import gateway, usage
from ..services.gateway import BlockedError, Completion, ExternalModelForbidden, GatewayError
from ..services.guard import check

router = APIRouter(prefix="/api", tags=["gateway"], dependencies=[Depends(require_business)])

UNAVAILABLE = "模型服务暂时不可用，请稍后再试"


def _guard_out(result) -> GuardOut:
    return GuardOut(blocked=result.blocked, summary=result.summary(),
                    findings=[FindingOut(kind=f.kind, label=f.label, excerpt=f.excerpt) for f in result.findings])


def _meta(body: ChatIn) -> dict:
    return {"length": len(body.message), "sha": audit.digest(body.message), "level": body.level}


# 下面三个函数也供 Agent 对话（api/conversations.py）使用：agent 为 Agent 代号，extra 为附加审计字段
# （对话编号、提示词版本），都不含原文。

def _blocked(db: Session, username: str, project_code: str, body: ChatIn, e: BlockedError, stream: bool,
             agent: str = "", extra: dict | None = None):
    audit.record(db, actor=username, action="llm.chat", result="blocked", project_code=project_code,
                 kinds=sorted({f.kind for f in e.result.findings}), stream=stream, agent=agent,
                 **(extra or {}), **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level, agent_code=agent,
                 model=gateway.model_for(body.level), status="blocked", stream=stream)
    raise HTTPException(422, detail=_guard_out(e.result).model_dump()) from e


def _external_forbidden(db: Session, username: str, project_code: str, body: ChatIn, e: ExternalModelForbidden,
                        stream: bool, agent: str = "", extra: dict | None = None):
    audit.record(db, actor=username, action="llm.chat", result="blocked", project_code=project_code,
                 reason="external_model_strict", stream=stream, agent=agent, **(extra or {}), **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level, agent_code=agent,
                 model=gateway.model_for(body.level), status="blocked", stream=stream)
    raise HTTPException(403, str(e)) from e


def _failed(db: Session, username: str, project_code: str, body: ChatIn, e: GatewayError, stream: bool,
            agent: str = "", extra: dict | None = None) -> None:
    audit.record(db, actor=username, action="llm.chat", result="error", project_code=project_code,
                 error=str(e), attempts=e.attempts, stream=stream, agent=agent, **(extra or {}), **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level, agent_code=agent,
                 model=gateway.model_for(body.level), status="error", stream=stream, attempts=e.attempts)


def _succeeded(db: Session, username: str, project_code: str, body: ChatIn, out: Completion, stream: bool,
               agent: str = "", extra: dict | None = None) -> None:
    audit.record(db, actor=username, action="llm.chat", result="allowed", project_code=project_code,
                 model=out.model, demo=out.demo, stream=stream, attempts=out.attempts,
                 pseudonymized=out.pseudonymized, external=out.external, agent=agent, **(extra or {}), **_meta(body))
    usage.record(db, username=username, project_code=project_code, level=body.level, model=out.model,
                 status="ok", stream=stream, completion=out, agent_code=agent)


@router.post("/gateway/check", response_model=GuardOut)
def gateway_check(body: GuardIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result = check(body.text)
    audit.record(db, actor=user.username, action="guard.check", result="blocked" if result.blocked else "allowed",
                 length=len(body.text), sha=audit.digest(body.text),
                 kinds=sorted({f.kind for f in result.findings}))
    return _guard_out(result)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
