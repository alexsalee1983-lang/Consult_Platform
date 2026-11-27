"""四期 E2①：调研助手平台侧接口。要求业务角色；只有项目成员能看能写，非成员一律 404。

GET  /api/projects/{code}/survey/questions                 按项目画像选出的题目
POST /api/projects/{code}/survey/packs                     生成调研包（JSON，带包哈希；严格档不含画像字段）
GET  /api/projects/{code}/survey/packs                     调研包清单（不含题面）
POST /api/projects/{code}/survey/packs/{pack}/results      导入回传：封闭题转待确认事实，矛盾标出；不过检查整批 422
GET  /api/projects/{code}/survey/facts                     事实确认单（含开放题回答）
POST /api/projects/{code}/survey/facts/{id}/confirm        确认一条事实（只记确认人角色）
GET  /api/projects/{code}/survey/pool                      答案池：按题号汇聚，带来源渠道、可信度、题状态（四期 E3①）
GET  /api/projects/{code}/survey/spot-checks               自动确认的抽检队列
POST /api/projects/{code}/survey/facts/{id}/spot-check     抽检结论（通过 / 不通过退回人工）
POST /api/projects/{code}/survey/selfeval                    生成客户自评包（加密离线网页 + 邮件正文；口令只返回这一次）（四期 E3②）
GET  /api/projects/{code}/survey/selfeval                    客户自评包清单（状态、截止日、提醒阶段）
POST /api/projects/{code}/survey/selfeval/{s}/sent           记发出时间
GET  /api/projects/{code}/survey/selfeval/{s}/template.xlsx  Excel 模板（兜底）
POST /api/projects/{code}/survey/selfeval/{s}/import         导入浏览器本机解密后的答卷 → 答案池（客户自评）
POST /api/projects/{code}/survey/selfeval/{s}/import-xlsx    导入 Excel 答卷（文件不保存）
DELETE /api/projects/{code}/survey/selfeval/{s}/import       撤销本次导入
GET  /api/projects/{code}/survey/agenda                    现场议程：按受访角色分快速确认 / 议程 / 补问，补资料请求，转人工（四期 E3③）
题库只经命令行 survey-import 导入。规则见 app/project/survey.py。
"""
from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..models.survey import SurveyAnswer
from ..models.survey import SelfEvalPack
from ..project import selfeval as se
from ..project import agenda as agenda_svc
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
    source: str = Field(default="", max_length=16)   # 空 = 渠道默认来源；现场题卡可写 evidence（现场证据）


class ResponseIn(BaseModel):
    respondent_role: str = Field(max_length=64)
    answers: list[AnswerIn] = Field(max_length=500)


class ResultsIn(BaseModel):
    pack_hash: str = Field(max_length=64)
    channel: str = Field(default="onsite", max_length=16)
    responses: list[ResponseIn] = Field(max_length=svc.MAX_RESPONSES)


class ResultsOut(BaseModel):
    facts: int
    unclear: int
    open: int
    conflicts: int
    auto_confirmed: int
    requests: int = 0       # 证据不足、列进补资料请求的题数（E3③ 自动分流）
    to_manual: int = 0      # 转人工（发确认卡）的题数


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
    channel: str
    source: str
    credibility: int
    spot_check: str


class ConfirmIn(BaseModel):
    confirmed_role: str = Field(max_length=64)


class PoolOut(BaseModel):
    qid: str
    control: str
    key_control: bool
    status: str
    value: str
    answers: list[FactOut]


class SpotIn(BaseModel):
    passed: bool


def _fact(db: Session, a: SurveyAnswer) -> FactOut:
    return FactOut(id=a.id, qid=a.qid, control=a.control, kind=a.kind, respondent_role=a.respondent_role,
                   value=a.value, label=svc.option_label(db, a.qid, a.value) if a.value else "", text=a.text,
                   fact_status=a.fact_status, conflict=a.conflict, confirmed_role=a.confirmed_role,
                   confirmed_at=a.confirmed_at, channel=a.channel, source=a.source,
                   credibility=svc.CREDIBILITY.get(a.source, 0), spot_check=a.spot_check)


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
    responses = [svc.ResponseIn(r.respondent_role, [svc.AnswerIn(a.qid, a.value, a.text, a.source)
                                                    for a in r.answers]) for r in body.responses]
    try:
        return svc.import_results(db, user, p, pack, body.pack_hash, responses, body.channel)
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


