"""四期 B23① 专家工作台接口（规则见 app/kb/expert_bench.py）。只给知识专家，其他角色 403。

GET  /api/expert-bench/cards                      问答卡（按影响 × 不确定度排，带每日额度）与大白话词表
GET  /api/expert-bench/templates                  按类型填空卡的格子
POST /api/expert-bench/fill                       填空拼成正文（过机密级检测），只预览不保存
POST /api/expert-bench/cards/{version_id}/answer  回答换成审核决定；不在本人卡片里的 404；
                                                  自己写的文字有可识别信息且没确认（deid_ack）409
POST /api/expert-bench/deid-preview               四期 B23②：自己写的文字先去标识给专家看，不保存
四期 B44（规则见 app/kb/expert_ux.py）：
GET  /api/expert-bench/compare/{version_id}       对照审核：引用与依据原文并排、出入处分段、Agent 意见与存疑处
POST /api/expert-bench/cards/{version_id}/revise  修改后认可：另建专家版本并以本人署名认可发布；不在本人卡里 404
GET  /api/expert-bench/groups                     按产品 × 章节分组，按认可后知识健康提升排序
POST /api/expert-bench/batch-approve              同组成批认可（≤50，有存疑的跳过）；不在本人卡里 404，跨组 422
GET  /api/expert-bench/profile                    我的专家档案、各专家待审量、可选产品与行业
PUT  /api/expert-bench/profile                    改我的专家档案（擅长产品、行业），用于按专长派卡
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..kb import expert_bench as eb
from ..kb import expert_ux as ux
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


class ReviseIn(BaseModel):
    body: str = Field(min_length=1, max_length=50_000)
    comment: str = Field(default="", max_length=1000)
    elapsed_ms: int = 0
    deid_ack: bool = False


class BatchIn(BaseModel):
    version_ids: list[int] = Field(min_length=1, max_length=ux.BATCH_MAX)
    comment: str = Field(default="", max_length=1000)
    elapsed_ms: int = 0


class ProfileIn(BaseModel):
    products: list[str] = Field(default_factory=list, max_length=10)
    industries: list[str] = Field(default_factory=list, max_length=10)


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


def _run(db: Session, fn, *args, **kwargs):
    try:
        return fn(db, *args, **kwargs)
    except rv.ReviewError as e:
        db.rollback()
        raise HTTPException(e.status, str(e)) from e


@router.get("/compare/{version_id}")
def compare(version_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _run(db, ux.compare, user, version_id)


@router.post("/cards/{version_id}/revise")
def revise(version_id: int, body: ReviseIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _run(db, ux.revise, user, version_id, body.body, comment=body.comment, elapsed_ms=body.elapsed_ms,
                deid_ack=body.deid_ack)


@router.get("/groups")
def groups(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _run(db, ux.groups, user)


@router.post("/batch-approve")
def batch_approve(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _run(db, ux.batch_approve, user, body.version_ids, comment=body.comment, elapsed_ms=body.elapsed_ms)


@router.get("/profile")
def profile(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _run(db, ux.profile_view, user)


@router.put("/profile")
def set_profile(body: ProfileIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return _run(db, ux.set_profile, user, body.products, body.industries)
