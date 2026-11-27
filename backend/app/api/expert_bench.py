"""四期 B23① 专家工作台接口（规则见 app/kb/expert_bench.py）。只给知识专家，其他角色 403。

GET  /api/expert-bench/cards                      问答卡（按影响 × 不确定度排，带每日额度）与大白话词表
GET  /api/expert-bench/templates                  按类型填空卡的格子
POST /api/expert-bench/fill                       填空拼成正文（过机密级检测），只预览不保存
POST /api/expert-bench/cards/{version_id}/answer  回答换成审核决定；不在本人卡片里的 404；
                                                  自己写的文字有可识别信息且没确认（deid_ack）409
POST /api/expert-bench/deid-preview               四期 B23②：自己写的文字先去标识给专家看，不保存
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..kb import expert_bench as eb
from ..kb import review as rv
from ..models import User

router = APIRouter(prefix="/api/expert-bench", tags=["expert-bench"], dependencies=[Depends(require_business)])


class AnswerItem(BaseModel):
    id: str = Field(max_length=40)
    choice: str = Field(max_length=8)
    value: str = Field(default="", max_length=eb.MAX_VALUE)


class AnswerIn(BaseModel):
    answers: list[AnswerItem] = Field(min_length=1, max_length=40)
    fill: dict[str, str] | None = None
    comment: str = Field(default="", max_length=1000)
    elapsed_ms: int = 0
    deid_ack: bool = False


class PreviewIn(BaseModel):
    texts: dict[str, str]


class FillIn(BaseModel):
    template: str = Field(max_length=16)
    fill: dict[str, str]


def _expert(db: Session, user: User) -> None:
    try:
        eb.require_expert(db, user)
    except rv.ReviewError as e:
        raise HTTPException(e.status, str(e)) from e


def _fill_ok(fill: dict[str, str] | None) -> dict[str, str] | None:
    if fill is not None and (len(fill) > 10 or any(len(k) > 24 or len(v) > 2000 for k, v in fill.items())):
        raise HTTPException(422, "填空内容太长")
    return fill


@router.get("/cards")
def cards(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _expert(db, user)
    return eb.cards(db, user)


@router.get("/templates")
def templates(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict]:
    _expert(db, user)
    return [{"key": k, "name": n, "slots": [{"key": s, "label": label, "hint": h, "required": r}
                                            for s, label, h, r in slots]} for k, (n, slots) in eb.TEMPLATES.items()]


@router.post("/fill")
def fill(body: FillIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _expert(db, user)
    try:
        return {"body": eb.compose(body.template, _fill_ok(body.fill) or {})}
    except rv.ReviewError as e:
        raise HTTPException(e.status, str(e)) from e


@router.post("/cards/{version_id}/answer")
def answer(version_id: int, body: AnswerIn, db: Session = Depends(get_db),
           user: User = Depends(current_user)) -> dict:
    _expert(db, user)
    try:
        return eb.answer(db, user, version_id, [a.model_dump() for a in body.answers], fill=_fill_ok(body.fill),
                         comment=body.comment, elapsed_ms=body.elapsed_ms, deid_ack=body.deid_ack)
    except rv.ReviewError as e:
        db.rollback()
        raise HTTPException(e.status, str(e)) from e


@router.post("/deid-preview")
def deid_preview(body: PreviewIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _expert(db, user)
    if len(body.texts) > eb.MAX_PREVIEW_KEYS or any(len(k) > 24 or len(v) > 2000 for k, v in body.texts.items()):
        raise HTTPException(422, "预览内容太长")
    return eb.deid_preview(body.texts)
