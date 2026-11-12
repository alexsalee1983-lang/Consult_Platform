"""质量门禁与签署（M3 ⑤）。要求业务角色；只有项目成员能看能操作，非成员一律 404。

POST /api/projects/{code}/deliverables/{dcode}/reviews            发起质量评审（202，后台执行）
GET  /api/projects/{code}/deliverables/{dcode}/reviews            评审列表（新的在前，不含问题清单）
GET  /api/projects/{code}/deliverables/{dcode}/reviews/{rid}      某次评审：结论、计数、问题清单、评审报告
GET  /api/projects/{code}/deliverables/{dcode}/signoff            签署流程：状态、门禁、最近评审、流程记录
POST /api/projects/{code}/deliverables/{dcode}/signoff/submit     顾问审核完成，提交签署（门禁不过 409）
POST /api/projects/{code}/deliverables/{dcode}/signoff/sign       项目经理签署（非项目经理 403，提交人不能自签 409）
POST /api/projects/{code}/deliverables/{dcode}/signoff/return     项目经理退回修改（必须写理由）

严格档 422。规则见 app/project/quality.py。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import QualityReview, SignOff, User
from ..project import deliverables as dsvc
from ..project import quality as svc

router = APIRouter(prefix="/api/projects", tags=["quality"], dependencies=[Depends(require_business)])


class ReviewOut(BaseModel):
    id: int
    revision: int
    versions: dict
    status: str
    conclusion: str
    conclusion_name: str
    blocking: int
    major: int
    minor: int
    stale: bool                  # 交付件在评审后有修改
    error: str
    model: str
    prompt_version: str
    created_by: str
    created_at: datetime
    finished_at: datetime | None


class ReviewDetail(ReviewOut):
    issues: list[dict]
    report: str


class SignOffOut(BaseModel):
    id: int
    action: str
    revision: int
    versions: dict
    review_id: int | None
    comment: str
    actor: str
    created_at: datetime


class SignOffState(BaseModel):
    deliverable: str
    status: str
    revision: int
    gate: bool
    reasons: list[str]
    latest_review: ReviewOut | None
    history: list[SignOffOut]


class CommentIn(BaseModel):
    comment: str = Field(default="", max_length=svc.MAX_COMMENT)


def _raise(e: Exception):
    raise HTTPException(e.status, e.detail) from e


def _load(db: Session, code: str, dcode: str, user: User):
    p = project_for_member(db, code, user)
    try:
        return p, dsvc.require_deliverable(db, p, dcode)
    except dsvc.DeliverableError as e:
        _raise(e)


def _review(r: QualityReview, revision: int) -> dict:
    return {"id": r.id, "revision": r.revision, "versions": r.versions or {}, "status": r.status,
            "conclusion": r.conclusion, "conclusion_name": svc.CONCLUSIONS.get(r.conclusion, ""),
            "blocking": r.blocking, "major": r.major, "minor": r.minor, "stale": r.revision != revision,
            "error": r.error, "model": r.model, "prompt_version": r.prompt_version, "created_by": r.created_by,
            "created_at": r.created_at, "finished_at": r.finished_at}


def _signoff(s: SignOff) -> SignOffOut:
    return SignOffOut(id=s.id, action=s.action, revision=s.revision, versions=s.versions or {}, review_id=s.review_id,
                      comment=s.comment, actor=s.actor, created_at=s.created_at)


@router.post("/{code}/deliverables/{dcode}/reviews", response_model=ReviewOut, status_code=202)
def request_review(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        r = svc.request_review(db, user, p, d)
    except svc.QualityError as e:
        _raise(e)
    return ReviewOut(**_review(r, d.revision))


@router.get("/{code}/deliverables/{dcode}/reviews", response_model=list[ReviewOut])
def list_reviews(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    return [ReviewOut(**_review(r, d.revision)) for r in svc.list_reviews(db, d)]


@router.get("/{code}/deliverables/{dcode}/reviews/{rid}", response_model=ReviewDetail)
def get_review(code: str, dcode: str, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    r = db.get(QualityReview, rid)
    if r is None or r.deliverable_id != d.id:
        raise HTTPException(404, "评审记录不存在")
    return ReviewDetail(**_review(r, d.revision), issues=r.issues or [], report=r.report)


def _state(db: Session, d) -> SignOffState:
    g = svc.gate(db, d)
    latest = svc.latest_review(db, d)
    return SignOffState(deliverable=d.code, status=d.status, revision=d.revision,
                        gate=g.ok and d.status != "signed", reasons=g.reasons,
                        latest_review=ReviewOut(**_review(latest, d.revision)) if latest else None,
                        history=[_signoff(s) for s in svc.list_signoffs(db, d)])


@router.get("/{code}/deliverables/{dcode}/signoff", response_model=SignOffState)
def signoff_state(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    return _state(db, d)


def _act(fn, code: str, dcode: str, body: "CommentIn", db: Session, user: User) -> SignOffState:
    p, d = _load(db, code, dcode, user)
    try:
        fn(db, user, p, d, body.comment)
    except svc.QualityError as e:
        _raise(e)
    db.refresh(d)
    return _state(db, d)


@router.post("/{code}/deliverables/{dcode}/signoff/submit", response_model=SignOffState)
def signoff_submit(code: str, dcode: str, body: CommentIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    return _act(svc.submit, code, dcode, body, db, user)


@router.post("/{code}/deliverables/{dcode}/signoff/sign", response_model=SignOffState)
def signoff_sign(code: str, dcode: str, body: CommentIn, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    return _act(svc.sign, code, dcode, body, db, user)


@router.post("/{code}/deliverables/{dcode}/signoff/return", response_model=SignOffState)
def signoff_return(code: str, dcode: str, body: CommentIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    return _act(svc.send_back, code, dcode, body, db, user)
