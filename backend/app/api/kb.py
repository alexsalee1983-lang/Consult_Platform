"""知识投料接口（投料箱）。要求业务角色。

POST /api/kb/intake          上传一个文件（multipart，字段名 file），返回投料记录；机密级命中 422、重复 409
GET  /api/kb/intake          投料列表：知识专家、知识管理员看全部，其他人只看自己上传的
GET  /api/kb/intake/{id}     投料详情，附前 20 个片段预览（去标识通过后为去标识后的内容）；看不到的一律 404
GET  /api/kb/intake/{id}/deid           去标识报告（不含原文）+ 去标识后预览
POST /api/kb/intake/{id}/deid/rerun     带补充词重跑去标识（知识专家、知识管理员；补充词不保存）
POST /api/kb/intake/{id}/deid/decision  确认去标识结果：pass / reject（知识管理员，不能是上传者本人）

顾问也可以投料（架构设计：交付顾问“可以投料和反馈”）。投料只是原料，进入知识库还要去标识和双人审核。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, require_business, roles_of
from ..kb import deid, deid_flow, intake, parse
from ..models import KbUpload, User

router = APIRouter(prefix="/api/kb", tags=["kb"], dependencies=[Depends(require_business)])

PREVIEW_CHUNKS = 20
NOT_FOUND = "投料记录不存在"


class UploadOut(BaseModel):
    id: int
    filename: str
    kind: str
    size: int
    status: str             # received 待解析 / parsed 已解析 / failed 解析失败
    error: str
    page_count: int
    section_count: int
    chunk_count: int
    char_count: int
    uploaded_by: str
    created_at: datetime
    parsed_at: datetime | None


class UploadDetail(UploadOut):
    preview: list[dict]     # [{heading, text}]，前 20 个片段


def _sees_all(db: Session, user: User) -> bool:
    return bool(roles_of(db, user.id) & {KNOWLEDGE_EXPERT, KNOWLEDGE_ADMIN})


def _out(up: KbUpload) -> UploadOut:
    return UploadOut.model_validate(up, from_attributes=True)


@router.post("/intake", response_model=UploadOut, status_code=201)
def submit(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = file.file.read(parse.MAX_BYTES + 1)   # 同步端点在线程池执行，不阻塞事件循环
    if len(data) > parse.MAX_BYTES:
        raise HTTPException(413, f"文件超过 {parse.MAX_BYTES // 1024 // 1024}MB")
    try:
        r = intake.submit(db, user, file.filename or "", data, file.content_type or "")
    except intake.IntakeError as e:
        raise HTTPException(e.status, e.detail) from e
    return _out(r.upload)


@router.get("/intake", response_model=list[UploadOut])
def list_uploads(db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(KbUpload).order_by(KbUpload.id.desc()).limit(200)
    if not _sees_all(db, user):
        q = q.where(KbUpload.uploaded_by == user.username)
    return [_out(u) for u in db.scalars(q)]


@router.get("/intake/{upload_id}", response_model=UploadDetail)
def get_upload(upload_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = db.get(KbUpload, upload_id)
    if up is None or (up.uploaded_by != user.username and not _sees_all(db, user)):
        raise HTTPException(404, NOT_FOUND)
    if up.status.startswith("deid_"):
        report = deid_flow.latest_report(db, up.id)
        preview = deid_flow.preview(report, PREVIEW_CHUNKS) if report else []
    elif up.status == "parsed":
        preview = (intake.parsed_result(up) or {}).get("chunks", [])[:PREVIEW_CHUNKS]
    else:
        preview = []
    return UploadDetail(**_out(up).model_dump(), preview=preview)


# ---- 去标识 ----

class DeidReportOut(BaseModel):
    """去标识审查报告：只有类别、占位符、次数和章节位置，没有原文。"""
    id: int
    status: str
    total: int
    replacements: list[dict]
    agent_status: str
    agent_note: str
    conclusion: str
    risk_level: str
    combination_risks: list[dict]
    manual_items: list[str]
    safe_filename: str
    created_by: str
    created_at: datetime
    reviewed_by: str
    reviewed_at: datetime | None
    comment: str
    preview: list[dict] = []


class TermIn(BaseModel):
    term: str = Field(min_length=1, max_length=100)
    category: str = Field(max_length=16)


class RerunIn(BaseModel):
    extra_terms: list[TermIn] = Field(default_factory=list, max_length=50)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(pass|reject)$")
    comment: str = Field(default="", max_length=1000)


def _visible_upload(db: Session, upload_id: int, user: User) -> KbUpload:
    up = db.get(KbUpload, upload_id)
    if up is None or (up.uploaded_by != user.username and not _sees_all(db, user)):
        raise HTTPException(404, NOT_FOUND)
    return up


def _report_out(report) -> DeidReportOut:
    out = DeidReportOut.model_validate(report, from_attributes=True)
    out.preview = deid_flow.preview(report, PREVIEW_CHUNKS)
    return out


@router.get("/intake/{upload_id}/deid", response_model=DeidReportOut)
def get_deid(upload_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = _visible_upload(db, upload_id, user)
    report = deid_flow.latest_report(db, up.id)
    if report is None:
        raise HTTPException(404, "还没有去标识报告（解析或去标识仍在进行）")
    return _report_out(report)


@router.post("/intake/{upload_id}/deid/rerun", response_model=DeidReportOut)
def rerun_deid(upload_id: int, body: RerunIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    up = _visible_upload(db, upload_id, user)
    if not _sees_all(db, user):
        raise HTTPException(403, "只有知识专家、知识管理员能重跑去标识")
    try:
        terms = [deid.Term(t.term.strip(), t.category) for t in body.extra_terms]
        report = deid_flow.run(db, up, user.username, terms)
    except ValueError as e:
        status = e.status if isinstance(e, deid_flow.DeidFlowError) else 422
        raise HTTPException(status, str(e)) from e
    return _report_out(report)


@router.post("/intake/{upload_id}/deid/decision", response_model=DeidReportOut)
def decide_deid(upload_id: int, body: DecisionIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    up = _visible_upload(db, upload_id, user)
    try:
        report = deid_flow.decide(db, up, user, body.decision, body.comment)
    except deid_flow.DeidFlowError as e:
        raise HTTPException(e.status, str(e)) from e
    return _report_out(report)
