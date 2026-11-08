import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import get_db
from ..models import AuditLog, Project
from ..services.gateway import BlockedError, complete
from ..services.guard import check
from ..schemas import AuditOut, ChatIn, ChatOut, FindingOut, GuardIn, GuardOut

router = APIRouter(prefix="/api", tags=["gateway"])


def _guard_out(result) -> GuardOut:
    return GuardOut(blocked=result.blocked, summary=result.summary(),
                    findings=[FindingOut(kind=f.kind, label=f.label, excerpt=f.excerpt) for f in result.findings])


@router.post("/gateway/check", response_model=GuardOut)
def gateway_check(body: GuardIn, db: Session = Depends(get_db),
                  x_user: str = Header(default="dev", alias="X-User")):
    result = check(body.text)
    audit.record(db, actor=x_user, action="guard.check", result="blocked" if result.blocked else "allowed",
                 length=len(body.text), sha=audit.digest(body.text),
                 kinds=sorted({f.kind for f in result.findings}))
    return _guard_out(result)


@router.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, db: Session = Depends(get_db),
         x_user: str = Header(default="dev", alias="X-User")):
    project = db.scalar(select(Project).where(Project.code == body.project_code))
    if project is None:
        raise HTTPException(404, "项目不存在")
    meta = {"length": len(body.message), "sha": audit.digest(body.message), "level": body.level}
    try:
        out = complete(body.message, tier=project.tier, project_code=project.code, level=body.level)
    except BlockedError as e:
        audit.record(db, actor=x_user, action="llm.chat", result="blocked", project_code=project.code,
                     kinds=sorted({f.kind for f in e.result.findings}), **meta)
        raise HTTPException(422, detail=_guard_out(e.result).model_dump()) from e
    except httpx.HTTPError as e:
        audit.record(db, actor=x_user, action="llm.chat", result="error", project_code=project.code,
                     error=type(e).__name__, **meta)
        raise HTTPException(502, "模型服务暂时不可用，请稍后再试") from e
    audit.record(db, actor=x_user, action="llm.chat", result="allowed", project_code=project.code,
                 model=out.model, demo=out.demo, **meta)
    return ChatOut(reply=out.text, model=out.model, demo=out.demo)


@router.get("/audit", response_model=list[AuditOut])
def recent_audit(limit: int = 50, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 500))
    return db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)).all()
