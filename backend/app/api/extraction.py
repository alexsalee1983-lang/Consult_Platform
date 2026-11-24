"""四期 F4③：经验萃取问题（规则见 app/project/extraction_trigger.py）。只给知识专家（其他角色 403）。

GET  /api/extraction/questions               本人待答的萃取问题（每人每周 ≤ 3 题）
POST /api/extraction/questions/{id}/answer   回答：过机密级检测后投料，进公司做法确认队列（B17）
POST /api/extraction/questions/{id}/skip     跳过
GET  /api/extraction/stats                   各状态计数与跳过率
开关 F4_REVISION_MINING 关（默认）或生产环境一律 404。别人的题、不存在的题 404；已收尾 409。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import KNOWLEDGE_EXPERT, require_any
from ..models import User
from ..project import extraction_trigger as svc
from ..project.revision_mining import enabled

router = APIRouter(prefix="/api/extraction", tags=["extraction"],
                   dependencies=[Depends(require_any(KNOWLEDGE_EXPERT))])


class AnswerIn(BaseModel):
    body: str = Field(min_length=1, max_length=svc.ANSWER_MAX)


def _on() -> None:
    if not enabled():
        raise HTTPException(404, "经验萃取未开启（只在测试机、试用机上开）")


@router.get("/questions")
def questions(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _on()
    return {"questions": svc.mine(db, user), "weekly_cap": svc.WEEKLY_CAP}


@router.post("/questions/{qid}/answer")
def answer(qid: int, data: AnswerIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _on()
    try:
        return svc.answer(db, user, qid, data.body)
    except svc.ExtractionError as e:
        raise HTTPException(e.status, str(e)) from e


@router.post("/questions/{qid}/skip")
def skip(qid: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _on()
    try:
        return svc.skip(db, user, qid)
    except svc.ExtractionError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    _on()
    return svc.stats(db)
