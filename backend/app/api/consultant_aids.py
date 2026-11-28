"""顾问便利性（四期 A32）。要求业务角色；只有项目成员能用，非成员一律 404。

POST /api/projects/{code}/deliverables/{dcode}/word-return            回传改好的 Word（multipart：file），改过的章新建版本并发起评审
GET  /api/projects/{code}/confirmations/evidence?status=open|all       确认卡证据原文并排（页码、段落）与同源合卡
GET  /api/projects/{code}/deliverables/{dcode}/sections/{seq}/kb-suggest?q=   编辑器知识侧栏：条款、公司做法、黄金样例

规则见 app/project/word_return.py、card_evidence.py、kb_suggest.py。
"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import current_user
from ..core.db import get_db
from ..core.permissions import project_for_member, require_business
from ..kb import search
from ..models import Confirmation, User
from ..project import card_evidence, kb_suggest, word_return
from ..project import deliverables as dsvc

router = APIRouter(prefix="/api/projects", tags=["consultant-aids"], dependencies=[Depends(require_business)])


def _load(db: Session, code: str, dcode: str, user: User):
    p = project_for_member(db, code, user)
    try:
        return p, dsvc.require_deliverable(db, p, dcode)
    except dsvc.DeliverableError as e:
        raise HTTPException(e.status, e.detail) from e


@router.post("/{code}/deliverables/{dcode}/word-return")
def word_return_upload(code: str, dcode: str, file: UploadFile | None = File(default=None),
                       db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)              # 先判成员（非成员 404），再看文件
    if file is None:
        raise HTTPException(422, "请选择改好的 Word 文件")
    data = file.file.read(word_return.MAX_BYTES + 1)
    try:
        r = word_return.apply(db, user, p, d, data)
    except word_return.WordReturnError as e:
        raise HTTPException(e.status, e.detail) from e
    return {"export_id": r.export_id, "changed": r.changed, "unchanged": r.unchanged, "missing": r.missing,
            "review": r.review, "review_note": r.review_note}


@router.get("/{code}/confirmations/evidence")
def confirmation_evidence(code: str, status: str = Query(default="open", pattern="^(open|all)$"),
                          db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p = project_for_member(db, code, user)
    q = select(Confirmation).where(Confirmation.project_id == p.id)
    if status == "open":
        q = q.where(Confirmation.status == "open")
    cards = list(db.scalars(q.order_by(Confirmation.id)))
    ev = card_evidence.evidence_for(db, p, cards)
    return {"groups": card_evidence.groups(cards, ev), "evidence": {str(k): v for k, v in ev.items()}}


@router.get("/{code}/deliverables/{dcode}/sections/{seq}/kb-suggest")
def section_kb_suggest(code: str, dcode: str, seq: int, q: str = Query(default="", max_length=200),
                       db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    p, d = _load(db, code, dcode, user)
    try:
        s = dsvc.require_section(d, seq)
    except dsvc.DeliverableError as e:
        raise HTTPException(e.status, e.detail) from e
    try:
        return kb_suggest.suggest(db, user, p, d, s, q)
    except search.SearchError as e:
        raise HTTPException(e.status, e.detail) from e
