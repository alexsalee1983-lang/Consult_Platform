"""项目区资料与证据编号。要求业务角色；只有项目成员能看能写，非成员一律 404。

GET  /api/projects/{code}/evidences              证据清单（D-xx 客户资料、I-xx 访谈纪要、O-xx 现场观察）
GET  /api/projects/{code}/evidences/{evidence}   证据详情：纪要 / 观察带正文；资料带解析状态和前 20 节预览
POST /api/projects/{code}/documents              上传客户资料（multipart：file，可选 title），分配 D-xx；
                                                 机密级命中 422（不保存）、重复 409、严格档 422
POST /api/projects/{code}/notes                  录入访谈纪要 / 现场观察（JSON），分配 I-xx / O-xx
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..kb import parse
from ..models import Evidence, User
from ..models.evidence import EVIDENCE_KIND_NAMES
from ..project import evidence as ev_svc

router = APIRouter(prefix="/api/projects", tags=["evidence"], dependencies=[Depends(require_business)])

PREVIEW_SECTIONS = 20


class EvidenceOut(BaseModel):
    code: str
    kind: str                   # document / interview / observation
    kind_name: str
    title: str
    interviewee_role: str
    occurred_on: date | None
    chars: int
    status: str                 # 资料：received 待解析 / parsed 已解析 / failed 解析失败；纪要和观察：ready
    filename: str               # 资料的原文件名（只在项目区接口里出现）
    error: str
    created_by: str
    created_at: datetime


class EvidenceDetail(EvidenceOut):
    content: str                # 纪要 / 观察正文
    preview: list[dict]         # 资料解析后的前 20 节 [{heading, text}]


class NoteIn(BaseModel):
    kind: str = Field(pattern="^(interview|observation)$")
    title: str = Field(min_length=1, max_length=200)
    interviewee_role: str = Field(default="", max_length=100)
    occurred_on: date | None = None
    content: str = Field(min_length=1)


def _out(e: Evidence, cls=EvidenceOut, **extra):
    d = e.document
    return cls(code=e.code, kind=e.kind, kind_name=EVIDENCE_KIND_NAMES.get(e.kind, e.kind), title=e.title,
               interviewee_role=e.interviewee_role, occurred_on=e.occurred_on,
               chars=d.char_count if d else e.char_count, status=d.status if d else "ready",
               filename=d.filename if d else "", error=d.error if d else "", created_by=e.created_by,
               created_at=e.created_at, **extra)


def _raise(e: ev_svc.EvidenceError):
    raise HTTPException(e.status, e.detail) from e


@router.get("/{code}/evidences", response_model=list[EvidenceOut])
def list_evidences(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_out(e) for e in ev_svc.list_evidences(db, p)]


@router.get("/{code}/evidences/{evidence}", response_model=EvidenceDetail)
def get_evidence(code: str, evidence: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    e = ev_svc.get_evidence(db, p, evidence)
    if e is None:
        raise HTTPException(404, "证据不存在")
    preview: list[dict] = []
    if e.document is not None:
        preview = (ev_svc.document_sections(e.document) or [])[:PREVIEW_SECTIONS]
    return _out(e, EvidenceDetail, content=e.content, preview=preview)


@router.post("/{code}/documents", response_model=EvidenceOut, status_code=201)
async def upload_document(code: str, file: UploadFile = File(...), title: str = Form(default="", max_length=200),
                          db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    data = await file.read(parse.MAX_BYTES + 1)
    if len(data) > parse.MAX_BYTES:
        raise HTTPException(413, f"文件超过 {parse.MAX_BYTES // 1024 // 1024}MB")
    try:
        r = ev_svc.upload_document(db, user, p, file.filename or "", data, file.content_type or "", title)
    except ev_svc.EvidenceError as e:
        _raise(e)
    db.refresh(r.evidence, attribute_names=["document"])
    return _out(r.evidence)


@router.post("/{code}/notes", response_model=EvidenceOut, status_code=201)
def add_note(code: str, body: NoteIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        e = ev_svc.add_note(db, user, p, body.kind, body.title, body.content, body.interviewee_role,
                            body.occurred_on)
    except ev_svc.EvidenceError as err:
        _raise(err)
    return _out(e)

