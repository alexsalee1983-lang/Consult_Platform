"""合同要点卡规则检查、需求澄清清单与纪要回填（四期 A40）。要求业务角色；只有项目成员能用，非成员 404。

POST /api/projects/{code}/contract/check              要点卡 → 规则检查（只列例外）、澄清题、待我处理
POST /api/projects/{code}/contract/clarifications.docx 要点卡 → 需求澄清清单 Word（一页，可作会议议程）
POST /api/projects/{code}/contract/answers            会上直接勾选的答案与新需求 → 回填结果与变更单草稿
POST /api/projects/{code}/contract/minutes            导入会议纪要（multipart：file、card、gates），后台对题抽答案（202）
GET  /api/projects/{code}/contract/minutes            最近一次纪要回填的状态与结果
规则见 app/project/contract_checks.py；要点卡本身由立项页保存（A32），这里只读入、不改。
"""
import json
from typing import NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core import audit
from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..models import User
from ..project import contract_checks as svc
from ..project import evidence

router = APIRouter(prefix="/api/projects", tags=["contract"], dependencies=[Depends(require_business)])


class CardIn(BaseModel):
    card: dict[str, dict | str] = Field(default_factory=dict)


class AnswersIn(CardIn):
    answers: list[dict] = Field(default_factory=list, max_length=100)
    requests: list[dict] = Field(default_factory=list, max_length=50)
    gates_due: list[str] = Field(default_factory=list, max_length=5)


def _raise(e: svc.ContractError) -> NoReturn:
    raise HTTPException(e.status, e.detail) from e


@router.post("/{code}/contract/check")
def check(code: str, body: CardIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        return svc.evaluate(db, p, body.card)
    except svc.ContractError as e:
        _raise(e)


@router.post("/{code}/contract/clarifications.docx")
def clarification_docx(code: str, body: CardIn, db: Session = Depends(get_db),
                       user: User = Depends(current_user)) -> Response:
    p = project_for_member(db, code, user)
    try:
        ev = svc.evaluate(db, p, body.card)
    except svc.ContractError as e:
        _raise(e)
    if not ev["questions"]:
        raise HTTPException(422, "没有需要澄清的题")
    data = svc.clarification_docx(p, ev["questions"])
    audit.record(db, actor=user.username, action="contract.clarify_export", result="allowed", project_code=p.code,
                 questions=len(ev["questions"]))
    db.commit()
    name = f"{p.code}-需求澄清清单.docx"
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename=\"{p.code}-clarify.docx\"; "
                             f"filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store"})


@router.post("/{code}/contract/answers")
def answers(code: str, body: AnswersIn, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    try:
        return svc.answer(db, user, p, body.card, body.answers, body.requests, body.gates_due)
    except svc.ContractError as e:
        _raise(e)


@router.post("/{code}/contract/minutes", status_code=202)
def import_minutes(code: str, file: UploadFile | None = File(default=None), card: str = Form(default="{}"),
                   gates: str = Form(default=""), db: Session = Depends(get_db),
                   user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)          # 先判成员（非成员 404），再看文件
    if file is None:
        raise HTTPException(422, "请选择会议纪要文件")
    try:
        card_data = json.loads(card or "{}")
    except ValueError:
        raise HTTPException(422, "要点卡格式不对") from None
    if not isinstance(card_data, dict):
        raise HTTPException(422, "要点卡格式不对")
    data = file.file.read(evidence.NOTE_FILE_MAX + 1)
    try:
        job_id = svc.start_minutes(db, user, p, card_data, [g for g in gates.split(",") if g], file.filename or "", data)
    except svc.ContractError as e:
        _raise(e)
    return {"job_id": job_id, "status": "queued"}


@router.get("/{code}/contract/minutes")
def minutes_state(code: str, db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    return svc.minutes_state(db, p)
