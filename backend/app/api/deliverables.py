"""交付件分章节与版本（M3）。要求业务角色；只有项目成员能看能写，非成员一律 404。

GET  /api/projects/{code}/deliverables/{dcode}                          交付件概览：大纲、各章当前版本、来源计数
PUT  /api/projects/{code}/deliverables/{dcode}/outline                  设置大纲（完整的章节标题列表）
GET  /api/projects/{code}/deliverables/{dcode}/sections/{seq}           某章正文（默认当前版本，?version= 看历史）和版本列表
PUT  /api/projects/{code}/deliverables/{dcode}/sections/{seq}           顾问修改某章：新建一个版本（可带 base_version 防覆盖）
GET  /api/projects/{code}/deliverables/{dcode}/sections/{seq}/diff      两个版本的差异（?a=1&b=2，统一 diff 格式）
GET  /api/projects/{code}/knowledge-updates                             引用的知识条目已更新或下线、需要复核的章节（二期 B4）
GET  /api/projects/{code}/deliverables/{dcode}/export                   导出 Word（各章当前版本，套模板；规则见 app/project/export.py）

严格档项目只收占位符版本（真名、IP、系统名 422）；已签署的交付件写入 409；机密级命中 422（不保存）。规则见 app/project/deliverables.py。
"""
from datetime import datetime

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import Deliverable, User
from ..project import deliverables as svc
from ..project import export as export_svc
from ..project import kb_impact

router = APIRouter(prefix="/api/projects", tags=["deliverables"], dependencies=[Depends(require_business)])


class SectionRow(BaseModel):
    seq: int
    title: str
    current_version: int
    chars: int
    updated_at: datetime
    sources: dict
    unresolved: list[str]
    knowledge_updates: list[dict] = []   # 引用的知识条目已更新 / 下线（二期 B4）


class DeliverableDetail(BaseModel):
    code: str
    title: str
    status: str
    revision: int
    updated_at: datetime | None
    updated_by: str
    sections: list[SectionRow]
    totals: dict                 # 当前版本的来源计数：evidence / knowledge / inferred / unresolved


class OutlineIn(BaseModel):
    titles: list[str] = Field(min_length=1, max_length=svc.MAX_SECTIONS)


class SectionIn(BaseModel):
    body: str = Field(min_length=1, max_length=svc.MAX_BODY_CHARS)
    note: str = Field(default="", max_length=svc.MAX_NOTE)
    base_version: int | None = None


class VersionMeta(BaseModel):
    version: int
    chars: int
    note: str
    author: str
    agent_code: str
    created_at: datetime


class SectionDetail(BaseModel):
    seq: int
    title: str
    current_version: int
    version: int                 # 本次返回的版本（0 = 还没有正文）
    body: str
    sources: dict
    note: str
    author: str
    agent_code: str
    created_at: datetime | None
    versions: list[VersionMeta]


def _raise(e: svc.DeliverableError):
    raise HTTPException(e.status, e.detail) from e


def _load(db: Session, code: str, dcode: str, user: User):
    p = project_for_member(db, code, user)
    try:
        return p, svc.require_deliverable(db, p, dcode)
    except svc.DeliverableError as e:
        _raise(e)


def _detail(db: Session, d: Deliverable) -> DeliverableDetail:
    updates = kb_impact.deliverable_updates(db, d)
    return DeliverableDetail(code=d.code, title=d.title, status=d.status, revision=d.revision,
                             updated_at=d.updated_at, updated_by=d.updated_by,
                             sections=[SectionRow(**r, knowledge_updates=updates.get(r["seq"], []))
                                       for r in svc.outline(d)],
                             totals={**svc.totals(d), "knowledge_updates": sum(len(x) for x in updates.values())})


def _section(d: Deliverable, seq: int, version: int | None = None) -> SectionDetail:
    try:
        s = svc.require_section(d, seq)
    except svc.DeliverableError as e:
        _raise(e)
    v = svc.get_version(s, version)
    if version is not None and v is None:
        raise HTTPException(404, "版本不存在")
    metas = [VersionMeta(version=x.version, chars=x.char_count, note=x.note, author=x.author, agent_code=x.agent_code,
                         created_at=x.created_at) for x in reversed(s.versions)]
    return SectionDetail(seq=s.seq, title=s.title, current_version=s.current_version, version=v.version if v else 0,
                         body=v.body if v else "", sources=v.sources if v else {}, note=v.note if v else "",
                         author=v.author if v else "", agent_code=v.agent_code if v else "",
                         created_at=v.created_at if v else None, versions=metas)


class KnowledgeUpdate(BaseModel):
    deliverable: str
    deliverable_title: str
    status: str
    section: int
    section_title: str
    section_version: int
    doc_id: str
    change: str                  # updated 条目发布了新版本 / retired 条目已下线
    current_version: int | None


@router.get("/{code}/knowledge-updates", response_model=list[KnowledgeUpdate])
def knowledge_updates(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return kb_impact.project_updates(db, project_for_member(db, code, user))


@router.get("/{code}/deliverables/{dcode}", response_model=DeliverableDetail)
def get_deliverable(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    return _detail(db, d)


@router.put("/{code}/deliverables/{dcode}/outline", response_model=DeliverableDetail)
def put_outline(code: str, dcode: str, body: OutlineIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        d = svc.set_outline(db, user, p, d, body.titles)
    except svc.DeliverableError as e:
        _raise(e)
    return _detail(db, d)


@router.get("/{code}/deliverables/{dcode}/sections/{seq}", response_model=SectionDetail)
def get_section(code: str, dcode: str, seq: int, version: int | None = Query(default=None, ge=1),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    _, d = _load(db, code, dcode, user)
    return _section(d, seq, version)


@router.put("/{code}/deliverables/{dcode}/sections/{seq}", response_model=SectionDetail, status_code=201)
def put_section(code: str, dcode: str, seq: int, body: SectionIn, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        svc.write_section(db, user, p, d, seq, body.body, note=body.note, base_version=body.base_version)
    except svc.DeliverableError as e:
        _raise(e)
    db.refresh(d)
    return _section(d, seq)


@router.get("/{code}/deliverables/{dcode}/sections/{seq}/diff")
def section_diff(code: str, dcode: str, seq: int, a: int = Query(ge=1), b: int = Query(ge=1),
                 db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _, d = _load(db, code, dcode, user)
    try:
        return {"seq": seq, "a": a, "b": b, "diff": svc.diff(svc.require_section(d, seq), a, b)}
    except svc.DeliverableError as e:
        _raise(e)


@router.get("/{code}/deliverables/{dcode}/export")
def export_deliverable(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    try:
        r = export_svc.export_deliverable(db, user, p, d)
    except export_svc.ExportError as e:
        raise HTTPException(e.status, e.detail) from e
    return Response(r.data, media_type=export_svc.MIME, headers={
        "Content-Disposition": f"attachment; filename=\"{p.code}-{d.code}.docx\"; filename*=UTF-8''{quote(r.filename)}",
        "X-Export-Id": r.export_id, "Cache-Control": "no-store"})
