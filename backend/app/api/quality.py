"""质量门禁与签署（M3 ⑤）。要求业务角色；只有项目成员能看能操作，非成员一律 404。

POST /api/projects/{code}/deliverables/{dcode}/reviews            发起质量评审（202，后台执行）
GET  /api/projects/{code}/deliverables/{dcode}/reviews            评审列表（新的在前，不含问题清单）
GET  /api/projects/{code}/deliverables/{dcode}/reviews/{rid}      某次评审：结论、计数、问题清单、评审报告、驳回记录
POST /api/projects/{code}/deliverables/{dcode}/reviews/{rid}/waivers  项目经理驳回评审 Agent 提出的一条阻断（必须写理由）
GET  /api/projects/{code}/deliverables/{dcode}/signoff            签署流程：状态、门禁、最近评审、流程记录
POST /api/projects/{code}/deliverables/{dcode}/signoff/submit     顾问审核完成，提交签署（门禁不过 409）
POST /api/projects/{code}/deliverables/{dcode}/signoff/sign       项目经理签署（非项目经理 403）
POST /api/projects/{code}/deliverables/{dcode}/signoff/return     项目经理退回修改（必须写理由）

四期 A26：
GET  /api/projects/{code}/deliverables/{dcode}/revise             一键修订状态（最近一次任务、剩余机械问题、是否转人工）
POST /api/projects/{code}/deliverables/{dcode}/revise             一键修订（202，后台执行，最多 REVISE_MAX_ROUNDS 轮）
POST /api/projects/{code}/deliverables/{dcode}/client-feedback    录入客户意见（每条一张确认卡）
POST /api/projects/{code}/deliverables/{dcode}/signoff/merged     核对与签署合并（D3 门禁 0 问题，项目经理）
GET  /api/deliverables/batch-sign                                本人任经理的项目里待签的 D3 及能否签
POST /api/deliverables/batch-sign                                批量签（逐份判、逐份审计）
签署接口另按写签分离阻断（对客户交付件，起草人、最后修改人不能签，403）。
严格档 422。规则见 app/project/quality.py、app/project/revise.py。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import QualityReview, ReviewWaiver, SignOff, User
from ..project import confirmations as conf_svc
from ..project import deliverables as dsvc
from ..project import quality as svc
from ..project import revise as revise_svc

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
    open_blocking: int           # 未驳回的阻断（门禁按它判断）
    waived: int                  # 已驳回的阻断
    stale: bool                  # 交付件在评审后有修改
    error: str
    model: str
    prompt_version: str
    created_by: str
    created_at: datetime
    finished_at: datetime | None


class WaiverOut(BaseModel):
    issue: int                   # 问题清单下标
    reason: str
    actor: str
    created_at: datetime


class ReviewDetail(ReviewOut):
    issues: list[dict]           # 每条另有 waivable：评审 Agent 提出的阻断，项目经理可以驳回
    report: str
    waivers: list[WaiverOut]


class WaiveIn(BaseModel):
    issue: int = Field(ge=0)
    reason: str = Field(max_length=svc.MAX_COMMENT)


class SignOffOut(BaseModel):
    id: int
    action: str
    revision: int
    versions: dict
    review_id: int | None
    waived: int                  # 依据的评审中被驳回的阻断条数
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
    # 四期 A26
    drafters: list[str] = []          # 起草人（各章第 1 版作者）
    last_modifiers: list[str] = []    # 最后修改人（各章当前版本作者）
    separation_exempt: bool = False   # 写签分离豁免（对内件，默认 D3）
    sign_block: str = ""              # 当前用户因写签分离不能签的原因；空 = 无冲突
    merged: bool = False              # 走核对与签署合并（D3）
    merged_ready: bool = False        # 现在就能一步签


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


def _review(db: Session, r: QualityReview, revision: int) -> dict:
    b = svc.blocking_of(db, r)
    return {"open_blocking": b.remaining, "waived": b.waived, "id": r.id, "revision": r.revision, "versions": r.versions or {}, "status": r.status,
            "conclusion": r.conclusion, "conclusion_name": svc.CONCLUSIONS.get(r.conclusion, ""),
            "blocking": r.blocking, "major": r.major, "minor": r.minor, "stale": r.revision != revision,
            "error": r.error, "model": r.model, "prompt_version": r.prompt_version, "created_by": r.created_by,
            "created_at": r.created_at, "finished_at": r.finished_at}


def _signoff(db: Session, s: SignOff) -> SignOffOut:
    r = db.get(QualityReview, s.review_id) if s.review_id else None
    return SignOffOut(id=s.id, action=s.action, revision=s.revision, versions=s.versions or {}, review_id=s.review_id,
                      waived=svc.blocking_of(db, r).waived if r else 0,
                      comment=s.comment, actor=s.actor, created_at=s.created_at)


def _detail(db: Session, r: QualityReview, revision: int) -> ReviewDetail:
    issues = [{**i, "waivable": svc.waivable(i)} for i in r.issues or []]
    return ReviewDetail(**_review(db, r, revision), issues=issues, report=r.report,
                        waivers=[_waiver(w) for w in svc.list_waivers(db, r)])


def _waiver(w: ReviewWaiver) -> WaiverOut:
    return WaiverOut(issue=w.issue_index, reason=w.reason, actor=w.actor, created_at=w.created_at)


@router.post("/{code}/deliverables/{dcode}/reviews", response_model=ReviewOut, status_code=202)
def request_review(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        r = svc.request_review(db, user, p, d)
    except svc.QualityError as e:
        _raise(e)
    return ReviewOut(**_review(db, r, d.revision))


@router.get("/{code}/deliverables/{dcode}/reviews", response_model=list[ReviewOut])
def list_reviews(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    return [ReviewOut(**_review(db, r, d.revision)) for r in svc.list_reviews(db, d)]


@router.get("/{code}/deliverables/{dcode}/reviews/{rid}", response_model=ReviewDetail)
def get_review(code: str, dcode: str, rid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    r = db.get(QualityReview, rid)
    if r is None or r.deliverable_id != d.id:
        raise HTTPException(404, "评审记录不存在")
    return _detail(db, r, d.revision)


@router.post("/{code}/deliverables/{dcode}/reviews/{rid}/waivers", response_model=ReviewDetail)
def waive_issue(code: str, dcode: str, rid: int, body: WaiveIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    r = db.get(QualityReview, rid)
    if r is None or r.deliverable_id != d.id:
        raise HTTPException(404, "评审记录不存在")
    try:
        svc.waive(db, user, p, d, r, body.issue, body.reason)
    except svc.QualityError as e:
        _raise(e)
    db.refresh(d)
    return _detail(db, r, d.revision)


def _state(db: Session, d, user: User) -> SignOffState:
    g = svc.gate(db, d)
    latest = svc.latest_review(db, d)
    drafters, last = svc.writers(d)
    merged = svc.merged_kind(d)
    return SignOffState(deliverable=d.code, status=d.status, revision=d.revision,
                        gate=g.ok and d.status != "signed", reasons=g.reasons,
                        latest_review=ReviewOut(**_review(db, latest, d.revision)) if latest else None,
                        history=[_signoff(db, s) for s in svc.list_signoffs(db, d)],
                        drafters=sorted(drafters), last_modifiers=sorted(last),
                        separation_exempt=svc.separation_exempt(d),
                        sign_block=svc.separation_conflict(d, user.username), merged=merged,
                        merged_ready=merged and not svc.merged_check(db, d))


@router.get("/{code}/deliverables/{dcode}/signoff", response_model=SignOffState)
def signoff_state(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    return _state(db, d, user)


def _act(fn, code: str, dcode: str, body: "CommentIn", db: Session, user: User) -> SignOffState:
    p, d = _load(db, code, dcode, user)
    try:
        fn(db, user, p, d, body.comment)
    except svc.QualityError as e:
        _raise(e)
    db.refresh(d)
    return _state(db, d, user)


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


@router.post("/{code}/deliverables/{dcode}/signoff/merged", response_model=SignOffState)
def signoff_merged(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        svc.sign_merged(db, user, p, d)
    except svc.QualityError as e:
        _raise(e)
    db.refresh(d)
    return _state(db, d, user)


# ---- 一键修订、客户意见（四期 A26）----

class ReviseState(BaseModel):
    status: str                  # 最近一次修订任务：queued / running / done / failed；没修订过为空
    trigger: str
    created_at: datetime | None
    finished_at: datetime | None
    error: str
    max_rounds: int
    mechanical_open: int         # 当前评审里还没修的机械问题
    pending_feedback: int        # 已采纳、还没修订的客户意见
    handoff: bool                # 自动修订已结束、仍有机械问题：转人工


@router.get("/{code}/deliverables/{dcode}/revise", response_model=ReviseState)
def revise_state(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    return ReviseState(**revise_svc.state(db, p, d))


@router.post("/{code}/deliverables/{dcode}/revise", response_model=ReviseState, status_code=202)
def revise_start(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        revise_svc.start(db, user, p, d)
    except revise_svc.RevisionError as e:
        _raise(e)
    db.refresh(d)
    return ReviseState(**revise_svc.state(db, p, d))


class FeedbackItem(BaseModel):
    section: int | None = Field(default=None, ge=1)
    text: str = Field(max_length=conf_svc.MAX_TEXT)


class FeedbackIn(BaseModel):
    items: list[FeedbackItem] = Field(max_length=conf_svc.MAX_FEEDBACK_ITEMS)


@router.post("/{code}/deliverables/{dcode}/client-feedback", status_code=201)
def client_feedback(code: str, dcode: str, body: FeedbackIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)
    try:
        cards = conf_svc.create_feedback(db, user, p, d, [(i.section, i.text) for i in body.items])
    except conf_svc.ConfirmationError as e:
        _raise(e)
    return {"confirmations": [c.id for c in cards]}


# ---- D3 批量签（四期 A26）：跨项目，路径不在 /api/projects 下 ----

batch_router = APIRouter(prefix="/api/deliverables", tags=["quality"], dependencies=[Depends(require_business)])


class BatchItem(BaseModel):
    project: str = Field(max_length=32)
    deliverable: str = Field(default="D3", max_length=8)


class BatchIn(BaseModel):
    items: list[BatchItem] = Field(min_length=1, max_length=50)


@batch_router.get("/batch-sign")
def batch_candidates(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict]:
    return svc.batch_candidates(db, user)


@batch_router.post("/batch-sign")
def batch_sign(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict]:
    return svc.batch_sign(db, user, [(i.project, i.deliverable.upper()) for i in body.items])
