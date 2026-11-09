"""知识投料接口（投料箱）。要求业务角色。

POST /api/kb/intake          上传一个文件（multipart，字段名 file），返回投料记录；机密级命中 422、重复 409
GET  /api/kb/intake          投料列表：知识专家、知识管理员看全部，其他人只看自己上传的
GET  /api/kb/intake/{id}     投料详情，解析完成后附前 20 个片段预览；看不到的一律 404

顾问也可以投料（架构设计：交付顾问“可以投料和反馈”）。投料只是原料，进入知识库还要去标识和双人审核。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import KNOWLEDGE_ADMIN, KNOWLEDGE_EXPERT, require_business, roles_of
from ..kb import intake, parse
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
async def submit(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = await file.read(parse.MAX_BYTES + 1)
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
    parsed = intake.parsed_result(up) if up.status == "parsed" else None
    preview = (parsed or {}).get("chunks", [])[:PREVIEW_CHUNKS]
    return UploadDetail(**_out(up).model_dump(), preview=preview)
