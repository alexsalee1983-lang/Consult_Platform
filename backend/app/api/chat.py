"""顾问对话与机密级检查。要求业务角色；对话只能在自己是成员的项目里进行。"""
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..schemas import ChatIn, ChatOut, FindingOut, GuardIn, GuardOut
from ..services.gateway import BlockedError, complete
from ..services.guard import check

router = APIRouter(prefix="/api", tags=["gateway"], dependencies=[Depends(require_business)])


def _guard_out(result) -> GuardOut:
    return GuardOut(blocked=result.blocked, summary=result.summary(),
                    findings=[FindingOut(kind=f.kind, label=f.label, excerpt=f.excerpt) for f in result.findings])


@router.post("/gateway/check", response_model=GuardOut)
def gateway_check(body: GuardIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    result = check(body.text)
    audit.record(db, actor=user.username, action="guard.check", result="blocked" if result.blocked else "allowed",
                 length=len(body.text), sha=audit.digest(body.text),
                 kinds=sorted({f.kind for f in result.findings}))
    return _guard_out(result)


@router.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    project = project_for_member(db, body.project_code, user)
    actor = user.username
    meta = {"length": len(body.message), "sha": audit.digest(body.message), "level": body.level}
    try:
        out = complete(body.message, tier=project.tier, project_code=project.code, level=body.level)
    except BlockedError as e:
        audit.record(db, actor=actor, action="llm.chat", result="blocked", project_code=project.code,
                     kinds=sorted({f.kind for f in e.result.findings}), **meta)
        raise HTTPException(422, detail=_guard_out(e.result).model_dump()) from e
    except httpx.HTTPError as e:
        audit.record(db, actor=actor, action="llm.chat", result="error", project_code=project.code,
                     error=type(e).__name__, **meta)
        raise HTTPException(502, "模型服务暂时不可用，请稍后再试") from e
    audit.record(db, actor=actor, action="llm.chat", result="allowed", project_code=project.code,
                 model=out.model, demo=out.demo, **meta)
    return ChatOut(reply=out.text, model=out.model, demo=out.demo)
