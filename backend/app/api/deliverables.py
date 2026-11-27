"""交付件分章节与版本（M3）。要求业务角色；只有项目成员能看能写，非成员一律 404。

GET  /api/projects/{code}/deliverables/{dcode}                          交付件概览：大纲、各章当前版本、来源计数
PUT  /api/projects/{code}/deliverables/{dcode}/outline                  设置大纲（完整的章节标题列表）
GET  /api/projects/{code}/deliverables/{dcode}/sections/{seq}           某章正文（默认当前版本，?version= 看历史）和版本列表
PUT  /api/projects/{code}/deliverables/{dcode}/sections/{seq}           顾问修改某章：新建一个版本（可带 base_version 防覆盖）
GET  /api/projects/{code}/deliverables/{dcode}/sections/{seq}/diff      两个版本的差异（?a=1&b=2，统一 diff 格式）
GET  /api/projects/{code}/assessment                                    成熟度初判、符合性条、风险矩阵（二期 A10，规则见 app/project/assessment.py）
GET  /api/projects/{code}/knowledge-updates                             引用的知识条目已更新或下线、需要复核的章节（二期 B4）
GET  /api/projects/{code}/deliverables/{dcode}/export                   导出 Word（各章当前版本，套模板；规则见 app/project/export.py）
     ?format=pptx 导出 PowerPoint（二期 A9，规则见 app/project/export_pptx.py）
     挂本交付件、未关闭且已“冻结导出”的质量事件存在时 409（四期 F3②）
POST /api/projects/{code}/deliverables/{dcode}/form/import              上传合同（multipart：file），后台 Agent 抽取表单要点（四期 A28②，202）
GET  /api/projects/{code}/deliverables/{dcode}/form/import              最近一次合同导入的状态与抽取结果（回填表单，顾问核对后提交）

严格档项目只收占位符版本（真名、IP、系统名 422）；已签署的交付件写入 409；机密级命中 422（不保存）。规则见 app/project/deliverables.py。
"""
from datetime import datetime

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents.knowledge import product_code
from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import PROJECT_MANAGER, PROJECT_ROLES, project_for_member, require_business
from ..models import Deliverable, ProjectMember, User
from ..project import assessment
from ..project import deliverables as svc
from ..project import export as export_svc
from ..project import export_pptx
from ..project import form_import
from ..project import forms as forms_svc
from ..project import kb_impact, loops
from ..products import config as product_config
from ..services import quality_incidents

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
    stale: str = ""                      # 上游变更后待复核的原因（四期 A31⑫），空 = 无


class DeliverableDetail(BaseModel):
    code: str
    title: str
    status: str
    revision: int
    updated_at: datetime | None
    updated_by: str
    sections: list[SectionRow]
    totals: dict                 # 当前版本的来源计数：evidence / knowledge / inferred / unresolved


class FormIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


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


class Score(BaseModel):
    domain: str
    current: float
    target: float
    basis: str


class DomainCounts(BaseModel):
    domain: str
    counts: dict[str, int]       # met 符合 / partial 部分符合 / unmet 不符合 / insufficient 证据不足 / na 不适用


class Finding(BaseModel):
    code: str
    domain: str
    item: str
    conclusion: str
    impact: str                  # 高 / 中 / 低
    likelihood: str


class Assessment(BaseModel):
    scores_enabled: bool         # 产品配置 maturity_scores；数据安全为 false
    scores_label: str            # 成熟度初判；ISO 27001 为符合性初判
    source: str                  # gap：最近一次差距分析；空：还没有
    scores_source: str           # D4：已签署 D4 的成熟度初判表；gap；空
    scores: list[Score]
    domains: list[DomainCounts]
    findings: list[Finding]      # 风险矩阵：不符合、部分符合、证据不足且写了影响和可能性