@router.get("/{code}/survey/pool", response_model=list[PoolOut])
def answer_pool(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [PoolOut(**{**q, "answers": [_fact(db, a) for a in q["answers"]]}) for q in svc.pool(db, p)]


@router.get("/{code}/survey/spot-checks", response_model=list[FactOut])
def spot_checks(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_fact(db, a) for a in svc.spot_queue(db, p)]


@router.post("/{code}/survey/facts/{answer_id}/spot-check", response_model=FactOut)
def spot_check(code: str, answer_id: int, body: SpotIn, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    a = svc.get_answer(db, p, answer_id)
    if a is None:
        raise HTTPException(404, "事实不存在")
    try:
        return _fact(db, svc.spot_verdict(db, user, p, a, body.passed))
    except svc.SurveyError as e:
        _raise(e)


# ---------- 四期 E3②：客户自评包 ----------

class SelfEvalIn(BaseModel):
    pack: str = Field(max_length=16)
    respondent_role: str = Field(max_length=64)
    due_date: date


class SelfEvalCreated(BaseModel):
    code: str
    version: int
    pack_hash: str
    due_date: date
    password: str          # 只在这里返回一次，平台不保存
    html: str
    filename: str
    mail_text: str


class SelfEvalOut(BaseModel):
    code: str
    pack: str
    version: int
    respondent_role: str
    questions: int
    due_date: date
    status: str
    sent_at: datetime | None
    remind_stage: int
    imported_at: datetime | None
    answers: int
    created_by: str
    created_at: datetime


class SelfAnswerIn(BaseModel):
    qid: str = Field(max_length=32)
    value: str = Field(default="", max_length=16)
    note: str = Field(default="", max_length=500)


class SelfImportIn(BaseModel):
    version: int
    pack_hash: str = Field(max_length=64)
    answers: list[SelfAnswerIn] = Field(max_length=500)
    confirm: bool = False


class SelfImportOut(ResultsOut):
    unanswered: int
    mismatch: int
    extras: list[dict]


def _se_out(db: Session, sp: SelfEvalPack) -> SelfEvalOut:
    pack = db.get(svc.SurveyPack, sp.pack_id)
    return SelfEvalOut(code=sp.code, pack=pack.code if pack else "", version=sp.version,
                       respondent_role=sp.respondent_role, questions=len(sp.qids), due_date=sp.due_date,
                       status=sp.status, sent_at=sp.sent_at, remind_stage=sp.remind_stage,
                       imported_at=sp.imported_at, answers=len(sp.answer_ids or []), created_by=sp.created_by,
                       created_at=sp.created_at)


@router.post("/{code}/survey/selfeval", response_model=SelfEvalCreated, status_code=201)
def create_selfeval(code: str, body: SelfEvalIn, response: Response, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    response.headers["Cache-Control"] = "no-store"          # 口令只出现一次，不留缓存
    try:
        return se.generate(db, user, p, body.pack, body.respondent_role, body.due_date)
    except svc.SurveyError as e:
        _raise(e)


@router.get("/{code}/survey/selfeval", response_model=list[SelfEvalOut])
def list_selfeval(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    return [_se_out(db, sp) for sp in se.list_packs(db, p)]


@router.post("/{code}/survey/selfeval/{s}/sent", response_model=SelfEvalOut)
def selfeval_sent(code: str, s: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        return _se_out(db, se.mark_sent(db, user, p, se.get(db, p, s)))
    except svc.SurveyError as e:
        _raise(e)


@router.get("/{code}/survey/selfeval/{s}/template.xlsx")
def selfeval_template(code: str, s: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        sp = se.get(db, p, s)
    except svc.SurveyError as e:
        _raise(e)
    name = f"{p.code}-{sp.code}-v{sp.version}-客户自评.xlsx"
    return Response(se.template_xlsx(db, p, sp),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=\"{p.code}-{sp.code}.xlsx\"; "
                                                    f"filename*=UTF-8''{quote(name)}"})


@router.post("/{code}/survey/selfeval/{s}/import", response_model=SelfImportOut, status_code=201)
def selfeval_import(code: str, s: str, body: SelfImportIn, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    p = project_for_member(db, code, user)
    try:
        sp = se.get(db, p, s)
        answers = [se.Answer(a.qid, a.value, a.note) for a in body.answers]
        return se.import_answers(db, user, p, sp, body.version, body.pack_hash, answers, [], body.confirm)
    except svc.SurveyError as e:
        _raise(e)


@router.post("/{code}/survey/selfeval/{s}/import-xlsx", response_model=SelfImportOut, status_code=201)
async def selfeval_import_xlsx(code: str, s: str, file: UploadFile | None = File(None), confirm: bool = Form(False),
                               db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = project_for_member(db, code, user)          # 先判成员（非成员 404），再看文件
    try:
        sp = se.get(db, p, s)
        if file is None:
            raise svc.SurveyError(422, "请选择 Excel 答卷文件")
        data = await file.read(se.XLSX_MAX + 1)
        version, given_hash, answers, extras = se.from_xlsx(db, sp, se.read_xlsx(data))
        return se.import_answers(db, user, p, sp, version, given_hash, answers, extras, confirm, via="xlsx")
    except svc.SurveyError as e:
        _raise(e)


@router.delete("/{code}/survey/selfeval/{s}/import")
def selfeval_undo(code: str, s: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        return se.undo_import(db, user, p, se.get(db, p, s))
    except svc.SurveyError as e:
        _raise(e)
@router.get("/{code}/survey/agenda")
def survey_agenda(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    return agenda_svc.build(db, p)
