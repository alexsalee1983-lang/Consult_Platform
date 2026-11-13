"""严格档客户同意记录与去标识摘要卡（M4 ④）。要求业务角色；只有项目成员能看能写，非成员一律 404。

GET  /api/projects/{code}/consents                       客户同意记录
POST /api/projects/{code}/consents                       登记客户书面同意（项目经理，严格档；写文号和签署日期）
POST /api/projects/{code}/consents/{id}/revoke           登记客户书面撤回（项目经理）：清空依据它上传的摘要卡
GET  /api/projects/{code}/summary-cards                  摘要卡清单（含内容）
GET  /api/projects/{code}/summary-cards/template         空白摘要卡模板（字段和规则，现场工具离场前下载）
POST /api/projects/{code}/summary-cards                  整批上传摘要卡（项目成员；须有有效同意；规则检查不过 422）
POST /api/projects/{code}/summary-cards/{card}/withdraw  撤下一张摘要卡（上传者或项目经理），内容清空
规则见 app/project/cards.py。
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..models.consent import ProjectConsent, SummaryCard
from ..project import cards as svc

router = APIRouter(prefix="/api/projects", tags=["summary-cards"], dependencies=[Depends(require_business)])


class ConsentOut(BaseModel):
    id: int
    doc_ref: str
    signed_on: date
    recorded_by: str
    recorded_at: datetime
    revoked: bool
    revoke_ref: str
    revoked_on: date | None
    revoked_by: str
    revoked_at: datetime | None


class ConsentIn(BaseModel):
    doc_ref: str = Field(max_length=64)
    signed_on: date


class RevokeIn(BaseModel):
    revoke_ref: str = Field(max_length=64)
    received_on: date


class CardIn(BaseModel):
    item: str = Field(max_length=1000)
    finding: str = Field(max_length=10000)
    magnitude: str = Field(default="", max_length=1000)
    onsite_refs: list[str] = Field(default_factory=list, max_length=100)
    client_reviewed_on: date | None = None


class CardsIn(BaseModel):
    cards: list[CardIn] = Field(max_length=svc.MAX_CARDS)


class CardOut(BaseModel):
    code: str
    item: str
    finding: str
    magnitude: str
    onsite_refs: list[str]
    client_reviewed_on: date
    consent_id: int
    chars: int
    uploaded_by: str
    created_at: datetime
    withdrawn: bool
    withdrawn_by: str
    withdrawn_at: datetime | None


def _consent(c: ProjectConsent) -> ConsentOut:
    return ConsentOut(id=c.id, doc_ref=c.doc_ref, signed_on=c.signed_on, recorded_by=c.recorded_by,
                      recorded_at=c.recorded_at, revoked=c.revoked, revoke_ref=c.revoke_ref, revoked_on=c.revoked_on,
                      revoked_by=c.revoked_by, revoked_at=c.revoked_at)


def _card(c: SummaryCard) -> CardOut:
    return CardOut(code=c.code, item=c.item, finding=c.finding, magnitude=c.magnitude, onsite_refs=c.onsite_refs,
                   client_reviewed_on=c.client_reviewed_on, consent_id=c.consent_id, chars=c.char_count,
                   uploaded_by=c.uploaded_by, created_at=c.created_at, withdrawn=c.withdrawn,
                   withdrawn_by=c.withdrawn_by, withdrawn_at=c.withdrawn_at)


def _raise(e: svc.CardError):
    raise HTTPException(e.status, e.detail) from e


@router.get("/{code}/consents", response_model=list[ConsentOut])
def list_consents(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_consent(c) for c in svc.list_consents(db, p)]


@router.post("/{code}/consents", response_model=ConsentOut, status_code=201)
def record_consent(code: str, body: ConsentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        return _consent(svc.record_consent(db, user, p, body.doc_ref, body.signed_on))
    except svc.CardError as e:
        _raise(e)


@router.post("/{code}/consents/{consent_id}/revoke", response_model=ConsentOut)
def revoke_consent(code: str, consent_id: int, body: RevokeIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        return _consent(svc.revoke_consent(db, user, p, consent_id, body.revoke_ref, body.received_on))
    except svc.CardError as e:
        _raise(e)


@router.get("/{code}/summary-cards", response_model=list[CardOut])
def list_cards(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_card(c) for c in svc.list_cards(db, p)]


@router.get("/{code}/summary-cards/template")
def card_template(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    if p.tier != "strict":
        raise HTTPException(422, svc.STANDARD_REFUSAL)
    return svc.template(db, p)


@router.post("/{code}/summary-cards", response_model=list[CardOut], status_code=201)
def upload_cards(code: str, body: CardsIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        out = svc.upload_cards(db, user, p, [svc.CardIn(c.item, c.finding, c.magnitude, c.onsite_refs,
                                                        c.client_reviewed_on) for c in body.cards])
    except svc.CardError as e:
        _raise(e)
    return [_card(c) for c in out]


@router.post("/{code}/summary-cards/{card}/withdraw", response_model=CardOut)
def withdraw_card(code: str, card: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    c = svc.get_card(db, p, card)
    if c is None:
        raise HTTPException(404, "摘要卡不存在")
    try:
        return _card(svc.withdraw_card(db, user, p, c))
    except svc.CardError as e:
        _raise(e)
