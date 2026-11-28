"""立项与合同要点卡、授权台账、先行启动（四期 A32）。规则见 app/project/intake.py。

GET  /api/projects/{code}/intake                       要点卡（当前版本与历史版本）、授权台账、关口 1 还差什么、先行启动状态
POST /api/projects/{code}/intake/basis                 上传立项依据（multipart：file、basis_kind），后台 Agent 抽要点卡（202）
PUT  /api/projects/{code}/intake/card                  保存要点卡（fields: {键: {value, state}}，checked=true 为核对完成）
POST /api/projects/{code}/intake/authorizations        登记授权（kind、status、version、signed_on），追加式留历史
POST /api/projects/{code}/early-start/approve          部门负责人一键确认先行启动（项目负责人角色，不必是成员）
GET  /api/early-starts                                 待确认或超期未签合同的先行启动项目（项目负责人，只含元数据）

前四个先判成员（非成员 404），写操作只给本项目的项目经理（403）；严格档上传依据 422（手填要点卡）。
"""
from datetime import date
from typing import NoReturn

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import PROJECT_LEAD, PROJECT_MANAGER, membership, project_for_member, require_business, roles_of
from ..models import Project, User
from ..project import intake as svc

router = APIRouter(prefix="/api", tags=["intake"], dependencies=[Depends(require_business)])


def _raise(e: svc.IntakeError) -> NoReturn:
    raise HTTPException(e.status, e.detail) from e


def _overview(db: Session, p: Project, user: User) -> dict:
    m = membership(db, p.id, user.id)
    return {**svc.overview(db, p), "is_manager": m is not None and m.project_role == PROJECT_MANAGER}


@router.get("/projects/{code}/intake")
def get_intake(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    return _overview(db, p, user)


@router.post("/projects/{code}/intake/basis", status_code=202)
def upload_basis(code: str, file: UploadFile | None = File(default=None), basis_kind: str = Form(default="contract"),
                 db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)          # 先判成员（非成员 404），再看文件
    if file is None:
        raise HTTPException(422, "请选择依据文件")
    data = file.file.read(svc.evidence.NOTE_FILE_MAX + 1)
    try:
        card = svc.upload_basis(db, user, p, basis_kind, file.filename or "", data)
    except svc.IntakeError as e:
        _raise(e)
    return svc.card_out(card)


class CardIn(BaseModel):
    fields: dict[str, dict] = Field(default_factory=dict)
    checked: bool = False


@router.put("/projects/{code}/intake/card")
def save_card(code: str, body: CardIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        card = svc.save_card(db, user, p, body.fields, body.checked)
    except svc.IntakeError as e:
        _raise(e)
    return svc.card_out(card)


class AuthIn(BaseModel):
    kind: str = Field(max_length=16)
    status: str = Field(default="signed", max_length=16)
    version: str = Field(default="", max_length=32)
    signed_on: date


@router.post("/projects/{code}/intake/authorizations")
def record_auth(code: str, body: AuthIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        svc.record_auth(db, user, p, body.kind, body.status, body.version, body.signed_on)
    except svc.IntakeError as e:
        _raise(e)
    return _overview(db, p, user)


def _require_lead(db: Session, user: User) -> None:
    if PROJECT_LEAD not in roles_of(db, user.id):
        raise HTTPException(403, "需要“项目负责人”角色")


@router.post("/projects/{code}/early-start/approve")
def approve_early_start(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _require_lead(db, user)                          # 非负责人一律 403，不暴露项目是否存在
    p = db.scalar(select(Project).where(Project.code == code, Project.deleted_at.is_(None)))
    if p is None:
        raise HTTPException(404, "项目不存在")
    try:
        svc.approve_early_start(db, user, p)
    except svc.IntakeError as e:
        _raise(e)
    return svc.early_start(db, p) or {}


@router.get("/early-starts")
def early_starts(db: Session = Depends(get_db), user: User = Depends(current_user)) -> list[dict]:
    _require_lead(db, user)
    return svc.pending_early_starts(db)