@router.get("/{code}/assessment", response_model=Assessment)
def project_assessment(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return assessment.view(db, project_for_member(db, code, user))


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


@router.get("/{code}/deliverables/{dcode}/form")
def get_form(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    """四期 A14：D1、D2 表单定义；writable=False 表示已有正文，表单只读（之后在编辑器里改）。"""
    p, d = _load(db, code, dcode, user)
    form = forms_svc.load_form(d.code)
    if form is None:
        raise HTTPException(404, f"{d.code} 没有表单")
    try:
        prod = product_config.product(product_code(p.service))
    except product_config.CatalogError:
        prod = None                                     # 产品配置不可用时不预填，表单照常可填
    rows = db.execute(select(User.display_name, User.username, ProjectMember.project_role)
                      .join(ProjectMember, ProjectMember.user_id == User.id).where(ProjectMember.project_id == p.id)
                      .order_by(ProjectMember.project_role != PROJECT_MANAGER, User.username)).all()
    members = [(dn or un, PROJECT_ROLES.get(r, r)) for dn, un, r in rows]   # A28① 角色与职责按项目成员预填
    return {**forms_svc.schema(form, p.tier, prod, p, members), "writable": not any(s.current_version for s in d.sections)}


@router.post("/{code}/deliverables/{dcode}/form", response_model=DeliverableDetail, status_code=201)
def post_form(code: str, dcode: str, body: FormIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    form = forms_svc.load_form(d.code)
    if form is None:
        raise HTTPException(404, f"{d.code} 没有表单")
    try:
        d = forms_svc.submit(db, user, p, d, form, body.values)
    except (svc.DeliverableError, forms_svc.FormError) as e:
        raise HTTPException(e.status, e.detail) from e
    return _detail(db, d)


@router.post("/{code}/deliverables/{dcode}/form/import", status_code=202)
def import_form(code: str, dcode: str, file: UploadFile | None = File(default=None), db: Session = Depends(get_db),
                user: User = Depends(current_user)) -> dict:
    """四期 A28②：上传合同，后台由 Agent 抽取表单要点；严格档 422，机密级命中 422（文件不保存）。"""
    p, d = _load(db, code, dcode, user)             # 先判成员（非成员 404），再看文件
    if file is None:
        raise HTTPException(422, "请选择合同文件")
    data = file.file.read(form_import.evidence.NOTE_FILE_MAX + 1)
    try:
        job_id = form_import.start(db, user, p, d, file.filename or "", data)
    except form_import.ImportError_ as e:
        raise HTTPException(e.status, e.detail) from e
    return {"job_id": job_id, "status": "queued"}


@router.get("/{code}/deliverables/{dcode}/form/import")
def import_form_state(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    """最近一次合同导入的状态；done 时 values 为抽取结果，前端回填表单待顾问核对。"""
    p, d = _load(db, code, dcode, user)
    return form_import.state(db, p, d)


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


@router.get("/{code}/deliverables/{dcode}/errata")
def errata(code: str, dcode: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    """四期 A31②：已签署件引用的条目下线后的勘误页（按当前知识状态生成，原签署版本不动）。"""
    p, d = _load(db, code, dcode, user)
    return loops.errata_page(db, p, d)


@router.get("/{code}/deliverables/{dcode}/export")
def export_deliverable(code: str, dcode: str, format: str = Query(default="docx", pattern="^(docx|pptx)$"),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    p, d = _load(db, code, dcode, user)
    frozen = quality_incidents.export_frozen(db, p.code, d.code)
    if frozen is not None:                           # 四期 F3②：质量事件遏制“冻结导出”
        audit.record(db, actor=user.username, action="deliverable.export", result="blocked", project_code=p.code,
                     deliverable=d.code, reason="quality_incident_frozen", incident=frozen)
        raise HTTPException(409, f"{d.code} 因质量事件 #{frozen} 冻结导出，事件复盘关闭后恢复")
    mod = export_pptx if format == "pptx" else export_svc
    try:
        r = mod.export_deliverable(db, user, p, d)
    except export_svc.ExportError as e:
        raise HTTPException(e.status, e.detail) from e
    return Response(r.data, media_type=mod.MIME, headers={
        "Content-Disposition": f"attachment; filename=\"{p.code}-{d.code}.{format}\"; filename*=UTF-8''{quote(r.filename)}",
        "X-Export-Id": r.export_id, "Cache-Control": "no-store"})
