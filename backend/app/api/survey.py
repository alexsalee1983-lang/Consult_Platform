"""四期 E2①：调研助手平台侧接口。要求业务角色；只有项目成员能看能写，非成员一律 404。

GET  /api/projects/{code}/survey/questions                 按项目画像选出的题目
POST /api/projects/{code}/survey/packs                     生成调研包（JSON，带包哈希；严格档不含画像字段）
GET  /api/projects/{code}/survey/packs                     调研包清单（不含题面）
POST /api/projects/{code}/survey/packs/{pack}/results      导入回传：封闭题转待确认事实，矛盾标出；不过检查整批 422
GET  /api/projects/{code}/survey/facts                     事实确认单（含开放题回答）
POST /api/projects/{code}/survey/facts/{id}/confirm        确认一条事实（只记确认人角色）
题库只经命令行 survey-import 导入。规则见 app/project/survey.py。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..models.survey import SurveyAnswer
from ..project import survey as svc

router = APIRouter(prefix="/api/projects", tags=["survey"], dependencies=[Depends(require_business)])


class QuestionOut(BaseModel):
    qid: str
    control: str
    kind: str
    text: str
    options: list[dict]


class PackOut(BaseModel):
    code: str
    pack_hash: str
    tier: str
    questions: int
    created_by: str
    created_at: datetime
    returned: int


class AnswerIn(BaseModel):
    qid: str = Field(max_length=32)
    value: str = Field(default="", max_length=16)
    text: str = Field(default="", max_length=4000)


class ResponseIn(BaseModel):
    respondent_role: str = Field(max_length=64)
    answers: list[AnswerIn] = Field(max_length=500)


class ResultsIn(BaseModel):
    pack_hash: str = Field(max_length=64)
    responses: list[ResponseIn] = Field(max_length=svc.MAX_RESPONSES)


class ResultsOut(BaseModel):
    facts: int
    unclear: int
    open: int
    conflicts: int


class FactOut(BaseModel):
    id: int
    qid: str
    control: str
    kind: str
    respondent_role: str
    value: str
    label: str
    text: str
    fact_status: str
    conflict: bool
    confirmed_role: str
    confirmed_at: datetime | None


class ConfirmIn(BaseModel):
    confirmed_role: str = Field(max_length=64)


def _fact(db: Session, a: SurveyAnswer) -> FactOut:
    return FactOut(id=a.id, qid=a.qid, control=a.control, kind=a.kind, respondent_role=a.respondent_role,
                   value=a.value, label=svc.option_label(db, a.qid, a.value) if a.value else "", text=a.text,
                   fact_status=a.fact_status, conflict=a.conflict, confirmed_role=a.confirmed_role,
                   confirmed_at=a.confirmed_at)


def _raise(e: svc.SurveyError):
    raise HTTPException(e.status, e.detail) from e


@router.get("/{code}/survey/questions", response_model=list[QuestionOut])
def questions(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [QuestionOut(**{"options": [], **svc.question_json(q, p.industry)})
            for q in svc.select_questions(db, p)]


@router.post("/{code}/survey/packs", status_code=201)
def create_pack(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        return svc.build_pack(db, user, p)
    except svc.SurveyError as e:
        raise HTTPException(e.status, e.detail) from e


@router.get("/{code}/survey/packs", response_model=list[PackOut])
def list_packs(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [PackOut(code=k.code, pack_hash=k.pack_hash, tier=k.tier, questions=len(k.qids), created_by=k.created_by,
                    created_at=k.created_at, returned=k.returned) for k in svc.list_packs(db, p)]


@router.post("/{code}/survey/packs/{pack}/results", response_model=ResultsOut, status_code=201)
def import_results(code: str, pack: str, body: ResultsIn, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    responses = [svc.ResponseIn(r.respondent_role, [svc.AnswerIn(a.qid, a.value, a.text) for a in r.answers])
                 for r in body.responses]
    try:
        return svc.import_results(db, user, p, pack, body.pack_hash, responses)
    except svc.SurveyError as e:
        _raise(e)


@router.get("/{code}/survey/facts", response_model=list[FactOut])
def facts(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_fact(db, a) for a in svc.list_answers(db, p)]


@router.post("/{code}/survey/facts/{answer_id}/confirm", response_model=FactOut)
def confirm(code: str, answer_id: int, body: ConfirmIn, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    a = svc.get_answer(db, p, answer_id)
    if a is None:
        raise HTTPException(404, "事实不存在")
    try:
        return _fact(db, svc.confirm_fact(db, user, p, a, body.confirmed_role))
    except svc.SurveyError as e:
        _raise(e)
