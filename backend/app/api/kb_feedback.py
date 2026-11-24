"""知识使用反馈接口（三期 B29 最小版）。规则在 app/kb/feedback.py。

- GET  /api/projects/{code}/kb-feedback：某处引用（章节版本或 Agent 回复）里能标的知识、本人的选择，
  以及每条知识的标题、版本、依据和权威等级（四期 A17，不含正文）；
- POST /api/projects/{code}/kb-feedback：标“有用 / 过时 / 不对”（可附一句话）；
- GET  /api/kb-usage：每条知识的引用次数、采纳率和三键计数（与 B4 引用计数一样只给知识专家、知识管理员；
  只有编号、标题和计数，不含项目、正文和附言）。
项目内接口只给项目成员（非成员 404）；对话回复只给对话发起人。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, project_for_member, require_any, require_business
from ..kb import feedback as svc
from ..models import Project, User
from ..project import deliverables as dl

router = APIRouter(prefix="/api", tags=["kb-feedback"], dependencies=[Depends(require_business)])


class FeedbackIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=32)
    rating: str = Field(max_length=8)
    note: str = Field(default="", max_length=400)
    message_id: int | None = None          # 顾问对话：Agent 回复的消息 id
    deliverable: str = Field(default="", max_length=8)   # 交付件审阅：交付件编号 + 章节号 + 版本号
    seq: int | None = None
    version: int | None = None


class TargetOut(BaseModel):
    surface: str
    doc_ids: list[str]
    mine: dict[str, str]
    items: list[dict] = []          # A17：每条知识的标题、版本、依据、权威等级、待复查（不含正文）


class FeedbackOut(TargetOut):
    recheck_marked: bool


def _target(db: Session, p: Project, user: User, message_id: int | None, deliverable: str, seq: int | None,
            version: int | None) -> svc.Target:
    try:
        if message_id is not None:
            return svc.message_target(db, p, user, message_id)
        if not deliverable or seq is None or version is None:
            raise HTTPException(422, "要给出对话回复，或交付件、章节号和版本号")
        s = dl.require_section(dl.require_deliverable(db, p, deliverable), seq)
        sv = dl.get_version(s, version)
        if sv is None:
            raise HTTPException(404, "版本不存在")
        return svc.section_target(sv)
    except (svc.FeedbackError, dl.DeliverableError) as e:
        raise HTTPException(e.status, str(e)) from None


def _out(db: Session, user: User, t: svc.Target) -> dict:
    return {"surface": t.surface, "doc_ids": sorted(t.doc_ids), "mine": svc.mine(db, user, t),
            "items": svc.describe(db, user, t.doc_ids)}


@router.get("/projects/{code}/kb-feedback", response_model=TargetOut)
def get_feedback(code: str, message_id: int | None = Query(default=None),
                 deliverable: str = Query(default="", max_length=8), seq: int | None = Query(default=None),
                 version: int | None = Query(default=None), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return _out(db, user, _target(db, p, user, message_id, deliverable, seq, version))


@router.post("/projects/{code}/kb-feedback", response_model=FeedbackOut)
def post_feedback(code: str, body: FeedbackIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    t = _target(db, p, user, body.message_id, body.deliverable, body.seq, body.version)
    try:
        _, marked = svc.submit(db, p, user, t, body.doc_id, body.rating, body.note)
    except svc.FeedbackError as e:
        raise HTTPException(e.status, str(e)) from None
    return {**_out(db, user, t), "recheck_marked": marked}


@router.get("/kb-usage", dependencies=[Depends(require_any(KNOWLEDGE_EXPERT, KNOWLEDGE_ADMIN))])
def kb_usage(days: int = Query(default=svc.STATS_DAYS, ge=1, le=365), db: Session = Depends(get_db)):
    return svc.stats(db, days)
