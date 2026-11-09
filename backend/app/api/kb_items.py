"""知识条目与审批接口。要求业务角色；编辑、提交、审核只限知识专家、知识管理员（规则见 app/kb/review.py）。

GET  /api/kb/items                       条目列表（筛选：layer、product、stage、doc_type、status、q）
POST /api/kb/items                       新建条目和第 1 版草稿（从去标识通过的投料，或公开资料直接填写）
GET  /api/kb/items/{doc_id}              条目详情：元数据、现行版本、版本历史
POST /api/kb/items/{doc_id}/versions     基于现行版本建新草稿（修订）
POST /api/kb/items/{doc_id}/retire       下线（知识管理员）
POST /api/kb/items/{doc_id}/reconfirm    复审确认仍有效，复审日期顺延一个周期（知识专家）
GET  /api/kb/versions/{id}               版本详情：正文、审核记录、与现行版本的差异、相似条目
PATCH /api/kb/versions/{id}              修改草稿
POST /api/kb/versions/{id}/submit        提交待审
POST /api/kb/versions/{id}/review        审核：role=content|compliance，decision=approve|reject
GET  /api/kb/review-queue                待审版本（知识专家、知识管理员）
GET  /api/kb/review-due                  30 天内到期待复审的条目（知识专家、知识管理员）

普通顾问只能看到已发布、未下线的现行版本；看不到的条目和版本一律 404。
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import require_business
from ..kb import review as rv
from ..kb.items import ItemMeta, VersionContent
from ..models import KbItem, KbVersion, User

router = APIRouter(prefix="/api/kb", tags=["kb"], dependencies=[Depends(require_business)])

NOT_FOUND = "知识条目不存在"


class ItemMetaIn(BaseModel):
    layer: str
    domain: str
    product: str
    doc_type: str
    stage: str
    capabilities: list[str]
    industries: list[str] = Field(default_factory=lambda: ["通用"])
    classification: str = "internal"
    visible_roles: list[str] = Field(default_factory=lambda: ["consultant", "admin"])
    owner: str = ""


class ContentIn(BaseModel):
    title: str = Field(max_length=200)
    body: str = Field(default="", max_length=500_000)
    source_type: str
    source_detail: str = Field(default="", max_length=300)
    summary: str = Field(default="", max_length=1000)
    applicability: str = Field(default="", max_length=500)
    change_note: str = Field(default="", max_length=1000)
    ref_document: str = Field(default="", max_length=200)
    ref_version: str = Field(default="", max_length=64)
    ref_status: str = Field(default="", max_length=16)
    effective_date: date | None = None
    review_date: date | None = None


class NewItemIn(BaseModel):
    meta: ItemMetaIn
    content: ContentIn
    upload_id: int | None = None


class NewVersionIn(BaseModel):
    content: ContentIn
    upload_id: int | None = None


class EditIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=500_000)
    summary: str | None = Field(default=None, max_length=1000)
    applicability: str | None = Field(default=None, max_length=500)
    change_note: str | None = Field(default=None, max_length=1000)
    source_detail: str | None = Field(default=None, max_length=300)
    ref_document: str | None = Field(default=None, max_length=200)
    ref_version: str | None = Field(default=None, max_length=64)
    ref_status: str | None = Field(default=None, max_length=16)
    effective_date: date | None = None
    review_date: date | None = None


class ReviewIn(BaseModel):
    role: str = Field(pattern="^(content|compliance)$")
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="", max_length=1000)


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class VersionBrief(BaseModel):
    id: int
    version: int
    status: str
    title: str
    created_by: str
    created_at: datetime
    submitted_by: str
    published_at: datetime | None
    review_date: date | None


class ReviewOut(BaseModel):
    reviewer: str
    review_role: str
    decision: str
    comment: str
    created_at: datetime


class VersionOut(VersionBrief):
    doc_id: str
    summary: str
    body: str
    applicability: str
    change_note: str
    source_type: str
    source_detail: str
    ref_document: str
    ref_version: str
    ref_status: str
    deid_status: str
    effective_date: date | None
    reviews: list[ReviewOut] = []
    diff: list[str] = []
    similar: list[dict] = []


class ItemOut(BaseModel):
    doc_id: str
    layer: str
    domain: str
    product: str
    doc_type: str
    stage: str
    capabilities: list[str]
    industries: list[str]
    classification: str
    visible_roles: list[str]
    owner: str
    retired: bool
    title: str = ""
    current: VersionBrief | None = None


class ItemDetail(ItemOut):
    versions: list[VersionBrief] = []


def _err(e: rv.ReviewError):
    return HTTPException(e.status, str(e))


def _brief(v: KbVersion) -> VersionBrief:
    return VersionBrief.model_validate(v, from_attributes=True)


def _item_out(db: Session, item: KbItem, cls=ItemOut, versions: list[KbVersion] | None = None):
    cur = db.get(KbVersion, item.current_version_id) if item.current_version_id else None
    latest = cur or (item.versions[-1] if item.versions else None)
    data = {c: getattr(item, c) for c in ItemOut.model_fields if hasattr(item, c) and c not in ("current", "title")}
    out = cls(**data, title=latest.title if latest else "", current=_brief(cur) if cur else None)
    if versions is not None:
        out.versions = [_brief(v) for v in versions]
    return out


def _item(db: Session, doc_id: str, user: User) -> KbItem:
    item = db.scalar(select(KbItem).where(KbItem.doc_id == doc_id))
    if item is None or not rv.can_view(db, user, item):
        raise HTTPException(404, NOT_FOUND)
    return item


def _version(db: Session, version_id: int, user: User) -> KbVersion:
    v = db.get(KbVersion, version_id)
    if v is None or not rv.can_view(db, user, v.item, v):
        raise HTTPException(404, NOT_FOUND)
    return v


def _version_out(db: Session, v: KbVersion, user: User) -> VersionOut:
    out = VersionOut(**_brief(v).model_dump(), doc_id=v.item.doc_id,
                     **{k: getattr(v, k) for k in ("summary", "body", "applicability", "change_note", "source_type",
                                                   "source_detail", "ref_document", "ref_version", "ref_status",
                                                   "deid_status", "effective_date")})
    if rv.is_knowledge_user(db, user):          # 审核信息只给知识侧角色
        out.reviews = [ReviewOut.model_validate(r, from_attributes=True) for r in v.reviews]
        out.diff = rv.diff_with_current(db, v)
        out.similar = rv.similar_items(db, v) if v.status in ("draft", "in_review") else []
    return out


@router.get("/items", response_model=list[ItemOut])
def list_items(layer: str = "", product: str = "", stage: str = "", doc_type: str = "", status: str = "",
               q: str = Query("", max_length=100), db: Session = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(KbItem).order_by(KbItem.doc_id).limit(500)
    for col, val in ((KbItem.layer, layer), (KbItem.product, product), (KbItem.stage, stage),
                     (KbItem.doc_type, doc_type)):
        if val:
            stmt = stmt.where(col == val)
    if status == "retired":
        stmt = stmt.where(KbItem.retired.is_(True))
    elif status == "published":
        stmt = stmt.where(KbItem.current_version_id.is_not(None), KbItem.retired.is_(False))
    elif status == "unpublished":
        stmt = stmt.where(KbItem.current_version_id.is_(None))
    if q:
        stmt = stmt.join(KbVersion, KbVersion.item_id == KbItem.id).where(
            or_(KbItem.doc_id.contains(q), KbVersion.title.contains(q))).distinct()
    return [_item_out(db, i) for i in db.scalars(stmt) if rv.can_view(db, user, i)]


@router.post("/items", response_model=VersionOut, status_code=201)
def create(body: NewItemIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        v = rv.new_item(db, user, ItemMeta(**body.meta.model_dump()), VersionContent(**body.content.model_dump()),
                        body.upload_id)
    except rv.ReviewError as e:
        raise _err(e) from e
    return _version_out(db, v, user)


@router.get("/items/{doc_id}", response_model=ItemDetail)
def get_item(doc_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _item(db, doc_id, user)
    versions = item.versions if rv.is_knowledge_user(db, user) else [
        v for v in item.versions if v.id == item.current_version_id]
    return _item_out(db, item, ItemDetail, versions)


@router.post("/items/{doc_id}/versions", response_model=VersionOut, status_code=201)
def revise(doc_id: str, body: NewVersionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = _item(db, doc_id, user)
    try:
        v = rv.new_version(db, user, item, VersionContent(**body.content.model_dump()), body.upload_id)
    except rv.ReviewError as e:
        raise _err(e) from e
    return _version_out(db, v, user)


@router.post("/items/{doc_id}/retire", response_model=ItemOut)
def retire(doc_id: str, body: ReasonIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        return _item_out(db, rv.retire(db, user, _item(db, doc_id, user), body.reason))
    except rv.ReviewError as e:
        raise _err(e) from e


@router.post("/items/{doc_id}/reconfirm", response_model=VersionBrief)
def reconfirm(doc_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        return _brief(rv.reconfirm(db, user, _item(db, doc_id, user)))
    except rv.ReviewError as e:
        raise _err(e) from e


@router.get("/versions/{version_id}", response_model=VersionOut)
def get_version(version_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _version_out(db, _version(db, version_id, user), user)


@router.patch("/versions/{version_id}", response_model=VersionOut)
def edit(version_id: int, body: EditIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        v = rv.edit_draft(db, user, _version(db, version_id, user), body.model_dump(exclude_unset=True))
    except rv.ReviewError as e:
        raise _err(e) from e
    return _version_out(db, v, user)


@router.post("/versions/{version_id}/submit", response_model=VersionOut)
def submit(version_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        v = rv.submit(db, user, _version(db, version_id, user))
    except rv.ReviewError as e:
        raise _err(e) from e
    return _version_out(db, v, user)


@router.post("/versions/{version_id}/review", response_model=VersionOut)
def do_review(version_id: int, body: ReviewIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        v = rv.review(db, user, _version(db, version_id, user), body.role, body.decision, body.comment)
    except rv.ReviewError as e:
        raise _err(e) from e
    return _version_out(db, v, user)


def _knowledge_only(db: Session, user: User) -> None:
    if not rv.is_knowledge_user(db, user):
        raise HTTPException(403, "只有知识专家、知识管理员能查看")


@router.get("/review-queue", response_model=list[VersionOut])
def review_queue(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _knowledge_only(db, user)
    rows = db.scalars(select(KbVersion).where(KbVersion.status == "in_review").order_by(KbVersion.submitted_at))
    return [_version_out(db, v, user) for v in rows]


@router.get("/review-due", response_model=list[VersionOut])
def review_due(days: int = Query(30, ge=0, le=366), db: Session = Depends(get_db), user: User = Depends(current_user)):
    _knowledge_only(db, user)
    return [_version_out(db, v, user) for v in rv.review_due(db, days)]
